from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, make_response
import pymysql
from flask_session import Session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from textblob import TextBlob
from dotenv import load_dotenv
from PIL import Image
from translations import translations
from flask_mail import Mail, Message
import io
import requests
import os
import uuid
import base64
import PyPDF2
import docx
import webbrowser

# =====================================================
# LOAD ENVIRONMENT VARIABLES
# =====================================================

load_dotenv()

# =====================================================
# FLASK APP CONFIGURATION
# =====================================================

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")

app.config['SESSION_TYPE'] = 'filesystem'

Session(app)

# =====================================================
# FLASK MAIL CONFIGURATION
# =====================================================

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True

app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")

print("MAIL USER:", app.config['MAIL_USERNAME'])
print("MAIL PASS EXISTS:", bool(app.config['MAIL_PASSWORD']))

mail = Mail(app)

# =====================================================
# PROFILE IMAGE UPLOAD CONFIG
# =====================================================

UPLOAD_FOLDER = 'static/profile_images'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =====================================================
# DOCUMENT UPLOAD CONFIG
# =====================================================

DOCUMENT_FOLDER = 'uploads/documents'

app.config['DOCUMENT_FOLDER'] = DOCUMENT_FOLDER

os.makedirs(DOCUMENT_FOLDER, exist_ok=True)

# =====================================================
# AI GENERATED IMAGE FOLDER CONFIG
# =====================================================

GENERATED_FOLDER = 'static/generated_images'

app.config['GENERATED_FOLDER'] = GENERATED_FOLDER

os.makedirs(GENERATED_FOLDER, exist_ok=True)

# =====================================================
# OPENROUTER API CONFIGURATION
# =====================================================

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

print("OPENROUTER KEY:", OPENROUTER_API_KEY)
print("KEY TYPE:", type(OPENROUTER_API_KEY))

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "http://localhost:5000",
    "X-Title": "AI Chatbot"
}

# =====================================================
# TEST OPENROUTER CONNECTION
# =====================================================

try:

    test_payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": "Hello"
            }
        ]
    }

    response = requests.post(
        OPENROUTER_URL,
        headers=headers,
        json=test_payload,
        timeout=30
    )

    print("OPENROUTER STATUS:", response.status_code)
    print("OPENROUTER RESPONSE:", response.text)

except Exception as e:

    print("OPENROUTER CONNECTION ERROR:", e)

# =====================================================
# MYSQL DATABASE CONNECTION
# =====================================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ai_chatbot_db")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_SSL = os.getenv("DB_SSL", "false").lower() == "true"


def get_connection():
    connection_options = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10
    }

    if DB_SSL:
        connection_options["ssl"] = {}

    return pymysql.connect(**connection_options)

# =====================================================
# HOME PAGE
# =====================================================

@app.route('/')
def index():

    conn = get_connection()

    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # ================= TOTAL USERS =================

    cursor.execute("""
        SELECT COUNT(*) AS total_users
        FROM users
    """)

    total_users = cursor.fetchone()['total_users']

    # ================= TOTAL CHATS =================

    cursor.execute("""
        SELECT COUNT(*) AS total_chats
        FROM chat_history
    """)

    total_chats = cursor.fetchone()['total_chats']

    cursor.close()

    conn.close()

    return render_template(

        'index.html',

        total_users=total_users,

        total_chats=total_chats,

        ai_accuracy=99,

        ai_availability="24/7"

    )

# =====================================================
# REGISTER PAGE
# =====================================================

@app.route('/register', methods=['GET', 'POST'])
def register():

    if request.method == 'POST':

        username = request.form['username']
        email = request.form['email']
        password = request.form['password']

        hashed_password = generate_password_hash(password)

        conn = get_connection()
        cursor = conn.cursor()

        sql = """
        INSERT INTO users(username, email, password)
        VALUES(%s, %s, %s)
        """

        cursor.execute(sql, (username, email, hashed_password))

        conn.commit()
        conn.close()

        flash('Registration Successful')

        return redirect(url_for('login'))

    return render_template('register.html')

# =====================================================
# LOGIN PAGE
# =====================================================


@app.route('/login', methods=['GET', 'POST'])
def login():

    # ==========================================
    # GET METHOD
    # ==========================================

    if request.method == 'GET':

        remembered_email = request.cookies.get(
            'remember_email'
        )

        return render_template(

            'login.html',

            remembered_email=remembered_email

        )

    # ==========================================
    # POST METHOD
    # ==========================================

    email = request.form['email']
    password = request.form['password']

    remember = request.form.get('remember')

    print(email)
    print(password)

    # ==========================================
    # ADMIN LOGIN
    # ==========================================

    if email.strip() == "admin@gmail.com" and password.strip() == "admin123":

        session['user_id'] = "admin"

        session['username'] = "Admin"

        session['role'] = "admin"

        response = make_response(
            redirect(url_for('admin_dashboard'))
        )

        # ==========================================
        # REMEMBER ME
        # ==========================================

        if remember:

            response.set_cookie(

                'remember_email',

                email,

                max_age=60 * 60 * 24 * 30
            )

        else:

            response.delete_cookie(
                'remember_email'
            )

        return response

    # ==========================================
    # NORMAL USER LOGIN
    # ==========================================

    conn = get_connection()

    cursor = conn.cursor()

    sql = """
        SELECT * FROM users
        WHERE email=%s
    """

    cursor.execute(sql, (email,))

    user = cursor.fetchone()

    conn.close()

    # ==========================================
    # PASSWORD CHECK
    # ==========================================

    if user and check_password_hash(
        user['password'],
        password
    ):

        session['user_id'] = user['id']

        session['username'] = user['username']

        session['role'] = "user"

        response = make_response(
            redirect(url_for('dashboard'))
        )

        # ==========================================
        # REMEMBER ME
        # ==========================================

        if remember:

            response.set_cookie(

                'remember_email',

                email,

                max_age=60 * 60 * 24 * 30
            )

        else:

            response.delete_cookie(
                'remember_email'
            )

        return response

    else:

        flash('Invalid Email or Password')

        return redirect(url_for('login'))

# =====================================================
# LOGOUT
# =====================================================

@app.route('/logout')
def logout():

    session.clear()

    return redirect(url_for('index'))

# =====================================================
# DASHBOARD
# =====================================================

@app.route('/dashboard')
def dashboard():

    # 🔐 login check
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # ================= TOTAL CHATS =================
    cursor.execute("""
        SELECT COUNT(*) AS total_chats
        FROM chats
        WHERE user_id = %s
    """, (user_id,))

    result = cursor.fetchone()
    total_chats = result['total_chats'] if result else 0

    # ================= USER DETAILS =================
    cursor.execute("""
        SELECT username, email, profile_image
        FROM users
        WHERE id = %s
    """, (user_id,))

    user = cursor.fetchone()

    conn.close()

    return render_template(
        'dashboard.html',
        total_chats=total_chats,
        user=user
    )
# =====================================================
# CHAT PAGE
# =====================================================

@app.route('/chat')
def chat():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # USER DATA + SELECTED MODEL
    cursor.execute("""
        SELECT *
        FROM users
        WHERE id = %s
    """, (user_id,))

    user = cursor.fetchone()

    # DEFAULT MODEL IF NONE
    if not user.get('selected_model'):
        user['selected_model'] = "gpt-4o-mini"

    conn.close()

    return render_template('chat.html', user=user)

# =====================================================
# AI CHAT API
# =====================================================
# =====================================================
# AI CHAT API
# =====================================================
@app.route('/get_ai_response', methods=['POST'])
def get_ai_response():

    try:

        # ===================================
        # LOGIN CHECK
        # ===================================

        if 'user_id' not in session:

            return jsonify({
                'error': 'Unauthorized'
            }), 401


        # ===================================
        # GET USER MESSAGE
        # ===================================

        data = request.get_json()

        user_message = data.get('message', '').strip()


        if not user_message:

            return jsonify({
                'error': 'Message is empty'
            }), 400


        # ===================================
        # OPEN WEBSITE COMMANDS
        # ===================================

        lower_msg = user_message.lower().strip()


        # EXAMPLE:
        # open youtube
        # open amazon
        # open github.com
        # open https://google.com

        if lower_msg.startswith("open "):

            site_name = lower_msg.replace("open ", "").strip()


            # REMOVE EXTRA SPACES
            clean_name = site_name.replace(" ", "")


            # URL GENERATION
            if clean_name.startswith("http://") or clean_name.startswith("https://"):

                open_url = clean_name

            elif "." in clean_name:

                open_url = f"https://{clean_name}"

            else:

                open_url = f"https://www.{clean_name}.com"


            return jsonify({

                "action": "open_website",

                "url": open_url,

                "response": f"Opening {site_name.title()} 🚀"

            })


        # ===================================
        # SENTIMENT ANALYSIS
        # ===================================

        try:

            analysis = TextBlob(user_message)

            polarity = analysis.sentiment.polarity


            if polarity > 0:

                sentiment = 'Positive'

            elif polarity < 0:

                sentiment = 'Negative'

            else:

                sentiment = 'Neutral'

        except Exception as e:

            print("SENTIMENT ERROR:", e)

            sentiment = "Neutral"


        # ===================================
        # AI PROMPT
        # ===================================

        prompt = f"""
You are a friendly AI assistant.

User Message:
{user_message}

Sentiment:
{sentiment}

Reply shortly and helpfully.
"""


        # ===================================
        # OPENROUTER REQUEST
        # ===================================

        payload = {

            "model": "openai/gpt-4o-mini",

            "messages": [

                {
                    "role": "system",
                    "content": "You are a friendly AI assistant."
                },

                {
                    "role": "user",
                    "content": prompt
                }

            ]

        }


        response = requests.post(

            OPENROUTER_URL,

            headers=headers,

            json=payload,

            timeout=30

        )


        print("STATUS CODE:", response.status_code)

        print("RAW RESPONSE:", response.text)


        # ===================================
        # SAFE JSON PARSE
        # ===================================

        try:

            result = response.json()

        except Exception as e:

            print("JSON PARSE ERROR:", e)

            return jsonify({

                "response": "⚠️ Invalid AI response."

            })


        # ===================================
        # EXTRACT AI RESPONSE
        # ===================================

        ai_response = "⚠️ AI response unavailable."


        try:

            if result.get("choices"):

                ai_response = result["choices"][0]["message"]["content"]

            elif result.get("error"):

                ai_response = result["error"]["message"]

        except Exception as e:

            print("AI PARSE ERROR:", e)

            ai_response = "⚠️ Failed to parse AI response."


        # ===================================
        # SAVE CHAT TO DATABASE
        # ===================================

        try:

            conn = get_connection()

            cursor = conn.cursor()


            sql = """
            INSERT INTO chats(
                user_id,
                user_message,
                ai_response,
                sentiment
            )
            VALUES(%s, %s, %s, %s)
            """


            cursor.execute(sql, (

                session['user_id'],
                user_message,
                ai_response,
                sentiment

            ))


            conn.commit()

            cursor.close()

            conn.close()

        except Exception as db_error:

            print("DATABASE ERROR:", db_error)


        # ===================================
        # RETURN RESPONSE
        # ===================================

        return jsonify({

            'response': ai_response,

            'sentiment': sentiment

        })


    # ===================================
    # GLOBAL ERROR
    # ===================================

    except Exception as e:

        print("AI ERROR:", e)

        return jsonify({

            'response': '⚠️ Something went wrong while generating AI response.'

        }), 500 
# =====================================================
# WATCH DEMO PAGE
# =====================================================

@app.route('/watch_demo')
def watch_demo():

    return render_template('watch_demo.html')
# =====================================================
# CHAT HISTORY
# =====================================================
@app.route('/chat_history')
def chat_history():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    user_id = session['user_id']

    # ---------------- GET USER INFO ----------------
    cursor.execute("""
        SELECT id, username, profile_image
        FROM users
        WHERE id=%s
    """, (user_id,))
    user = cursor.fetchone()

    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())

    # ---------------- ALL CHATS ----------------
    cursor.execute("""
        SELECT * FROM chats
        WHERE user_id=%s
        ORDER BY id DESC
    """, (user_id,))
    chats = cursor.fetchall()

    # ---------------- WEEK CHATS COUNT ----------------
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM chats
        WHERE user_id=%s
        AND created_at >= %s
    """, (user_id, start_of_week))

    week_count = cursor.fetchone()['total']

    # ---------------- WEEK POSITIVE % ----------------
    cursor.execute("""
        SELECT sentiment
        FROM chats
        WHERE user_id=%s
        AND created_at >= %s
    """, (user_id, start_of_week))

    rows = cursor.fetchall()

    total = len(rows)
    positive = sum(1 for r in rows if r['sentiment'] == "Positive")

    positive_percent = round((positive / total) * 100) if total > 0 else 0

    conn.close()

    return render_template(
        'chat_history.html',
        chats=chats,
        week_count=week_count,
        positive_percent=positive_percent,
        user=user   # 👈 IMPORTANT
    )
#======================================================
# PROFILE PAGE
# =====================================================

@app.route('/profile')
def profile():

    # ================= LOGIN CHECK =================
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # ================= USER DATA =================
    cursor.execute("""
        SELECT 
            id,
            username,
            email,
            profile_image,
            created_at,
            bio,
            phone,
            dob,
            country,
            gender,
            portfolio_link
        FROM users
        WHERE id = %s
    """, (user_id,))

    user = cursor.fetchone()

    if not user:
        conn.close()
        return redirect(url_for('login'))

    # ================= SAFE STATS FUNCTION =================
    def safe_count(query, params):
        try:
            cursor.execute(query, params)
            result = cursor.fetchone()
            return list(result.values())[0] if result else 0
        except:
            return 0

    # ================= TOTAL CHATS =================
    total_chats = safe_count("""
        SELECT COUNT(*) AS total
        FROM chats
        WHERE user_id = %s
    """, (user_id,))

    # ================= TOTAL MESSAGES =================
    total_messages = safe_count("""
        SELECT COUNT(*) AS total
        FROM messages
        WHERE user_id = %s
    """, (user_id,))

    # ================= USER STREAK =================
    try:
        cursor.execute("""
            SELECT streak
            FROM users
            WHERE id = %s
        """, (user_id,))

        streak_data = cursor.fetchone()

        streak = streak_data.get('streak', 0) if streak_data else 0

    except:
        streak = 0

    # ================= USER RATING =================
    try:
        cursor.execute("""
            SELECT rating
            FROM users
            WHERE id = %s
        """, (user_id,))

        rating_data = cursor.fetchone()

        rating = rating_data.get('rating', 4.9) if rating_data else 4.9

    except:
        rating = 4.9

    conn.close()

    # ================= RENDER =================
    return render_template(
        'profile.html',
        user=user,
        total_chats=total_chats,
        total_messages=total_messages,
        streak=streak,
        rating=rating
    )
@app.route('/edit_profile', methods=['GET', 'POST'])
def edit_profile():

    # ================= LOGIN CHECK =================
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # =====================================================
    # UPDATE PROFILE
    # =====================================================
    if request.method == 'POST':

        username = request.form.get('username')
        email = request.form.get('email')
        bio = request.form.get('bio')

        # NEW FIELDS
        phone = request.form.get('phone')
        dob = request.form.get('dob')
        country = request.form.get('country')
        gender = request.form.get('gender')
        portfolio_link = request.form.get('portfolio_link')

        profile_image = request.files.get('profile_image')

        # =====================================================
        # IMAGE UPLOAD
        # =====================================================
        if profile_image and profile_image.filename != '':

            ext = profile_image.filename.rsplit('.', 1)[-1]
            image_name = f"{uuid.uuid4()}.{ext}"

            save_path = os.path.join(
                'static/profile_images',
                image_name
            )

            profile_image.save(save_path)

            cursor.execute("""
                UPDATE users
                SET
                    username=%s,
                    email=%s,
                    bio=%s,
                    phone=%s,
                    dob=%s,
                    country=%s,
                    gender=%s,
                    portfolio_link=%s,
                    profile_image=%s
                WHERE id=%s
            """, (
                username,
                email,
                bio,
                phone,
                dob,
                country,
                gender,
                portfolio_link,
                image_name,
                user_id
            ))

        # =====================================================
        # WITHOUT IMAGE
        # =====================================================
        else:

            cursor.execute("""
                UPDATE users
                SET
                    username=%s,
                    email=%s,
                    bio=%s,
                    phone=%s,
                    dob=%s,
                    country=%s,
                    gender=%s,
                    portfolio_link=%s
                WHERE id=%s
            """, (
                username,
                email,
                bio,
                phone,
                dob,
                country,
                gender,
                portfolio_link,
                user_id
            ))

        conn.commit()
        conn.close()

        return redirect(url_for('profile'))

    # =====================================================
    # GET USER DATA
    # =====================================================
    cursor.execute("""
        SELECT *
        FROM users
        WHERE id=%s
    """, (user_id,))

    user = cursor.fetchone()

    # =====================================================
    # STATS
    # =====================================================

    # TOTAL CHATS
    try:
        cursor.execute("""
            SELECT COUNT(*) AS total_chats
            FROM chats
            WHERE user_id=%s
        """, (user_id,))

        chats = cursor.fetchone()['total_chats']

    except:
        chats = 0

    # TOTAL MESSAGES
    try:
        cursor.execute("""
            SELECT COUNT(*) AS total_messages
            FROM messages
            WHERE user_id=%s
        """, (user_id,))

        messages = cursor.fetchone()['total_messages']

    except:
        messages = 0

    # SESSIONS
    try:
        cursor.execute("""
            SELECT COUNT(*) AS sessions
            FROM sessions
            WHERE user_id=%s
        """, (user_id,))

        sessions = cursor.fetchone()['sessions']

    except:
        sessions = 0

    # RATING
    rating = user.get('rating', 4.9)

    # STREAK
    streak = user.get('streak', 0)

    conn.close()

    # =====================================================
    # RENDER
    # =====================================================
    return render_template(
        'edit_profile.html',
        user=user,
        chats=chats,
        messages=messages,
        sessions=sessions,
        rating=rating,
        streak=streak
    )
@app.route('/settings', methods=['GET', 'POST'])
def settings():

    # =========================
    # LOGIN CHECK
    # =========================
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor()

    # =========================
    # SAVE SETTINGS
    # =========================
    if request.method == 'POST':

        # ---------- Appearance ----------
        dark_mode = True if request.form.get('dark_mode') else False
        theme_color = request.form.get('theme_color')

        dark_mode_db = "ON" if dark_mode else "OFF"

        # if dark mode OFF → force blue theme
        if not dark_mode:
            theme_color = "Blue Theme"

        # ---------- AI Settings ----------
        ai_model = request.form.get('ai_model')
        ai_personality = request.form.get('ai_personality')

        # ---------- Notifications ----------
        email_notifications = request.form.get('email_notifications')
        chat_alerts = request.form.get('chat_alerts')
        marketing_emails = request.form.get('marketing_emails')

        # ---------- Language ----------
        language = request.form.get('language')
        session['language'] = language
        # ---------- Voice ----------
        voice_assistant = request.form.get('voice_assistant')
        microphone_access = request.form.get('microphone_access')

        # =========================
        # UPDATE DATABASE
        # =========================
        cursor.execute("""
            UPDATE users
            SET dark_mode=%s,
                theme_color=%s,
                ai_model=%s,
                ai_personality=%s,
                email_notifications=%s,
                chat_alerts=%s,
                marketing_emails=%s,
                language=%s,
                voice_assistant=%s,
                microphone_access=%s
            WHERE id=%s
        """, (
            dark_mode_db,
            theme_color,
            ai_model,
            ai_personality,
            email_notifications,
            chat_alerts,
            marketing_emails,
            language,
            voice_assistant,
            microphone_access,
            user_id
        ))

        conn.commit()

        flash("Settings Saved Successfully!", "success")
        return redirect(url_for('settings'))

    # =========================
    # FETCH USER DATA
    # =========================
    cursor.execute("""
        SELECT * FROM users WHERE id=%s
    """, (user_id,))

    user = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template('settings.html', user=user)
@app.route('/notifications')
def notifications():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = get_connection()
    cursor = conn.cursor()

    # ALL NOTIFICATIONS
    cursor.execute("""
        SELECT id, title, message, is_read, created_at
        FROM notifications
        WHERE user_id=%s
        ORDER BY created_at DESC
    """, (user_id,))
    notifications = cursor.fetchall()

    # STATS
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM notifications
        WHERE user_id=%s
    """, (user_id,))
    total = cursor.fetchone()['total']

    cursor.execute("""
        SELECT COUNT(*) AS unread
        FROM notifications
        WHERE user_id=%s AND is_read=0
    """, (user_id,))
    unread = cursor.fetchone()['unread']

    cursor.close()
    conn.close()

    return render_template(
        "notifications.html",
        notifications=notifications,
        total=total,
        unread=unread,
        user_id=user_id
    )
@app.route('/mark-read/<int:noti_id>')
def mark_read(noti_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE notifications
        SET is_read=1
        WHERE id=%s
    """, (noti_id,))

    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('notifications'))
# =====================================================
# AI MODELS PAGE
# =====================================================

@app.route('/ai_models')
def ai_models():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    cursor.execute("SELECT * FROM ai_models")
    models = cursor.fetchall()

    conn.close()

    return render_template('ai_models.html', models=models)
@app.route('/select_model/<model_name>')
def select_model(model_name):

    if 'user_id' not in session:
        return redirect(url_for('login'))

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE users
            SET selected_model = %s
            WHERE id = %s
        """, (model_name, session['user_id']))

        conn.commit()

    except Exception as e:
        print("Model selection error:", e)
        flash("Something went wrong while selecting model ❌")
        return redirect(url_for('ai_models'))

    finally:
        if conn:
            conn.close()

    # update session also (VERY IMPORTANT)
    session['selected_model'] = model_name

    flash(f"Model switched to {model_name} 🚀")
    return redirect(url_for('chat'))
# =====================================================
# AI PERSONALITY PAGE
# =====================================================

@app.route('/ai_personality')
def ai_personality():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM ai_personalities")

    personalities = cursor.fetchall()

    conn.close()

    return render_template(
        'ai_personality.html',
        personalities=personalities
    )

# =====================================================
# SENTIMENT ANALYSIS PAGE
# =====================================================

@app.route('/sentiment_analysis')
def sentiment_analysis():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor()

    # =========================
    # 1. SENTIMENT DATA
    # =========================
    cursor.execute("""
        SELECT sentiment, COUNT(*) as total
        FROM chats
        WHERE user_id=%s
        GROUP BY sentiment
    """, (session['user_id'],))

    sentiments = cursor.fetchall()

    # Convert tuple → dict style handling
    data = {
        "Positive": 0,
        "Neutral": 0,
        "Negative": 0
    }

    for row in sentiments:
        if row['sentiment'] in data:
            data[row['sentiment']] = row['total']

    total = sum(data.values())

    positive_pct = round((data["Positive"] / total) * 100, 1) if total else 0
    neutral_pct  = round((data["Neutral"] / total) * 100, 1) if total else 0
    negative_pct = round((data["Negative"] / total) * 100, 1) if total else 0


    # =========================
    # 2. USER DATA (FIX FOR YOUR ERROR)
    # =========================
    cursor.execute("""
        SELECT username, profile_image
        FROM users
        WHERE id=%s
    """, (session['user_id'],))

    user = cursor.fetchone()

    cursor.close()
    conn.close()


    # =========================
    # 3. RENDER TEMPLATE
    # =========================
    return render_template(
        'sentiment_analysis.html',

        user=user,   # ✅ FIXED (prevents UndefinedError)

        sentiments=sentiments,

        positive_pct=positive_pct,
        neutral_pct=neutral_pct,
        negative_pct=negative_pct,
        total_chats=total,

        chart_data=[
            data["Positive"],
            data["Neutral"],
            data["Negative"]
        ]
    )

# =====================================================
# FEEDBACK PAGE
# =====================================================

@app.route('/feedback', methods=['GET', 'POST'])
def feedback():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':

        feedback_message = request.form['feedback']
        rating = request.form['rating']

        conn = get_connection()
        cursor = conn.cursor()

        sql = """
        INSERT INTO feedback(
            user_id,
            feedback_message,
            rating
        )
        VALUES(%s, %s, %s)
        """

        cursor.execute(sql, (
            session['user_id'],
            feedback_message,
            rating
        ))

        conn.commit()
        conn.close()

        flash('Feedback Submitted')

    return render_template('feedback.html')

# =====================================================
# ADMIN DASHBOARD
# =====================================================

@app.route('/admin_dashboard')
def admin_dashboard():

    # 🔐 Security check (important)
    if 'role' not in session or session['role'] != 'admin':
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # ================= TOTAL USERS =================
    cursor.execute("SELECT COUNT(*) AS total_users FROM users")
    user_result = cursor.fetchone()
    total_users = user_result['total_users']

    # ================= TOTAL CHATS =================
    cursor.execute("SELECT COUNT(*) AS total_chats FROM chats")
    chat_result = cursor.fetchone()
    total_chats = chat_result['total_chats']

    # ================= RECENT USERS (optional but useful) =================
    cursor.execute("""
        SELECT id, username, email, role
        FROM users
        ORDER BY id DESC
        LIMIT 5
    """)
    recent_users = cursor.fetchall()

    # ================= RECENT ACTIVITY (optional) =================
    cursor.execute("""
        SELECT activity_type, description, created_at
        FROM activities
        ORDER BY created_at DESC
        LIMIT 5
    """)
    activities = cursor.fetchall()

    conn.close()

    return render_template(
        'admin_dashboard.html',
        total_users=total_users,
        total_chats=total_chats,
        recent_users=recent_users,
        activities=activities
    )
# =====================================================
# MANAGE USERS
# =====================================================

@app.route('/manage_users')
def manage_users():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users ORDER BY id DESC")

    users = cursor.fetchall()

    conn.close()

    return render_template(
        'manage_users.html',
        users=users
    )

# =====================================================
# MANAGE CHATS
# =====================================================

# =========================================================
# MANAGE CHATS ROUTE
# =========================================================

@app.route('/manage_chats')
def manage_chats():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""

        SELECT 
            users.id AS user_id,
            users.username,
            users.email,
            users.profile_image,
            users.created_at AS login_date,

            latest_chat.sentiment,
            latest_chat.created_at AS chat_date

        FROM users

        LEFT JOIN (

            SELECT c1.*
            FROM chats c1

            INNER JOIN (
                SELECT user_id, MAX(id) AS max_chat_id
                FROM chats
                GROUP BY user_id
            ) c2

            ON c1.id = c2.max_chat_id

        ) AS latest_chat

        ON users.id = latest_chat.user_id

        ORDER BY latest_chat.created_at DESC

    """)

    chats = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'manage_chats.html',
        chats=chats
    )

# =========================================================
# VIEW SINGLE USER CHAT
# =========================================================
@app.route('/view_user_chat/<int:user_id>')
def view_user_chat(user_id):

    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor()

    # USER DETAILS
    cursor.execute("""

        SELECT *
        FROM users
        WHERE id = %s

    """, (user_id,))

    user = cursor.fetchone()

    # ALL CHAT HISTORY OF USER
    cursor.execute("""

        SELECT *
        FROM chats
        WHERE user_id = %s
        ORDER BY created_at DESC

    """, (user_id,))

    user_chats = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'view_user_chat.html',
        user=user,
        user_chats=user_chats
    )
# =====================================================
# DELETE CHAT
# =====================================================

@app.route('/delete_chat/<int:chat_id>')
def delete_chat(chat_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM chats WHERE id=%s",
        (chat_id,)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('chat_history'))

# =====================================================
# DELETE USER
# =====================================================

@app.route('/delete_user/<int:user_id>')
def delete_user(user_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM users WHERE id=%s",
        (user_id,)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('manage_users'))

# =====================================================
# SEARCH CHAT
# =====================================================
@app.route('/search_chat', methods=['POST'])
def search_chat():

    keyword = request.form.get('keyword', '')

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:

        sql = """
        SELECT *
        FROM chat_history
        WHERE LOWER(message) LIKE LOWER(%s)
           OR LOWER(response) LIKE LOWER(%s)
        ORDER BY id DESC
        """

        search = f"%{keyword}%"

        cursor.execute(sql, (search, search))

        chats = cursor.fetchall()

        return jsonify({
            "status": "success",
            "chats": chats
        })

    except Exception as e:

        print("Search error:", e)

        return jsonify({
            "status": "error",
            "message": str(e)
        })

    finally:
        cursor.close()
        conn.close()
# =====================================================
# ANALYTICS PAGE
# =====================================================
@app.route('/analytics')
def analytics():

    conn = get_connection()
    cursor = conn.cursor()

    # =========================
    # DATE FILTER
    # =========================
    start_date = request.args.get('start')
    end_date = request.args.get('end')

    date_filter = ""
    params = []

    if start_date and end_date:
        date_filter = "WHERE DATE(created_at) BETWEEN %s AND %s"
        params = [start_date, end_date]

    # =========================
    # DAILY ACTIVITY
    # =========================
    cursor.execute(f"""
        SELECT DATE(created_at) as date,
               COUNT(*) as total
        FROM chats
        {date_filter}
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """, params)

    daily_data = cursor.fetchall()

    # =========================
    # SENTIMENT
    # =========================
    cursor.execute(f"""
        SELECT sentiment, COUNT(*) as total
        FROM chats
        {date_filter}
        GROUP BY sentiment
    """, params)

    sentiment_rows = cursor.fetchall()

    sentiment_data = {"Positive": 0, "Neutral": 0, "Negative": 0}

    for r in sentiment_rows:
        if r['sentiment'] in sentiment_data:
            sentiment_data[r['sentiment']] = r['total']

    # =========================
    # TOP USERS (FIXED)
    # =========================
    cursor.execute(f"""
        SELECT u.id, u.username,
               COUNT(c.id) as total_chats
        FROM chats c
        JOIN users u ON u.id = c.user_id
        {"WHERE DATE(c.created_at) BETWEEN %s AND %s" if start_date and end_date else ""}
        GROUP BY u.id, u.username
        ORDER BY total_chats DESC
        LIMIT 5
    """, params)

    top_users = cursor.fetchall()

    # =========================
    # HEATMAP
    # =========================
    cursor.execute(f"""
        SELECT HOUR(created_at) as hour,
               COUNT(*) as total
        FROM chats
        {date_filter}
        GROUP BY hour
        ORDER BY hour ASC
    """, params)

    heatmap = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "analytics.html",
        daily_data=daily_data,
        sentiment_data=sentiment_data,
        top_users=top_users,
        heatmap=heatmap,
        start_date=start_date,
        end_date=end_date
    )
# =====================================================
# ERROR HANDLERS
# =====================================================

@app.errorhandler(404)
def not_found(error):
    return render_template('error.html')


# ================= IMAGE ANALYSIS PAGE =================
# ============================================================
# IMAGE ANALYSIS ROUTE
# ============================================================

@app.route('/image_analysis', methods=['GET', 'POST'])
def image_analysis():

    # ============================================================
    # LOGIN CHECK
    # ============================================================

    if 'user_id' not in session:
        return redirect(url_for('login'))

    # ============================================================
    # LOAD PAGE
    # ============================================================

    if request.method == 'GET':

        # RECENT ANALYSIS HISTORY
        history = []

        try:

            conn = get_connection()
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            cursor.execute("""
                SELECT *
                FROM image_analysis_history
                WHERE user_id=%s
                ORDER BY id DESC
                LIMIT 12
            """, (session['user_id'],))

            history = cursor.fetchall()

            cursor.close()
            conn.close()

        except Exception as e:

            print("HISTORY ERROR:", e)

        return render_template(
            'image_analysis.html',
            history=history
        )

    try:

        # ============================================================
        # GET ACTION
        # ============================================================

        action = request.form.get('action')

        # ============================================================
        # IMAGE DESCRIPTION FEATURE
        # ============================================================

        if action == "describe":

            image = request.files.get('image')

            if not image:

                return jsonify({
                    'success': False,
                    'error': 'Please upload image'
                })

            # ============================================================
            # SAVE IMAGE
            # ============================================================

            filename = secure_filename(image.filename)

            unique_name = f"{uuid.uuid4()}_{filename}"

            filepath = os.path.join(
                app.config['UPLOAD_FOLDER'],
                unique_name
            )

            image.save(filepath)

            # ============================================================
            # CONVERT IMAGE TO BASE64
            # ============================================================

            with open(filepath, "rb") as img_file:

                image_base64 = base64.b64encode(
                    img_file.read()
                ).decode('utf-8')

            # ============================================================
            # OPENROUTER PAYLOAD
            # ============================================================

            payload = {

                "model": "openai/gpt-4o-mini",

                "messages": [

                    {
                        "role": "user",

                        "content": [

                            {
                                "type": "text",

                                "text": """
                                Analyze this image naturally like a real human.

                                Explain clearly:

                                1. What is visible in image
                                2. Person appearance
                                3. Clothes and colors
                                4. Background details
                                5. Mood and emotions
                                6. Lighting and environment
                                7. Important objects
                                8. Full detailed explanation

                                Talk naturally and professionally.
                                """
                            },

                            {
                                "type": "image_url",

                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }

                            }

                        ]

                    }

                ],

                "max_tokens": 800

            }

            # ============================================================
            # API REQUEST
            # ============================================================

            response = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload
            )

            result = response.json()

            print("IMAGE ANALYSIS RESULT:")
            print(result)

            # ============================================================
            # ERROR CHECK
            # ============================================================

            if 'choices' not in result:

                error_message = result.get(
                    'error',
                    {}
                ).get(
                    'message',
                    'AI analysis failed'
                )

                return jsonify({
                    'success': False,
                    'error': error_message
                })

            # ============================================================
            # AI RESPONSE
            # ============================================================

            ai_result = result['choices'][0]['message']['content']

            # ============================================================
            # SAVE HISTORY IN DATABASE
            # ============================================================

            try:

                conn = get_connection()
                cursor = conn.cursor()

                cursor.execute("""

                    INSERT INTO image_analysis_history
                    (
                        user_id,
                        image_path,
                        analysis_result,
                        created_at
                    )

                    VALUES (%s,%s,%s,NOW())

                """, (

                    session['user_id'],
                    filepath,
                    ai_result

                ))

                conn.commit()

                cursor.close()
                conn.close()

            except Exception as db_error:

                print("DATABASE SAVE ERROR:", db_error)

            # ============================================================
            # RETURN RESPONSE
            # ============================================================

            return jsonify({

                'success': True,
                'result': ai_result,
                'image_path': filepath

            })

        # ============================================================
        # IMAGE GENERATION FEATURE
        # ============================================================

        elif action == "generate":

            prompt = request.form.get('prompt')

            if not prompt:

                return jsonify({
                    'success': False,
                    'error': 'Please enter prompt'
                })

            # ============================================================
            # IMAGE GENERATION PAYLOAD
            # ============================================================

            payload = {

                "model": "openai/dall-e-3",

                "prompt": prompt,

                "n": 1,

                "size": "1024x1024"

            }

            # ============================================================
            # API REQUEST
            # ============================================================

            response = requests.post(
                "https://openrouter.ai/api/v1/images/generations",
                headers=headers,
                json=payload
            )

            result = response.json()

            print("IMAGE GENERATION RESULT:")
            print(result)

            # ============================================================
            # ERROR CHECK
            # ============================================================

            if 'data' not in result:

                error_message = result.get(
                    'error',
                    {}
                ).get(
                    'message',
                    'Image generation failed'
                )

                return jsonify({
                    'success': False,
                    'error': error_message
                })

            generated_image = result['data'][0]['url']

            return jsonify({

                'success': True,
                'image_url': generated_image

            })

        # ============================================================
        # INVALID ACTION
        # ============================================================

        else:

            return jsonify({
                'success': False,
                'error': 'Invalid action'
            })

    except Exception as e:

        print("IMAGE AI ERROR:", str(e))

        return jsonify({

            'success': False,
            'error': str(e)

        })
# ================= DOCUMENT UPLOAD PAGE =================

# =====================================================
# DOCUMENT ANALYSIS PAGE
# =====================================================

@app.route('/document_upload')
def document_upload():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    return render_template('document_upload.html')


# =====================================================
# DOCUMENT ANALYSIS API
# =====================================================

@app.route('/upload_document', methods=['POST'])
def upload_document():

    try:

        # =================================================
        # LOGIN CHECK
        # =================================================

        if 'user_id' not in session:

            return jsonify({
                "success": False,
                "error": "Please login first"
            })


        # =================================================
        # FILE CHECK
        # =================================================

        if 'document' not in request.files:

            return jsonify({
                "success": False,
                "error": "No document uploaded"
            })


        file = request.files['document']


        if file.filename == '':

            return jsonify({
                "success": False,
                "error": "No file selected"
            })


        # =================================================
        # SAFE FILE NAME
        # =================================================

        filename = secure_filename(file.filename)

        unique_name = f"{uuid.uuid4()}_{filename}"

        filepath = os.path.join(
            app.config['DOCUMENT_FOLDER'],
            unique_name
        )

        file.save(filepath)


        # =================================================
        # EXTRACT TEXT
        # =================================================

        extracted_text = ""


        # PDF FILE
        if filename.lower().endswith('.pdf'):

            with open(filepath, 'rb') as pdf_file:

                pdf_reader = PyPDF2.PdfReader(pdf_file)

                for page in pdf_reader.pages:

                    text = page.extract_text()

                    if text:
                        extracted_text += text + "\n"


        # TXT FILE
        elif filename.lower().endswith('.txt'):

            with open(filepath, 'r', encoding='utf-8') as txt_file:

                extracted_text = txt_file.read()


        # DOCX FILE
        elif filename.lower().endswith('.docx'):

            document = docx.Document(filepath)

            for para in document.paragraphs:

                extracted_text += para.text + "\n"


        else:

            return jsonify({
                "success": False,
                "error": "Unsupported file format"
            })


        # =================================================
        # EMPTY TEXT CHECK
        # =================================================

        if extracted_text.strip() == "":

            return jsonify({
                "success": False,
                "error": "Could not read document content"
            })


        # =================================================
        # LIMIT HUGE DOCUMENTS
        # =================================================

        extracted_text = extracted_text[:10000]


        # =================================================
        # AI PROMPT
        # =================================================

        prompt = f"""
You are an advanced AI document analyzer.

Analyze the uploaded document professionally.

Explain clearly:

1. Main Topic
2. Important Points
3. Detailed Summary
4. Key Insights
5. Final Conclusion

Keep response very clean, readable and professional.

DOCUMENT CONTENT:

{extracted_text}
"""


        # =================================================
        # OPENROUTER PAYLOAD
        # =================================================

        payload = {

            "model": "deepseek/deepseek-chat-v3-0324",

            "messages": [

                {
                    "role": "system",
                    "content": "You are a professional AI document analyzer."
                },

                {
                    "role": "user",
                    "content": prompt
                }

            ],

            "max_tokens": 1200,

            "temperature": 0.7

        }


        # =================================================
        # API REQUEST
        # =================================================

        response = requests.post(

            OPENROUTER_URL,

            headers=headers,

            json=payload

        )


        result = response.json()


        # DEBUG RESPONSE
        print(result)


        # =================================================
        # ERROR HANDLING
        # =================================================

        if 'choices' not in result:

            error_message = result.get(
                'error',
                {}
            ).get(
                'message',
                'AI analysis failed'
            )

            return jsonify({

                "success": False,

                "error": error_message

            })


        # =================================================
        # AI RESPONSE
        # =================================================

        analysis = result['choices'][0]['message']['content']


        # =================================================
        # SUCCESS RESPONSE
        # =================================================

        return jsonify({

            "success": True,

            "filename": filename,

            "analysis": analysis

        })


    except Exception as e:

        print("DOCUMENT ANALYSIS ERROR:", e)

        return jsonify({

            "success": False,

            "error": str(e)

        })

# ================= VOICE CHAT PAGE =================

@app.route('/voice_chat')
def voice_chat():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    return render_template('voice_chat.html')

# =========================
# FORGOT PASSWORD ROUTE
# =========================

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():

    if request.method == 'POST':

        username = request.form.get('username')
        email = request.form.get('email')

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM users
            WHERE username=%s AND email=%s
        """, (username, email))

        user = cursor.fetchone()
        conn.close()

        if user:
            session['reset_user'] = user['id']
            return redirect(url_for('reset_password'))

        else:
            flash("Invalid username or email")

    return render_template('forgot_password.html')


# =========================
# RESET PASSWORD ROUTE
# =========================

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():

    user_id = session.get('reset_user')

    # ❌ if no session → go back
    if not user_id:
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':

        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # ❌ validation
        if not new_password or not confirm_password:
            return "All fields required!"

        if new_password != confirm_password:
            return "Passwords not matching!"

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE users
            SET password=%s
            WHERE id=%s
        """, (new_password, user_id))

        conn.commit()
        conn.close()

        # clear session
        session.pop('reset_user', None)

        # ✅ redirect to login
        return redirect(url_for('login'))

    return render_template('reset_password.html')


# ==========================================
# GLOBAL USER THEME SETTINGS
# ==========================================
# =========================================
# USER SETTINGS CONTEXT PROCESSOR
# =========================================
@app.context_processor
def inject_user_settings():

    default = {
        "user_theme": "Blue Theme",
        "dark_mode": False
    }

    # -----------------------------
    # USER NOT LOGGED IN
    # -----------------------------
    if 'user_id' not in session:
        return default

    conn = get_connection()

    # IMPORTANT
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    cursor.execute("""
        SELECT theme_color, dark_mode
        FROM users
        WHERE id=%s
    """, (session['user_id'],))

    user = cursor.fetchone()

    cursor.close()
    conn.close()

    # -----------------------------
    # RETURN USER SETTINGS
    # -----------------------------
    if user:

        return {
            "user_theme": user.get('theme_color', 'Blue Theme'),
            "dark_mode": True if user.get('dark_mode') == "ON" else False
        }

    return default


# =========================================
# TRANSLATOR CONTEXT PROCESSOR
# =========================================
@app.context_processor
def inject_translator():

    # DEFAULT LANGUAGE
    language = session.get('language', 'English')

    # GET LANGUAGE DATA
    lang_data = translations.get(
        language,
        translations['English']
    )

    # TRANSLATION FUNCTION
    def t(key):

        return lang_data.get(key, key)

    # SEND TO HTML
    return dict(
        t=t,
        current_language=language
    )


# =========================
# ABOUT PAGE (USER SIDE)
# =========================
@app.route('/about')
def about():

    conn = get_connection()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    # About page content (single row)
    cursor.execute("SELECT * FROM about LIMIT 1")
    about_data = cursor.fetchone()

    # Team members / Neural crew
    cursor.execute("SELECT * FROM people ORDER BY id DESC")
    people = cursor.fetchall()

    conn.close()

    return render_template(
        'about.html',
        about=about_data,
        people=people
    )

# =========================
# ADMIN PAGE
# =========================
@app.route('/admin_about')
def admin_about():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM about LIMIT 1")
    about = cursor.fetchone()

    cursor.execute("SELECT * FROM people ORDER BY id DESC")
    people = cursor.fetchall()

    conn.close()

    return render_template('admin_about.html', about=about, people=people)

# =========================
# SAVE ABOUT DESCRIPTION
# =========================
@app.route('/save_about', methods=['POST'])
def save_about():
    conn = get_connection()
    cursor = conn.cursor()

    desc = request.form['description']

    cursor.execute("SELECT * FROM about LIMIT 1")
    existing = cursor.fetchone()

    if existing:
        cursor.execute("UPDATE about SET description=%s WHERE id=%s",
                       (desc, existing['id']))
    else:
        cursor.execute("INSERT INTO about(description) VALUES(%s)", (desc,))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_about'))

# =========================
# ADD PERSON
# =========================
@app.route('/add_person', methods=['POST'])
def add_person():
    file = request.files['profile_image']
    filename = secure_filename(file.filename)
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    name = request.form['name']
    age = request.form['age']
    role = request.form['role']
    bio = request.form['bio']
    skills = request.form['skills']

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO people (profile_image, name, age, role, bio, skills)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (filename, name, age, role, bio, skills))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_about'))

# =========================
# DELETE PERSON
# =========================
@app.route('/delete_person/<int:id>')
def delete_person(id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM people WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return redirect(url_for('admin_about'))


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/support')
def support():
    return render_template('support.html')

@app.route('/features')
def features():
    return render_template('features.html')


@app.route('/team')
def team():

    conn = get_connection()

    cursor = conn.cursor()

    cursor.execute("SELECT * FROM team_members")

    members = cursor.fetchall()

    conn.close()

    return render_template(
        'team.html',
        members=members
    )


# ======================================================
# CONTACT ROUTE
# ======================================================

@app.route('/contact', methods=['GET', 'POST'])
def contact():

    if request.method == 'POST':

        fullname = request.form.get('fullname')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')

        try:

            msg = Message(
                subject=f"NOVA AI Contact • {subject}",
                sender=app.config['MAIL_USERNAME'],   # MUST BE SAME AS GMAIL
                recipients=['nikhildeshmuk98@gmail.com']
            )

            msg.body = f"""
NEW CONTACT MESSAGE - NOVA AI

Name    : {fullname}
Email   : {email}
Subject : {subject}

Message:
{message}
"""

            mail.send(msg)

            flash('Message sent successfully 🚀', 'success')
            return redirect(url_for('contact'))

        except Exception as e:
            print("MAIL ERROR:", e)
            flash('Something went wrong!', 'danger')
            return redirect(url_for('contact'))

    return render_template('contact.html')

# =========================================================
# COOKIES POLICY PAGE ROUTE
# =========================================================

@app.route('/cookies_policy')
def cookies_policy():

    # =========================
    # USER CHECK (OPTIONAL)
    # =========================
    user = None

    if 'user_id' in session:

        conn = get_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT *
            FROM users
            WHERE id=%s
        """, (session['user_id'],))

        user = cursor.fetchone()

        cursor.close()
        conn.close()

    # =========================
    # RENDER PAGE
    # =========================
    return render_template(
        'cookies_policy.html',
        user=user
    )



@app.route('/upload_people', methods=['GET', 'POST'])
def upload_people():

    if request.method == 'POST':

        profile = request.files['profile_image']

        filename = secure_filename(profile.filename)

        profile.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

        first_name = request.form['first_name']
        middle_name = request.form['middle_name']
        last_name = request.form['last_name']
        age = request.form['age']
        role_name = request.form['role_name']
        bio = request.form['bio']
        skills = request.form['skills']
        linkedin_url = request.form['linkedin_url']
        github_url = request.form['github_url']
        email = request.form['email']

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""

            INSERT INTO team_members(

                profile_image,
                first_name,
                middle_name,
                last_name,
                age,
                role_name,
                bio,
                skills,
                linkedin_url,
                github_url,
                email

            )

            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)

        """, (

            filename,
            first_name,
            middle_name,
            last_name,
            age,
            role_name,
            bio,
            skills,
            linkedin_url,
            github_url,
            email

        ))

        conn.commit()
        conn.close()

        return redirect(url_for('team'))

    return render_template('upload_people.html')

@app.route("/send-test-mail")
def send_test_mail():

    try:

        msg = Message(
            subject="Flask Mail Test 🚀",
            sender=app.config['MAIL_USERNAME'],
            recipients=['nikhildeshmuk98@gmail.com']
        )

        msg.body = "Hello Buddy! Flask mail working successfully 🚀"

        mail.send(msg)

        return "Mail Sent Successfully ✅"

    except Exception as e:
        return f"Error: {str(e)}"
    
@app.route("/test-mail")
def test_mail():
    try:
        msg = Message(
            subject="Test Mail 🚀",
            sender=app.config['MAIL_USERNAME'],
            recipients=[app.config['MAIL_USERNAME']]
        )

        msg.body = "Flask mail is working perfectly 🚀"

        mail.send(msg)

        return "Mail Sent Successfully ✅"

    except Exception as e:
        return str(e)
    
@app.route("/voice-command", methods=["POST"])
def voice_command():
    data = request.json
    command = data.get("command", "").lower()

    # open any website dynamically
    if "open" in command:
        site = command.replace("open", "").strip()

        sites = {
            "youtube": "https://youtube.com",
            "google": "https://google.com",
            "instagram": "https://instagram.com",
            "amazon": "https://amazon.in"
        }

        if site in sites:
            return jsonify({
                "action": "open",
                "url": sites[site],
                "message": f"Opening {site}"
            })

        return jsonify({
            "action": "open",
            "url": f"https://www.{site}.com",
            "message": f"Trying to open {site}"
        })

    # play music command
    elif "play" in command:
        song = command.replace("play", "").strip()

        return jsonify({
            "action": "play",
            "query": song,
            "message": f"Playing {song}"
        })

    return jsonify({
        "action": "none",
        "message": "Command not recognized"
    })


@app.route('/chat_stats', methods=['GET'])
def chat_stats():

    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        user_id = session['user_id']

        # -----------------------------
        # LAST 7 DAYS MESSAGES COUNT
        # -----------------------------
        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM chats
            WHERE user_id = %s
            AND created_at >= NOW() - INTERVAL 7 DAY
        """, (user_id,))

        week_data = cursor.fetchone()
        week_count = week_data['total'] if week_data else 0

        # -----------------------------
        # SENTIMENT ANALYSIS
        # -----------------------------
        cursor.execute("""
            SELECT sentiment, COUNT(*) AS count
            FROM chats
            WHERE user_id = %s
            GROUP BY sentiment
        """, (user_id,))

        rows = cursor.fetchall()

        total = sum(r['count'] for r in rows) if rows else 0
        positive = 0

        for r in rows:
            if r['sentiment'] == "Positive":
                positive = r['count']

        positive_percent = int((positive / total) * 100) if total > 0 else 0

        cursor.close()
        conn.close()

        return jsonify({
            "week_count": week_count,
            "positive_percent": positive_percent
        })

    except Exception as e:
        print("STATS ERROR:", e)
        return jsonify({"error": "Failed to load stats"}), 500
    
@app.route('/filter_chats')
def filter_chats():

    if 'user_id' not in session:
        return jsonify([])

    sentiment = request.args.get('sentiment', 'All')

    conn = get_connection()
    cursor = conn.cursor()

    if sentiment == "All":
        cursor.execute("""
            SELECT * FROM chats
            WHERE user_id=%s
            ORDER BY id DESC
        """, (session['user_id'],))
    else:
        cursor.execute("""
            SELECT * FROM chats
            WHERE user_id=%s AND sentiment=%s
            ORDER BY id DESC
        """, (session['user_id'], sentiment))

    chats = cursor.fetchall()
    conn.close()

    return jsonify(chats)

@app.route('/user_analytics_dashboard/<int:user_id>')
def user_analytics_dashboard(user_id):

    conn = get_connection()
    cursor = conn.cursor()

    # =========================================
    # FILTER VALUES
    # =========================================

    selected_date = request.args.get('date')
    selected_month = request.args.get('month')
    selected_year = request.args.get('year')

    # =========================================
    # USER DETAILS
    # =========================================

    cursor.execute("""
        SELECT *
        FROM users
        WHERE id=%s
    """, (user_id,))

    user = cursor.fetchone()

    # =========================================
    # DATE FILTER QUERY
    # =========================================

    where_clause = "WHERE user_id=%s"
    values = [user_id]

    # FILTER BY DATE

    if selected_date:

        where_clause += " AND DATE(created_at)=%s"
        values.append(selected_date)

    # FILTER BY MONTH

    elif selected_month:

        where_clause += " AND DATE_FORMAT(created_at, '%%Y-%%m')=%s"
        values.append(selected_month)

    # FILTER BY YEAR

    elif selected_year:

        where_clause += " AND YEAR(created_at)=%s"
        values.append(selected_year)

    # =========================================
    # TOTAL CHATS
    # =========================================

    total_query = f"""
        SELECT COUNT(*) AS total_chats
        FROM chats
        {where_clause}
    """

    cursor.execute(total_query, tuple(values))

    total_data = cursor.fetchone()

    total_chats = total_data['total_chats']

    # =========================================
    # POSITIVE
    # =========================================

    positive_query = f"""
        SELECT COUNT(*) AS total
        FROM chats
        {where_clause}
        AND sentiment='Positive'
    """

    cursor.execute(positive_query, tuple(values))

    positive = cursor.fetchone()['total']

    # =========================================
    # NEUTRAL
    # =========================================

    neutral_query = f"""
        SELECT COUNT(*) AS total
        FROM chats
        {where_clause}
        AND sentiment='Neutral'
    """

    cursor.execute(neutral_query, tuple(values))

    neutral = cursor.fetchone()['total']

    # =========================================
    # NEGATIVE
    # =========================================

    negative_query = f"""
        SELECT COUNT(*) AS total
        FROM chats
        {where_clause}
        AND sentiment='Negative'
    """

    cursor.execute(negative_query, tuple(values))

    negative = cursor.fetchone()['total']

    # =========================================
    # LAST 7 DAYS ANALYTICS
    # =========================================

    chart_query = f"""
        SELECT
            DATE(created_at) AS chat_day,
            COUNT(*) AS total
        FROM chats
        {where_clause}
        GROUP BY DATE(created_at)
        ORDER BY DATE(created_at) ASC
        LIMIT 7
    """

    cursor.execute(chart_query, tuple(values))

    chart_result = cursor.fetchall()

    chart_labels = []
    chart_values = []

    for row in chart_result:

        chart_labels.append(str(row['chat_day']))
        chart_values.append(row['total'])

    cursor.close()
    conn.close()

    # =========================================
    # PERCENTAGES
    # =========================================

    positive_pct = round((positive / total_chats) * 100, 1) if total_chats else 0

    neutral_pct = round((neutral / total_chats) * 100, 1) if total_chats else 0

    negative_pct = round((negative / total_chats) * 100, 1) if total_chats else 0

    # =========================================
    # RENDER TEMPLATE
    # =========================================

    return render_template(

        'user_analytics_dashboard.html',

        user=user,

        total_chats=total_chats,

        positive=positive,
        neutral=neutral,
        negative=negative,

        positive_pct=positive_pct,
        neutral_pct=neutral_pct,
        negative_pct=negative_pct,

        chart_labels=chart_labels,
        chart_values=chart_values,

        selected_date=selected_date,
        selected_month=selected_month,
        selected_year=selected_year
    )
# =====================================================
# RUN FLASK APPLICATION
# =====================================================

if __name__ == '__main__':

    app.run(
        debug=True,
        host='0.0.0.0',
        port=5000
    )