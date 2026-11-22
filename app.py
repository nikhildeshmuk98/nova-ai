
import os
import io
import uuid
import base64
import logging
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
import pymysql
import PyPDF2
import docx
from PIL import Image
from textblob import TextBlob
from dotenv import load_dotenv

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, make_response
)
from flask_session import Session
from flask_mail import Mail, Message
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from translations import translations


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

# IMPORTANT: Initialize logger BEFORE using logger.info/warning.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("nova_ai")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is missing. Add it to your .env file."
    )

app.config.update(
    SESSION_TYPE="filesystem",
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_FILE_DIR=os.path.join(BASE_DIR, "flask_session"),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    MAIL_SERVER="smtp.gmail.com",
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
)

os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

Session(app)
mail = Mail(app)


# =========================================================
# GOOGLE OAUTH CONFIGURATION
# =========================================================

oauth = OAuth(app)

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID", ""
).strip()

GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_CLIENT_SECRET", ""
).strip()

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid email profile"
        }
    )

    logger.info("Google OAuth configuration loaded.")

else:
    logger.warning(
        "Google OAuth credentials are missing. "
        "Google login will remain unavailable."
    )


# =========================================================
# FOLDERS AND UPLOAD CONFIGURATION
# =========================================================

PROFILE_FOLDER = os.path.join(
    BASE_DIR, "static", "profile_images"
)

DOCUMENT_FOLDER = os.path.join(
    BASE_DIR, "uploads", "documents"
)

GENERATED_FOLDER = os.path.join(
    BASE_DIR, "static", "generated_images"
)

for folder in (
    PROFILE_FOLDER,
    DOCUMENT_FOLDER,
    GENERATED_FOLDER
):
    os.makedirs(folder, exist_ok=True)

app.config["UPLOAD_FOLDER"] = PROFILE_FOLDER
app.config["DOCUMENT_FOLDER"] = DOCUMENT_FOLDER
app.config["GENERATED_FOLDER"] = GENERATED_FOLDER

ALLOWED_IMAGE_EXTENSIONS = {
    "png", "jpg", "jpeg", "webp"
}

ALLOWED_DOCUMENT_EXTENSIONS = {
    "pdf", "txt", "docx"
}


# =========================================================
# AI API CONFIGURATION
# =========================================================

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openai/gpt-4o-mini"
)

APP_URL = os.getenv(
    "APP_URL",
    "http://localhost:5000"
)

OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY", ""
).strip()

OPENAI_IMAGE_MODEL = os.getenv(
    "OPENAI_IMAGE_MODEL",
    "gpt-image-2.5-flare"
)


def openrouter_headers():
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not configured."
        )

    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": "NOVA AI",
    }


# =========================================================
# NOVA AI: IMAGE GENERATION
# =========================================================

IMAGE_MESSAGE_PREFIX = "NOVA_IMAGE::"


def is_image_generation_request(message):
    """Return True only for clear, explicit image-generation requests."""
    text = (message or "").strip().lower()
    if not text:
        return False

    # Avoid treating educational questions as generation commands.
    question_starters = (
        "how do i ", "how can i ", "how to ",
        "what is ", "what are ", "explain ", "teach me "
    )
    if text.startswith(question_starters):
        return False

    explicit_phrases = (
        "generate an image", "generate image", "create an image",
        "create image", "draw an image", "draw image",
        "make an image", "make image", "design an image",
        "design image", "generate a picture", "create a picture",
        "draw a picture", "make a picture", "generate artwork",
        "create artwork", "generate an illustration",
        "create an illustration", "make a wallpaper",
        "generate a wallpaper", "create a logo", "design a logo",
        "make a logo", "image of ", "picture of ",
        "illustration of ", "wallpaper of "
    )
    if any(phrase in text for phrase in explicit_phrases):
        return True

    # Natural imperative variants such as "Draw a futuristic robot".
    return bool(
        re.match(
            r"^(please\s+)?(generate|create|draw|make|design|render|illustrate)\s+",
            text
        )
        and any(word in text for word in (
            "image", "picture", "photo", "art", "artwork",
            "illustration", "wallpaper", "logo", "poster"
        ))
    )


def generate_image_asset(prompt):
    """Generate one PNG with OpenAI and save it under static/generated_images."""
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured on the server."
        )

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Please enter an image prompt.")
    if len(prompt) > 4000:
        raise ValueError("Image prompts must be 4,000 characters or fewer.")

    try:
        response = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": OPENAI_IMAGE_MODEL,
                "prompt": prompt,
                "size": "1024x1024",
                "quality": "medium",
                "output_format": "png",
                "n": 1
            },
            timeout=(20, 240)
        )
    except requests.RequestException as exc:
        logger.exception("OpenAI image API request failed")
        raise RuntimeError(
            "Could not connect to the OpenAI image service. Please try again."
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Image provider returned invalid data (HTTP {response.status_code})."
        ) from exc

    if not response.ok:
        error_obj = payload.get("error") or {}
        provider_message = str(
            error_obj.get("message") or "Image generation failed."
        )
        logger.error(
            "OpenAI image API returned HTTP %s: %s",
            response.status_code,
            provider_message[:300]
        )
        raise RuntimeError(provider_message[:500])

    items = payload.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise RuntimeError("OpenAI did not return an image.")

    encoded = items[0].get("b64_json")
    if not encoded:
        # GPT Image API normally returns base64 image data.
        raise RuntimeError("OpenAI returned no image data. Please retry.")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("The generated image data was invalid.") from exc

    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("The generated file was not a valid PNG image.")

    # Defensive cap: avoid unexpectedly writing very large provider responses.
    if len(image_bytes) > 20 * 1024 * 1024:
        raise RuntimeError("The generated image exceeded the 20 MB limit.")

    filename = f"nova_{uuid.uuid4().hex}.png"
    filepath = os.path.join(GENERATED_FOLDER, filename)
    try:
        with open(filepath, "xb") as image_file:
            image_file.write(image_bytes)
    except OSError as exc:
        logger.exception("Could not save generated image")
        raise RuntimeError("The generated image could not be saved on the server.") from exc

    return url_for(
        "static",
        filename=f"generated_images/{filename}"
    )


def decode_image_message(content):
    """Decode our stored image marker without changing ordinary chat content."""
    if not isinstance(content, str) or not content.startswith(IMAGE_MESSAGE_PREFIX):
        return None
    try:
        value = json.loads(content[len(IMAGE_MESSAGE_PREFIX):])
        if not isinstance(value, dict):
            return None
        return value
    except (ValueError, TypeError):
        logger.warning("Found an invalid stored NOVA image message")
        return None


# =========================================================
# DATABASE CONFIGURATION
# =========================================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ai_chatbot_db")
DB_PORT = int(os.getenv("DB_PORT", "3306"))

DB_SSL = (
    os.getenv("DB_SSL", "false").lower() == "true"
)


def get_connection():
    options = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
        "charset": "utf8mb4",
        "autocommit": False,
    }

    if DB_SSL:
        options["ssl"] = {}

    return pymysql.connect(**options)


# =========================================================
# COMMON HELPER FUNCTIONS
# =========================================================

def login_required_page():
    if (
        "user_id" not in session
        or session.get("user_id") == "admin"
    ):
        return redirect(url_for("login"))

    return None


def current_user_id():
    user_id = session.get("user_id")

    if user_id is None or user_id == "admin":
        return None

    try:
        return int(user_id)

    except (TypeError, ValueError):
        return None


def allowed_file(filename, extensions):
    return bool(
        filename
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in extensions
    )


def sentiment_for(text):
    try:
        polarity = TextBlob(text).sentiment.polarity

        if polarity > 0:
            return "Positive"

        if polarity < 0:
            return "Negative"

        return "Neutral"

    except Exception:
        logger.exception("Sentiment analysis failed")
        return "Neutral"


# =========================================================
# OPENROUTER API FUNCTION
# =========================================================

def call_openrouter(
    messages,
    model=None,
    max_tokens=1200,
    temperature=0.7,
    timeout=60
):
    payload = {
        "model": model or OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    response = requests.post(
        OPENROUTER_URL,
        headers=openrouter_headers(),
        json=payload,
        timeout=timeout
    )

    try:
        data = response.json()

    except ValueError:
        raise RuntimeError(
            f"AI provider returned invalid JSON "
            f"(HTTP {response.status_code})."
        )

    if not response.ok:
        error_data = data.get("error") or {}

        message = error_data.get(
            "message",
            "AI provider request failed"
        )

        raise RuntimeError(
            f"AI provider error: {message}"
        )

    choices = data.get("choices") or []

    if not choices:
        raise RuntimeError(
            data.get("error", {}).get(
                "message",
                "AI response was empty"
            )
        )

    content = (
        choices[0]
        .get("message", {})
        .get("content", "")
    )

    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )

    if not content:
        raise RuntimeError(
            "AI returned an empty response."
        )

    return content, data


# =========================================================
# DATABASE: PERSISTENT CONVERSATION MEMORY
# =========================================================

def initialize_memory_tables():
    """
    Creates only the two new conversation memory tables.
    Existing application tables and their data are untouched.
    """

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    user_id INT(11) NOT NULL,
                    title VARCHAR(255) NOT NULL
                        DEFAULT 'New conversation',
                    created_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_conversations_user_updated
                        (user_id, updated_at)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    conversation_id VARCHAR(36) NOT NULL,
                    user_id INT(11) NOT NULL,
                    role ENUM(
                        'user',
                        'assistant',
                        'system'
                    ) NOT NULL,
                    content LONGTEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_messages_conversation
                        (conversation_id, id),
                    INDEX idx_messages_user
                        (user_id, id)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
            """)

        conn.commit()

        logger.info(
            "Conversation memory tables initialized."
        )

    except Exception:
        conn.rollback()

        logger.exception(
            "Could not initialize conversation memory tables"
        )

        raise

    finally:
        conn.close()


@app.before_request
def ensure_memory_tables_once():
    if not app.config.get("_memory_tables_ready"):

        initialize_memory_tables()

        app.config["_memory_tables_ready"] = True


def create_conversation(user_id, first_message=""):

    conversation_id = str(uuid.uuid4())

    title = (
        first_message.strip()
        .replace("\n", " ")[:70]
        or "New conversation"
    )

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO conversations
                    (id, user_id, title)
                VALUES (%s, %s, %s)
                """,
                (
                    conversation_id,
                    user_id,
                    title
                )
            )

        conn.commit()

        return conversation_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def conversation_belongs_to_user(
    conversation_id,
    user_id
):

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM conversations
                WHERE id=%s AND user_id=%s
                """,
                (
                    conversation_id,
                    user_id
                )
            )

            return cur.fetchone() is not None

    finally:
        conn.close()


def save_conversation_message(
    conversation_id,
    user_id,
    role,
    content
):

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO conversation_messages
                    (conversation_id, user_id, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    conversation_id,
                    user_id,
                    role,
                    content
                )
            )

            cur.execute(
                """
                UPDATE conversations
                SET updated_at=NOW()
                WHERE id=%s AND user_id=%s
                """,
                (
                    conversation_id,
                    user_id
                )
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def load_conversation_context(
    conversation_id,
    user_id,
    limit=20
):

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT role, content
                FROM conversation_messages
                WHERE conversation_id=%s
                  AND user_id=%s
                ORDER BY id DESC
                LIMIT %s
                """,
                (
                    conversation_id,
                    user_id,
                    limit
                )
            )

            rows = list(reversed(cur.fetchall()))

        # Convert stored image records into readable context for follow-up prompts.
        for row in rows:
            image_info = decode_image_message(row.get("content", ""))
            if image_info:
                prompt_text = str(image_info.get("prompt") or "")
                row["content"] = (
                    "The assistant generated an image for this prompt: "
                    + prompt_text
                )

        return rows

    finally:
        conn.close()


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
def index():

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT COUNT(*) AS total_users FROM users"
            )

            total_users = cur.fetchone()["total_users"]

            try:
                cur.execute(
                    "SELECT COUNT(*) AS total_chats FROM chat_history"
                )

                total_chats = cur.fetchone()["total_chats"]

            except pymysql.MySQLError:

                cur.execute(
                    "SELECT COUNT(*) AS total_chats FROM chats"
                )

                total_chats = cur.fetchone()["total_chats"]

        return render_template(
            "index.html",
            total_users=total_users,
            total_chats=total_chats,
            ai_accuracy=99,
            ai_availability="24/7"
        )

    finally:
        conn.close()


# =========================================================
# GOOGLE LOGIN / GOOGLE REGISTRATION
# =========================================================

@app.route("/google/login")
def google_login():

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:

        flash(
            "Google Login is not configured. "
            "Please use normal login.",
            "warning"
        )

        return redirect(url_for("login"))

    try:

        redirect_uri = url_for(
            "google_callback",
            _external=True
        )

        return oauth.google.authorize_redirect(
            redirect_uri
        )

    except Exception:

        logger.exception(
            "Could not start Google authentication"
        )

        flash(
            "Unable to connect to Google. Please try again.",
            "danger"
        )

        return redirect(url_for("login"))


@app.route("/google/callback")
def google_callback():

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:

        flash(
            "Google authentication is not configured.",
            "danger"
        )

        return redirect(url_for("login"))

    conn = None

    try:

        token = oauth.google.authorize_access_token()

        google_user = token.get("userinfo")

        if not google_user:
            google_user = oauth.google.userinfo()

        if not google_user:

            flash(
                "Unable to retrieve your Google account information.",
                "danger"
            )

            return redirect(url_for("login"))

        google_id = google_user.get("sub")

        email = (
            google_user.get("email") or ""
        ).strip().lower()

        username = (
            google_user.get("name")
            or email.split("@")[0]
        ).strip()

        email_verified = google_user.get(
            "email_verified"
        )

        if not google_id or not email:

            flash(
                "Google account information is incomplete.",
                "danger"
            )

            return redirect(url_for("login"))

        if email_verified not in (
            True, "true", "True", 1
        ):

            flash(
                "Please use a verified Google email address.",
                "warning"
            )

            return redirect(url_for("login"))

        conn = get_connection()

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM users
                WHERE email=%s
                LIMIT 1
                """,
                (email,)
            )

            user = cur.fetchone()

            if user:

                if user.get("status") == "blocked":

                    flash(
                        "Your account is blocked. "
                        "Please contact support.",
                        "danger"
                    )

                    return redirect(url_for("login"))

            else:

                random_password = (
                    uuid.uuid4().hex
                    + uuid.uuid4().hex
                )

                hashed_password = generate_password_hash(
                    random_password
                )

                cur.execute(
                    """
                    INSERT INTO users
                        (username, email, password)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        username,
                        email,
                        hashed_password
                    )
                )

                conn.commit()

                cur.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE email=%s
                    LIMIT 1
                    """,
                    (email,)
                )

                user = cur.fetchone()

        if not user:

            flash(
                "Unable to create or retrieve your NOVA AI account.",
                "danger"
            )

            return redirect(url_for("register"))

        try:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE users
                    SET last_login=NOW()
                    WHERE id=%s
                    """,
                    (user["id"],)
                )

            conn.commit()

        except pymysql.MySQLError:

            conn.rollback()

            logger.warning(
                "Could not update last_login. "
                "Continuing Google authentication."
            )

        session.clear()

        session.update({
            "user_id": user["id"],
            "username": user["username"],
            "role": user.get("role") or "user",
            "auth_provider": "google"
        })

        flash(
            "Google Sign-In successful! "
            "Welcome to NOVA AI 🚀",
            "success"
        )

        return redirect(url_for("dashboard"))

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "Google authentication failed"
        )

        flash(
            "Google Sign-In failed. Please try again.",
            "danger"
        )

        return redirect(url_for("login"))

    finally:

        if conn:
            conn.close()


# =========================================================
# NORMAL REGISTRATION
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form.get(
            "username", ""
        ).strip()

        email = request.form.get(
            "email", ""
        ).strip().lower()

        password = request.form.get(
            "password", ""
        )

        if (
            not username
            or not email
            or len(password) < 8
        ):

            flash(
                "Enter username, valid email and "
                "a password of at least 8 characters."
            )

            return redirect(url_for("register"))

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO users
                        (username, email, password)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password)
                    )
                )

            conn.commit()

            flash("Registration Successful")

            return redirect(url_for("login"))

        except pymysql.err.IntegrityError:

            conn.rollback()

            flash(
                "That email address is already registered."
            )

            return redirect(url_for("register"))

        finally:
            conn.close()

    return render_template("register.html")


# =========================================================
# NORMAL LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":

        return render_template(
            "login.html",
            remembered_email=request.cookies.get(
                "remember_email"
            )
        )

    email = request.form.get(
        "email", ""
    ).strip().lower()

    password = request.form.get(
        "password", ""
    )

    remember = request.form.get("remember")

    admin_email = os.getenv(
        "ADMIN_EMAIL",
        "admin@gmail.com"
    )

    admin_password = os.getenv(
        "ADMIN_PASSWORD",
        ""
    )

    if (
        admin_password
        and email == admin_email
        and password == admin_password
    ):

        session.clear()

        session.update({
            "user_id": "admin",
            "username": "Admin",
            "role": "admin"
        })

        response = make_response(
            redirect(url_for("admin_dashboard"))
        )

    else:

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    "SELECT * FROM users WHERE email=%s",
                    (email,)
                )

                user = cur.fetchone()

            if (
                not user
                or not check_password_hash(
                    user["password"],
                    password
                )
            ):

                flash("Invalid Email or Password")

                return redirect(url_for("login"))

            if user.get("status") == "blocked":

                flash(
                    "Your account is blocked. "
                    "Please contact support."
                )

                return redirect(url_for("login"))

            session.clear()

            session.update({
                "user_id": user["id"],
                "username": user["username"],
                "role": user.get("role") or "user"
            })

            try:

                with conn.cursor() as cur:

                    cur.execute(
                        """
                        UPDATE users
                        SET last_login=NOW()
                        WHERE id=%s
                        """,
                        (user["id"],)
                    )

                conn.commit()

            except pymysql.MySQLError:

                conn.rollback()

                logger.warning(
                    "Could not update last_login."
                )

            response = make_response(
                redirect(url_for("dashboard"))
            )

        finally:
            conn.close()

    if remember:

        response.set_cookie(
            "remember_email",
            email,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="Lax"
        )

    else:

        response.delete_cookie("remember_email")

    return response


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("index"))

# =========================================================
# DASHBOARD AND CHAT
# =========================================================

@app.route("/dashboard")
def dashboard():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS total_chats
                FROM chats
                WHERE user_id=%s
                """,
                (user_id,)
            )

            total_chats = cur.fetchone()["total_chats"]

            cur.execute(
                """
                SELECT username, email, profile_image
                FROM users
                WHERE id=%s
                """,
                (user_id,)
            )

            user = cur.fetchone()

        return render_template(
            "dashboard.html",
            total_chats=total_chats,
            user=user
        )

    finally:
        conn.close()


@app.route("/chat")
def chat():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )

            user = cur.fetchone()

        if not user:

            session.clear()

            return redirect(url_for("login"))

        user["selected_model"] = (
            user.get("selected_model")
            or OPENROUTER_MODEL
        )

        return render_template(
            "chat.html",
            user=user
        )

    finally:
        conn.close()


# =========================================================
# MAIN AI RESPONSE API
# =========================================================

@app.route("/get_ai_response", methods=["POST"])
def get_ai_response():

    user_id = current_user_id()

    if user_id is None:
        return jsonify(error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}

    user_message = str(
        data.get("message", "")
    ).strip()

    conversation_id = data.get("conversation_id")

    if not user_message:
        return jsonify(error="Message is empty"), 400

    if len(user_message) > 12000:
        return jsonify(
            error="Message is too long (maximum 12,000 characters)."
        ), 400

    lower_msg = user_message.lower()

    if lower_msg.startswith("open "):

        site_name = user_message[5:].strip()

        if not site_name:
            return jsonify(error="Enter a website name."), 400

        candidate = site_name.replace(" ", "")

        if candidate.startswith(("http://", "https://")):

            parsed = urlparse(candidate)

            if (
                parsed.scheme not in ("http", "https")
                or not parsed.netloc
            ):
                return jsonify(error="Invalid website URL"), 400

            open_url = candidate

        elif "." in candidate:

            open_url = "https://" + candidate

        else:

            safe_domain = "".join(
                ch for ch in candidate
                if ch.isalnum() or ch in "-_"
            )

            if not safe_domain:
                return jsonify(error="Invalid website name"), 400

            open_url = f"https://www.{safe_domain}.com"

        return jsonify(
            action="open_website",
            url=open_url,
            response=f"Opening {site_name.title()} 🚀"
        )

    sentiment = sentiment_for(user_message)

    conn = None

    try:

        if conversation_id:

            conversation_id = str(conversation_id)

            if (
                len(conversation_id) > 36
                or not conversation_belongs_to_user(
                    conversation_id,
                    user_id
                )
            ):

                return jsonify(
                    error="Conversation not found."
                ), 404

        else:

            conversation_id = create_conversation(
                user_id,
                user_message
            )

        save_conversation_message(
            conversation_id,
            user_id,
            "user",
            user_message
        )

        # Explicit image-generation requests use OpenAI Images API.
        # Normal chat requests continue through the existing OpenRouter flow.
        if is_image_generation_request(user_message):
            image_url = generate_image_asset(user_message)
            assistant_text = f"Generated image for: {user_message}"
            stored_image = IMAGE_MESSAGE_PREFIX + json.dumps(
                {
                    "text": assistant_text,
                    "prompt": user_message,
                    "image_url": image_url
                },
                ensure_ascii=False
            )

            save_conversation_message(
                conversation_id,
                user_id,
                "assistant",
                stored_image
            )

            image_conn = get_connection()
            try:
                with image_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO chats
                            (user_id, user_message, ai_response, sentiment, tokens_used)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            user_message,
                            assistant_text,
                            sentiment,
                            0
                        )
                    )
                image_conn.commit()
            except Exception:
                image_conn.rollback()
                raise
            finally:
                image_conn.close()

            return jsonify(
                response=assistant_text,
                image_url=image_url,
                type="image",
                sentiment=sentiment,
                conversation_id=conversation_id
            )

        history = load_conversation_context(
            conversation_id,
            user_id,
            limit=20
        )

        system_prompt = (
            "You are NOVA AI, a friendly, accurate, patient assistant. "
            "Use the conversation history to understand follow-up questions. "
            "Do not ask the user to repeat information already present "
            "in this conversation. "
            "When the user says they did not understand, explain again "
            "more simply with an example. "
            "Be honest when uncertain, and do not invent facts. "
            "Match the user's language when practical."
        )

        messages = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        messages.extend(
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in history
        )

        conn = get_connection()

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT selected_model
                FROM users
                WHERE id=%s
                """,
                (user_id,)
            )

            user_model = cur.fetchone()

        selected_model = (
            user_model.get("selected_model")
            if user_model
            else None
        )

        ai_response, provider_data = call_openrouter(
            messages,
            model=selected_model or OPENROUTER_MODEL,
            max_tokens=1600
        )

        save_conversation_message(
            conversation_id,
            user_id,
            "assistant",
            ai_response
        )

        # Preserve existing chats table for old history and analytics.
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO chats
                    (
                        user_id,
                        user_message,
                        ai_response,
                        sentiment,
                        tokens_used
                    )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    user_message,
                    ai_response,
                    sentiment,
                    (
                        provider_data.get("usage") or {}
                    ).get("total_tokens", 0)
                )
            )

        conn.commit()

        return jsonify(
            response=ai_response,
            sentiment=sentiment,
            conversation_id=conversation_id
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception("AI chat request failed")

        return jsonify(
            error=(
                "AI response could not be generated. "
                "Check your API key/provider settings."
            ),
            response="⚠️ AI response unavailable."
        ), 502

    finally:

        if conn:
            conn.close()


# =========================================================
# CONVERSATION LIST API
# =========================================================

@app.route("/api/conversations", methods=["GET", "POST"])
def api_conversations():

    user_id = current_user_id()

    if user_id is None:
        return jsonify(error="Unauthorized"), 401

    if request.method == "POST":

        data = request.get_json(silent=True) or {}

        title = str(
            data.get("title", "New conversation")
        )[:70].strip()

        if not title:
            title = "New conversation"

        conversation_id = create_conversation(
            user_id,
            title
        )

        return jsonify(
            success=True,
            conversation_id=conversation_id,
            title=title
        ), 201

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM conversations
                WHERE user_id=%s
                ORDER BY updated_at DESC
                LIMIT 100
                """,
                (user_id,)
            )

            rows = cur.fetchall()

        return jsonify(conversations=rows)

    finally:
        conn.close()


# =========================================================
# CONVERSATION MESSAGES API
# =========================================================

@app.route("/api/conversations/<conversation_id>/messages")
def api_conversation_messages(conversation_id):

    user_id = current_user_id()

    if user_id is None:
        return jsonify(error="Unauthorized"), 401

    if not conversation_belongs_to_user(
        conversation_id,
        user_id
    ):

        return jsonify(
            error="Conversation not found"
        ), 404

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT role, content, created_at
                FROM conversation_messages
                WHERE conversation_id=%s
                  AND user_id=%s
                ORDER BY id ASC
                """,
                (
                    conversation_id,
                    user_id
                )
            )

            messages = cur.fetchall()

        # Add structured image fields while preserving the original `content`
        # property so older versions of chat.html continue to receive familiar data.
        for message in messages:
            image_info = decode_image_message(message.get("content", ""))
            if image_info:
                message["type"] = "image"
                message["display_content"] = image_info.get("text", "Generated image")
                message["image_url"] = image_info.get("image_url")
                message["image_prompt"] = image_info.get("prompt", "")

        return jsonify(
            conversation_id=conversation_id,
            messages=messages
        )

    finally:
        conn.close()


# =========================================================
# DEMO
# =========================================================

@app.route("/watch_demo")
def watch_demo():
    return render_template("watch_demo.html")


# =========================================================
# CHAT HISTORY
# =========================================================

@app.route("/chat_history")
def chat_history():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, username, profile_image
                FROM users
                WHERE id=%s
                """,
                (user_id,)
            )

            user = cur.fetchone()

            cur.execute(
                """
                SELECT *
                FROM chats
                WHERE user_id=%s
                ORDER BY id DESC
                """,
                (user_id,)
            )

            chats = cur.fetchall()

            week_start = (
                datetime.now()
                - timedelta(days=datetime.now().weekday())
            )

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM chats
                WHERE user_id=%s
                  AND created_at >= %s
                """,
                (
                    user_id,
                    week_start
                )
            )

            week_count = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT sentiment
                FROM chats
                WHERE user_id=%s
                  AND created_at >= %s
                """,
                (
                    user_id,
                    week_start
                )
            )

            rows = cur.fetchall()

        total = len(rows)

        positive_percent = (
            round(
                sum(
                    1 for row in rows
                    if row["sentiment"] == "Positive"
                ) / total * 100
            )
            if total
            else 0
        )

        return render_template(
            "chat_history.html",
            chats=chats,
            week_count=week_count,
            positive_percent=positive_percent,
            user=user
        )

    finally:
        conn.close()


# =========================================================
# PROFILE
# =========================================================

@app.route("/profile")
def profile():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
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
                WHERE id=%s
                """,
                (user_id,)
            )

            user = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM chats
                WHERE user_id=%s
                """,
                (user_id,)
            )

            total_chats = cur.fetchone()["total"]

            try:

                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM messages
                    WHERE user_id=%s
                    """,
                    (user_id,)
                )

                total_messages = cur.fetchone()["total"]

            except pymysql.MySQLError:

                total_messages = total_chats * 2

        if not user:
            return redirect(url_for("login"))

        return render_template(
            "profile.html",
            user=user,
            total_chats=total_chats,
            total_messages=total_messages,
            streak=0,
            rating=4.9
        )

    finally:
        conn.close()


# =========================================================
# EDIT PROFILE
# =========================================================

@app.route("/edit_profile", methods=["GET", "POST"])
def edit_profile():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:

        if request.method == "POST":

            fields = [
                "username",
                "email",
                "bio",
                "phone",
                "dob",
                "country",
                "gender",
                "portfolio_link"
            ]

            values = [
                request.form.get(field) or None
                for field in fields
            ]

            image = request.files.get("profile_image")

            if image and image.filename:

                if not allowed_file(
                    image.filename,
                    ALLOWED_IMAGE_EXTENSIONS
                ):

                    flash(
                        "Please upload PNG, JPG, JPEG or WEBP image."
                    )

                    return redirect(
                        url_for("edit_profile")
                    )

                filename = (
                    f"{uuid.uuid4().hex}_"
                    f"{secure_filename(image.filename)}"
                )

                image.save(
                    os.path.join(PROFILE_FOLDER, filename)
                )

                sql = """
                    UPDATE users
                    SET username=%s,
                        email=%s,
                        bio=%s,
                        phone=%s,
                        dob=%s,
                        country=%s,
                        gender=%s,
                        portfolio_link=%s,
                        profile_image=%s
                    WHERE id=%s
                """

                values.extend([
                    filename,
                    user_id
                ])

            else:

                sql = """
                    UPDATE users
                    SET username=%s,
                        email=%s,
                        bio=%s,
                        phone=%s,
                        dob=%s,
                        country=%s,
                        gender=%s,
                        portfolio_link=%s
                    WHERE id=%s
                """

                values.append(user_id)

            with conn.cursor() as cur:
                cur.execute(sql, tuple(values))

            conn.commit()

            session["username"] = (
                request.form.get("username") or ""
            ).strip()

            flash(
                "Profile updated successfully.",
                "success"
            )

            return redirect(url_for("profile"))

        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )

            user = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM chats
                WHERE user_id=%s
                """,
                (user_id,)
            )

            chats = cur.fetchone()["total"]

            try:

                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM messages
                    WHERE user_id=%s
                    """,
                    (user_id,)
                )

                messages = cur.fetchone()["total"]

            except pymysql.MySQLError:

                messages = chats * 2

        return render_template(
            "edit_profile.html",
            user=user,
            chats=chats,
            messages=messages,
            sessions=0,
            rating=4.9,
            streak=0
        )

    except Exception:

        conn.rollback()

        logger.exception("Profile update failed")

        flash(
            "Could not update profile. "
            "Check the entered details."
        )

        return redirect(url_for("edit_profile"))

    finally:
        conn.close()


# =========================================================
# SETTINGS
# =========================================================

@app.route("/settings", methods=["GET", "POST"])
def settings():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:

        if request.method == "POST":

            dark_mode = (
                "ON"
                if request.form.get("dark_mode")
                else "OFF"
            )

            theme_color = (
                request.form.get("theme_color")
                or "Blue Theme"
            )

            if dark_mode == "OFF":
                theme_color = "Blue Theme"

            values = (
                dark_mode,
                theme_color,
                request.form.get("ai_model"),
                request.form.get("ai_personality"),
                request.form.get("email_notifications"),
                request.form.get("chat_alerts"),
                request.form.get("marketing_emails"),
                request.form.get("language"),
                request.form.get("voice_assistant"),
                request.form.get("microphone_access"),
                user_id
            )

            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE users
                    SET
                        dark_mode=%s,
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
                    """,
                    values
                )

            conn.commit()

            session["language"] = (
                request.form.get("language")
                or "English"
            )

            flash(
                "Settings Saved Successfully!",
                "success"
            )

            return redirect(url_for("settings"))

        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )

            user = cur.fetchone()

        return render_template(
            "settings.html",
            user=user
        )

    finally:
        conn.close()


# =========================================================
# NOTIFICATIONS
# =========================================================

@app.route("/notifications")
def notifications():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, title, message, is_read, created_at
                FROM notifications
                WHERE user_id=%s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )

            notifications_list = cur.fetchall()

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM notifications
                WHERE user_id=%s
                """,
                (user_id,)
            )

            total = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(*) AS unread
                FROM notifications
                WHERE user_id=%s AND is_read=0
                """,
                (user_id,)
            )

            unread = cur.fetchone()["unread"]

        return render_template(
            "notifications.html",
            notifications=notifications_list,
            total=total,
            unread=unread,
            user_id=user_id
        )

    finally:
        conn.close()


@app.route("/mark-read/<int:noti_id>")
def mark_read(noti_id):

    user_id = current_user_id()

    if user_id is None:
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE notifications
                SET is_read=1
                WHERE id=%s AND user_id=%s
                """,
                (
                    noti_id,
                    user_id
                )
            )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("notifications"))


# =========================================================
# AI MODELS
# =========================================================

@app.route("/ai_models")
def ai_models():

    guard = login_required_page()

    if guard:
        return guard

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM ai_models"
            )

            models = cur.fetchall()

        return render_template(
            "ai_models.html",
            models=models
        )

    finally:
        conn.close()


@app.route("/select_model/<path:model_name>")
def select_model(model_name):

    guard = login_required_page()

    if guard:
        return guard

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE users
                SET selected_model=%s
                WHERE id=%s
                """,
                (
                    model_name,
                    current_user_id()
                )
            )

        conn.commit()

        session["selected_model"] = model_name

        flash(
            f"Model switched to {model_name} 🚀"
        )

    except Exception:

        conn.rollback()

        logger.exception("Model selection failed")

        flash("Could not select this model.")

    finally:
        conn.close()

    return redirect(url_for("chat"))


# =========================================================
# AI PERSONALITY
# =========================================================

@app.route("/ai_personality")
def ai_personality():

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM ai_personalities"
            )

            personalities = cur.fetchall()

        return render_template(
            "ai_personality.html",
            personalities=personalities
        )

    finally:
        conn.close()


# =========================================================
# SENTIMENT ANALYSIS
# =========================================================

@app.route("/sentiment_analysis")
def sentiment_analysis():

    guard = login_required_page()

    if guard:
        return guard

    user_id = current_user_id()

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT sentiment, COUNT(*) AS total
                FROM chats
                WHERE user_id=%s
                GROUP BY sentiment
                """,
                (user_id,)
            )

            sentiments = cur.fetchall()

            data = {
                "Positive": 0,
                "Neutral": 0,
                "Negative": 0
            }

            for row in sentiments:

                if row["sentiment"] in data:
                    data[row["sentiment"]] = row["total"]

            cur.execute(
                """
                SELECT username, profile_image
                FROM users
                WHERE id=%s
                """,
                (user_id,)
            )

            user = cur.fetchone()

        total = sum(data.values())

        return render_template(
            "sentiment_analysis.html",
            user=user,
            sentiments=sentiments,
            positive_pct=(
                round(data["Positive"] / total * 100, 1)
                if total else 0
            ),
            neutral_pct=(
                round(data["Neutral"] / total * 100, 1)
                if total else 0
            ),
            negative_pct=(
                round(data["Negative"] / total * 100, 1)
                if total else 0
            ),
            total_chats=total,
            chart_data=[
                data["Positive"],
                data["Neutral"],
                data["Negative"]
            ]
        )

    finally:
        conn.close()


# =========================================================
# FEEDBACK
# =========================================================

@app.route("/feedback", methods=["GET", "POST"])
def feedback():

    guard = login_required_page()

    if guard:
        return guard

    if request.method == "POST":

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO feedback
                        (user_id, feedback_message, rating)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        current_user_id(),
                        request.form.get("feedback", ""),
                        request.form.get("rating", 0)
                    )
                )

            conn.commit()

            flash("Feedback Submitted", "success")

        finally:
            conn.close()

    return render_template("feedback.html")


# =========================================================
# ADMIN AUTHORIZATION
# =========================================================

def admin_required():
    return session.get("role") == "admin"


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin_dashboard")
def admin_dashboard():

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT COUNT(*) AS total_users FROM users"
            )

            total_users = cur.fetchone()["total_users"]

            cur.execute(
                "SELECT COUNT(*) AS total_chats FROM chats"
            )

            total_chats = cur.fetchone()["total_chats"]

            cur.execute(
                """
                SELECT id, username, email, role
                FROM users
                ORDER BY id DESC
                LIMIT 5
                """
            )

            recent_users = cur.fetchall()

            try:

                cur.execute(
                    """
                    SELECT activity_type, description, created_at
                    FROM activities
                    ORDER BY created_at DESC
                    LIMIT 5
                    """
                )

                activities = cur.fetchall()

            except pymysql.MySQLError:

                activities = []

        return render_template(
            "admin_dashboard.html",
            total_users=total_users,
            total_chats=total_chats,
            recent_users=recent_users,
            activities=activities
        )

    finally:
        conn.close()


# =========================================================
# MANAGE USERS
# =========================================================

@app.route("/manage_users")
def manage_users():

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users ORDER BY id DESC"
            )

            users = cur.fetchall()

        return render_template(
            "manage_users.html",
            users=users
        )

    finally:
        conn.close()


# =========================================================
# MANAGE CHATS
# =========================================================

@app.route("/manage_chats")
def manage_chats():

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id AS user_id,
                    u.username,
                    u.email,
                    u.profile_image,
                    u.created_at AS login_date,
                    lc.sentiment,
                    lc.created_at AS chat_date
                FROM users u
                LEFT JOIN (
                    SELECT c1.*
                    FROM chats c1
                    INNER JOIN (
                        SELECT user_id, MAX(id) AS max_chat_id
                        FROM chats
                        GROUP BY user_id
                    ) c2
                    ON c1.id=c2.max_chat_id
                ) lc ON u.id=lc.user_id
                ORDER BY lc.created_at DESC
                """
            )

            chats = cur.fetchall()

        return render_template(
            "manage_chats.html",
            chats=chats
        )

    finally:
        conn.close()


# =========================================================
# VIEW USER CHAT
# =========================================================

@app.route("/view_user_chat/<int:user_id>")
def view_user_chat(user_id):

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )

            user = cur.fetchone()

            cur.execute(
                """
                SELECT *
                FROM chats
                WHERE user_id=%s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )

            user_chats = cur.fetchall()

        return render_template(
            "view_user_chat.html",
            user=user,
            user_chats=user_chats
        )

    finally:
        conn.close()


# =========================================================
# DELETE CHAT
# =========================================================

@app.route(
    "/delete_chat/<int:chat_id>",
    methods=["GET", "POST"]
)
def delete_chat(chat_id):

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "DELETE FROM chats WHERE id=%s",
                (chat_id,)
            )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("manage_chats"))


# =========================================================
# DELETE USER
# =========================================================

@app.route(
    "/delete_user/<int:user_id>",
    methods=["GET", "POST"]
)
def delete_user(user_id):

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "DELETE FROM users WHERE id=%s",
                (user_id,)
            )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("manage_users"))


# =========================================================
# SEARCH CHAT
# =========================================================

@app.route("/search_chat", methods=["POST"])
def search_chat():

    guard = login_required_page()

    if guard:
        return jsonify(error="Unauthorized"), 401

    keyword = request.form.get(
        "keyword", ""
    ).strip()

    search = f"%{keyword}%"

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM chat_history
                WHERE LOWER(message) LIKE LOWER(%s)
                   OR LOWER(response) LIKE LOWER(%s)
                ORDER BY id DESC
                """,
                (
                    search,
                    search
                )
            )

            chats = cur.fetchall()

        return jsonify(
            status="success",
            chats=chats
        )

    except pymysql.MySQLError:

        logger.exception("Search failed")

        return jsonify(
            status="error",
            message="Could not search chat history."
        )

    finally:
        conn.close()


# =========================================================
# ADMIN ANALYTICS
# =========================================================

@app.route("/analytics")
def analytics():

    if not admin_required():
        return redirect(url_for("login"))

    start_date = request.args.get("start")
    end_date = request.args.get("end")

    date_filter = ""
    params = []

    if start_date and end_date:

        date_filter = (
            "WHERE DATE(created_at) BETWEEN %s AND %s"
        )

        params = [
            start_date,
            end_date
        ]

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                f"""
                SELECT
                    DATE(created_at) AS date,
                    COUNT(*) AS total
                FROM chats
                {date_filter}
                GROUP BY DATE(created_at)
                ORDER BY date ASC
                """,
                params
            )

            daily_data = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    sentiment,
                    COUNT(*) AS total
                FROM chats
                {date_filter}
                GROUP BY sentiment
                """,
                params
            )

            sentiment_rows = cur.fetchall()

            sentiment_data = {
                "Positive": 0,
                "Neutral": 0,
                "Negative": 0
            }

            for row in sentiment_rows:

                if row["sentiment"] in sentiment_data:
                    sentiment_data[row["sentiment"]] = row["total"]

            top_where = (
                "WHERE DATE(c.created_at) BETWEEN %s AND %s"
                if start_date and end_date
                else ""
            )

            cur.execute(
                f"""
                SELECT
                    u.id,
                    u.username,
                    COUNT(c.id) AS total_chats
                FROM chats c
                JOIN users u ON u.id=c.user_id
                {top_where}
                GROUP BY u.id, u.username
                ORDER BY total_chats DESC
                LIMIT 5
                """,
                params
            )

            top_users = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    HOUR(created_at) AS hour,
                    COUNT(*) AS total
                FROM chats
                {date_filter}
                GROUP BY hour
                ORDER BY hour ASC
                """,
                params
            )

            heatmap = cur.fetchall()

        return render_template(
            "analytics.html",
            daily_data=daily_data,
            sentiment_data=sentiment_data,
            top_users=top_users,
            heatmap=heatmap,
            start_date=start_date,
            end_date=end_date
        )

    finally:
        conn.close()


# =========================================================
# 404 ERROR HANDLER
# =========================================================

@app.errorhandler(404)
def not_found(error):
    return render_template("error.html"), 404

# =========================================================
# IMAGE ANALYSIS AND IMAGE GENERATION
# =========================================================

@app.route("/image_analysis", methods=["GET", "POST"])
def image_analysis():

    guard = login_required_page()

    if guard:
        return guard

    if request.method == "GET":

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT *
                    FROM image_analysis_history
                    WHERE user_id=%s
                    ORDER BY id DESC
                    LIMIT 12
                    """,
                    (current_user_id(),)
                )

                history = cur.fetchall()

        except pymysql.MySQLError:

            logger.exception(
                "Could not load image history"
            )

            history = []

        finally:
            conn.close()

        return render_template(
            "image_analysis.html",
            history=history
        )

    action = request.form.get("action")

    try:

        # IMAGE DESCRIPTION
        if action == "describe":

            image_file = request.files.get("image")

            if not image_file or not image_file.filename:

                return jsonify(
                    success=False,
                    error="Please upload an image."
                ), 400

            if not allowed_file(
                image_file.filename,
                ALLOWED_IMAGE_EXTENSIONS
            ):

                return jsonify(
                    success=False,
                    error="Supported formats: PNG, JPG, JPEG, WEBP."
                ), 400

            filename = (
                f"{uuid.uuid4().hex}_"
                f"{secure_filename(image_file.filename)}"
            )

            filepath = os.path.join(
                PROFILE_FOLDER,
                filename
            )

            image_file.save(filepath)

            with Image.open(filepath) as img:
                img.verify()

            with open(filepath, "rb") as f:
                image_base64 = base64.b64encode(
                    f.read()
                ).decode("utf-8")

            extension = filename.rsplit(".", 1)[1].lower()

            mime_types = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp"
            }

            mime = mime_types.get(
                extension,
                "image/jpeg"
            )

            prompt = (
                "Describe the uploaded image accurately. "
                "Explain visible objects, clothing if relevant, "
                "colors, background, lighting, and composition. "
                "Do not guess identities or facts that cannot be seen."
            )

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{mime};base64,"
                                    f"{image_base64}"
                                )
                            }
                        }
                    ]
                }
            ]

            result_text, _ = call_openrouter(
                messages,
                model=os.getenv(
                    "OPENROUTER_VISION_MODEL",
                    "openai/gpt-4o-mini"
                ),
                max_tokens=1000
            )

            conn = get_connection()

            try:
                with conn.cursor() as cur:

                    cur.execute(
                        """
                        INSERT INTO image_analysis_history
                            (
                                user_id,
                                image_path,
                                analysis_result,
                                created_at
                            )
                        VALUES (%s, %s, %s, NOW())
                        """,
                        (
                            current_user_id(),
                            f"profile_images/{filename}",
                            result_text
                        )
                    )

                conn.commit()

            finally:
                conn.close()

            return jsonify(
                success=True,
                result=result_text,
                image_path=url_for(
                    "static",
                    filename=f"profile_images/{filename}"
                )
            )

        # IMAGE GENERATION (shared with normal chat)
        if action == "generate":
            prompt = request.form.get("prompt", "").strip()
            if not prompt:
                return jsonify(
                    success=False,
                    error="Please enter an image prompt."
                ), 400

            try:
                image_url = generate_image_asset(prompt)
            except ValueError as exc:
                return jsonify(success=False, error=str(exc)), 400
            except RuntimeError as exc:
                logger.warning("Image generation request failed: %s", str(exc)[:300])
                return jsonify(success=False, error=str(exc)), 502

            return jsonify(
                success=True,
                image_url=image_url,
                prompt=prompt
            )

        return jsonify(
            success=False,
            error="Invalid action."
        ), 400

    except Exception:

        logger.exception(
            "Image analysis/generation failed"
        )

        return jsonify(
            success=False,
            error=(
                "Image operation failed. "
                "Check server logs and API configuration."
            )
        ), 500


# =========================================================
# DOCUMENT ANALYSIS
# =========================================================

@app.route("/document_upload")
def document_upload():

    guard = login_required_page()

    if guard:
        return guard

    return render_template("document_upload.html")


@app.route("/upload_document", methods=["POST"])
def upload_document():

    if current_user_id() is None:

        return jsonify(
            success=False,
            error="Please login first."
        ), 401

    file = request.files.get("document")

    if not file or not file.filename:

        return jsonify(
            success=False,
            error="No document selected."
        ), 400

    if not allowed_file(
        file.filename,
        ALLOWED_DOCUMENT_EXTENSIONS
    ):

        return jsonify(
            success=False,
            error="Only PDF, TXT and DOCX files are supported."
        ), 400

    filename = (
        f"{uuid.uuid4().hex}_"
        f"{secure_filename(file.filename)}"
    )

    filepath = os.path.join(
        DOCUMENT_FOLDER,
        filename
    )

    try:

        file.save(filepath)

        extracted_text = ""

        if filename.lower().endswith(".pdf"):

            with open(filepath, "rb") as pdf_file:

                reader = PyPDF2.PdfReader(pdf_file)

                extracted_text = "\n".join(
                    page.extract_text() or ""
                    for page in reader.pages
                )

        elif filename.lower().endswith(".txt"):

            with open(
                filepath,
                "r",
                encoding="utf-8",
                errors="replace"
            ) as txt_file:

                extracted_text = txt_file.read()

        elif filename.lower().endswith(".docx"):

            document = docx.Document(filepath)

            extracted_text = "\n".join(
                paragraph.text
                for paragraph in document.paragraphs
            )

        if not extracted_text.strip():

            return jsonify(
                success=False,
                error="Could not read text from this document."
            ), 400

        prompt = (
            "Analyze this document. Provide its main topic, "
            "key points, concise summary, important insights "
            "and conclusion.\n\nDOCUMENT:\n"
            + extracted_text[:12000]
        )

        analysis, _ = call_openrouter(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a professional document analyst."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            model=os.getenv(
                "OPENROUTER_DOCUMENT_MODEL",
                "deepseek/deepseek-chat-v3-0324"
            ),
            max_tokens=1600
        )

        return jsonify(
            success=True,
            filename=secure_filename(file.filename),
            analysis=analysis
        )

    except Exception:

        logger.exception(
            "Document analysis failed"
        )

        return jsonify(
            success=False,
            error=(
                "Document analysis failed. "
                "Check provider configuration."
            )
        ), 500


# =========================================================
# VOICE CHAT
# =========================================================

@app.route("/voice_chat")
def voice_chat():

    guard = login_required_page()

    if guard:
        return guard

    return render_template("voice_chat.html")


# =========================================================
# FORGOT PASSWORD
# =========================================================

@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        username = request.form.get(
            "username", ""
        ).strip()

        email = request.form.get(
            "email", ""
        ).strip().lower()

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE username=%s AND email=%s
                    """,
                    (
                        username,
                        email
                    )
                )

                user = cur.fetchone()

            if user:

                session["reset_user"] = user["id"]

                return redirect(
                    url_for("reset_password")
                )

            flash("Invalid username or email")

        finally:
            conn.close()

    return render_template("forgot_password.html")


# =========================================================
# RESET PASSWORD
# =========================================================

@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():

    user_id = session.get("reset_user")

    if not user_id:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":

        password = request.form.get(
            "password", ""
        )

        confirm = request.form.get(
            "confirm_password", ""
        )

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters."
            )

            return redirect(
                url_for("reset_password")
            )

        if password != confirm:

            flash("Passwords do not match.")

            return redirect(
                url_for("reset_password")
            )

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    UPDATE users
                    SET password=%s
                    WHERE id=%s
                    """,
                    (
                        generate_password_hash(password),
                        user_id
                    )
                )

            conn.commit()

        finally:
            conn.close()

        session.pop("reset_user", None)

        flash(
            "Password updated. Please log in."
        )

        return redirect(url_for("login"))

    return render_template("reset_password.html")


# =========================================================
# TEMPLATE CONTEXT PROCESSORS
# =========================================================

@app.context_processor
def inject_user_settings():

    default = {
        "user_theme": "Blue Theme",
        "dark_mode": False
    }

    user_id = current_user_id()

    if user_id is None:
        return default

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT theme_color, dark_mode
                FROM users
                WHERE id=%s
                """,
                (user_id,)
            )

            user = cur.fetchone()

        if user:

            return {
                "user_theme": (
                    user.get("theme_color")
                    or "Blue Theme"
                ),
                "dark_mode": (
                    user.get("dark_mode") == "ON"
                )
            }

        return default

    finally:
        conn.close()


@app.context_processor
def inject_translator():

    language = session.get(
        "language",
        "English"
    )

    lang_data = translations.get(
        language,
        translations.get("English", {})
    )

    return {
        "t": lambda key: lang_data.get(key, key),
        "current_language": language
    }


# =========================================================
# ABOUT PAGE
# =========================================================

@app.route("/about")
def about():

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM about LIMIT 1"
            )

            about_data = cur.fetchone()

            cur.execute(
                "SELECT * FROM people ORDER BY id DESC"
            )

            people = cur.fetchall()

        return render_template(
            "about.html",
            about=about_data,
            people=people
        )

    finally:
        conn.close()


# =========================================================
# ADMIN ABOUT
# =========================================================

@app.route("/admin_about")
def admin_about():

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM about LIMIT 1"
            )

            about_data = cur.fetchone()

            cur.execute(
                "SELECT * FROM people ORDER BY id DESC"
            )

            people = cur.fetchall()

        return render_template(
            "admin_about.html",
            about=about_data,
            people=people
        )

    finally:
        conn.close()


# =========================================================
# SAVE ABOUT
# =========================================================

@app.route("/save_about", methods=["POST"])
def save_about():

    if not admin_required():
        return redirect(url_for("login"))

    description = request.form.get(
        "description", ""
    )

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT id FROM about LIMIT 1"
            )

            existing = cur.fetchone()

            if existing:

                cur.execute(
                    """
                    UPDATE about
                    SET description=%s
                    WHERE id=%s
                    """,
                    (
                        description,
                        existing["id"]
                    )
                )

            else:

                cur.execute(
                    """
                    INSERT INTO about(description)
                    VALUES(%s)
                    """,
                    (description,)
                )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("admin_about"))


# =========================================================
# ADD PERSON
# =========================================================

@app.route("/add_person", methods=["POST"])
def add_person():

    if not admin_required():
        return redirect(url_for("login"))

    file = request.files.get("profile_image")

    if not file or not file.filename:

        flash("Please select a profile image.")

        return redirect(
            url_for("admin_about")
        )

    if not allowed_file(
        file.filename,
        ALLOWED_IMAGE_EXTENSIONS
    ):

        flash("Invalid profile image format.")

        return redirect(
            url_for("admin_about")
        )

    filename = (
        f"{uuid.uuid4().hex}_"
        f"{secure_filename(file.filename)}"
    )

    file.save(
        os.path.join(PROFILE_FOLDER, filename)
    )

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO people
                    (profile_image, name, age, role, bio, skills)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    filename,
                    request.form.get("name"),
                    request.form.get("age"),
                    request.form.get("role"),
                    request.form.get("bio"),
                    request.form.get("skills")
                )
            )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("admin_about"))


# =========================================================
# DELETE PERSON
# =========================================================

@app.route(
    "/delete_person/<int:id>",
    methods=["GET", "POST"]
)
def delete_person(id):

    if not admin_required():
        return redirect(url_for("login"))

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "DELETE FROM people WHERE id=%s",
                (id,)
            )

        conn.commit()

    finally:
        conn.close()

    return redirect(url_for("admin_about"))


# =========================================================
# STATIC PAGES
# =========================================================

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/support")
def support():
    return render_template("support.html")


@app.route("/features")
def features():
    return render_template("features.html")


# =========================================================
# TEAM PAGE
# =========================================================

@app.route("/team")
def team():

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM team_members"
            )

            members = cur.fetchall()

        return render_template(
            "team.html",
            members=members
        )

    finally:
        conn.close()


# =========================================================
# CONTACT PAGE
# =========================================================

@app.route("/contact", methods=["GET", "POST"])
def contact():

    if request.method == "POST":

        if (
            not app.config.get("MAIL_USERNAME")
            or not app.config.get("MAIL_PASSWORD")
        ):

            flash(
                "Email service is not configured.",
                "danger"
            )

            return redirect(url_for("contact"))

        try:

            msg = Message(
                subject=(
                    "NOVA AI Contact • "
                    f"{request.form.get('subject', '')}"
                ),
                sender=app.config["MAIL_USERNAME"],
                recipients=[
                    os.getenv(
                        "CONTACT_RECIPIENT",
                        app.config["MAIL_USERNAME"]
                    )
                ]
            )

            msg.body = (
                f"Name: {request.form.get('fullname')}\n"
                f"Email: {request.form.get('email')}\n"
                f"Subject: {request.form.get('subject')}\n\n"
                f"Message:\n"
                f"{request.form.get('message')}"
            )

            mail.send(msg)

            flash(
                "Message sent successfully 🚀",
                "success"
            )

        except Exception:

            logger.exception(
                "Contact email failed"
            )

            flash(
                "Something went wrong while sending the message.",
                "danger"
            )

        return redirect(url_for("contact"))

    return render_template("contact.html")


# =========================================================
# COOKIES POLICY
# =========================================================

@app.route("/cookies_policy")
def cookies_policy():

    user = None

    user_id = current_user_id()

    if user_id is not None:

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    "SELECT * FROM users WHERE id=%s",
                    (user_id,)
                )

                user = cur.fetchone()

        finally:
            conn.close()

    return render_template(
        "cookies_policy.html",
        user=user
    )


# =========================================================
# UPLOAD TEAM MEMBERS
# =========================================================

@app.route("/upload_people", methods=["GET", "POST"])
def upload_people():

    if not admin_required():
        return redirect(url_for("login"))

    if request.method == "POST":

        profile = request.files.get("profile_image")

        if not profile or not profile.filename:

            flash("Please select a profile image.")

            return redirect(
                url_for("upload_people")
            )

        if not allowed_file(
            profile.filename,
            ALLOWED_IMAGE_EXTENSIONS
        ):

            flash("Invalid image format.")

            return redirect(
                url_for("upload_people")
            )

        filename = (
            f"{uuid.uuid4().hex}_"
            f"{secure_filename(profile.filename)}"
        )

        profile.save(
            os.path.join(PROFILE_FOLDER, filename)
        )

        fields = [
            "first_name",
            "middle_name",
            "last_name",
            "age",
            "role_name",
            "bio",
            "skills",
            "linkedin_url",
            "github_url",
            "email"
        ]

        values = [
            request.form.get(field)
            for field in fields
        ]

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO team_members
                    (
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
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    """,
                    [filename] + values
                )

            conn.commit()

        finally:
            conn.close()

        return redirect(url_for("team"))

    return render_template("upload_people.html")


# =========================================================
# SEND TEST MAIL
# =========================================================

@app.route("/send-test-mail")
def send_test_mail():

    try:

        if (
            not app.config.get("MAIL_USERNAME")
            or not app.config.get("MAIL_PASSWORD")
        ):

            return (
                "Mail credentials are not configured.",
                503
            )

        msg = Message(
            "Flask Mail Test 🚀",
            sender=app.config["MAIL_USERNAME"],
            recipients=[
                os.getenv(
                    "CONTACT_RECIPIENT",
                    app.config["MAIL_USERNAME"]
                )
            ]
        )

        msg.body = "NOVA AI Flask mail test."

        mail.send(msg)

        return "Mail Sent Successfully ✅"

    except Exception:

        logger.exception("Test mail failed")

        return (
            "Mail sending failed. Check server logs.",
            500
        )


@app.route("/test-mail")
def test_mail():
    return send_test_mail()


# =========================================================
# VOICE COMMAND
# =========================================================

@app.route("/voice-command", methods=["POST"])
def voice_command():

    data = request.get_json(silent=True) or {}

    command = str(
        data.get("command", "")
    ).lower().strip()

    if "open" in command:

        site = command.replace(
            "open", "", 1
        ).strip()

        sites = {
            "youtube": "https://youtube.com",
            "google": "https://google.com",
            "instagram": "https://instagram.com",
            "amazon": "https://amazon.in"
        }

        if site in sites:

            return jsonify(
                action="open",
                url=sites[site],
                message=f"Opening {site}"
            )

        safe_site = "".join(
            ch for ch in site.replace(" ", "")
            if ch.isalnum() or ch in ".-_"
        )

        if not safe_site:

            return jsonify(
                action="none",
                message="Website name not recognized."
            )

        target = (
            safe_site
            if "." in safe_site
            else safe_site + ".com"
        )

        return jsonify(
            action="open",
            url=f"https://www.{target}",
            message=f"Trying to open {site}"
        )

    if "play" in command:

        song = command.replace(
            "play", "", 1
        ).strip()

        return jsonify(
            action="play",
            query=song,
            message=f"Searching for {song}"
        )

    return jsonify(
        action="none",
        message="Command not recognized"
    )


# =========================================================
# CHAT STATISTICS
# =========================================================

@app.route("/chat_stats")
def chat_stats():

    user_id = current_user_id()

    if user_id is None:
        return jsonify(error="Unauthorized"), 401

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM chats
                WHERE user_id=%s
                  AND created_at >= NOW()-INTERVAL 7 DAY
                """,
                (user_id,)
            )

            week_count = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT sentiment, COUNT(*) AS count
                FROM chats
                WHERE user_id=%s
                GROUP BY sentiment
                """,
                (user_id,)
            )

            rows = cur.fetchall()

        total = sum(
            row["count"] for row in rows
        )

        positive = sum(
            row["count"]
            for row in rows
            if row["sentiment"] == "Positive"
        )

        return jsonify(
            week_count=week_count,
            positive_percent=(
                int(positive / total * 100)
                if total else 0
            )
        )

    finally:
        conn.close()


# =========================================================
# FILTER CHATS
# =========================================================

@app.route("/filter_chats")
def filter_chats():

    user_id = current_user_id()

    if user_id is None:
        return jsonify(error="Unauthorized"), 401

    sentiment = request.args.get(
        "sentiment",
        "All"
    )

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            if sentiment == "All":

                cur.execute(
                    """
                    SELECT *
                    FROM chats
                    WHERE user_id=%s
                    ORDER BY id DESC
                    """,
                    (user_id,)
                )

            else:

                cur.execute(
                    """
                    SELECT *
                    FROM chats
                    WHERE user_id=%s
                      AND sentiment=%s
                    ORDER BY id DESC
                    """,
                    (
                        user_id,
                        sentiment
                    )
                )

            chats = cur.fetchall()

        return jsonify(chats)

    finally:
        conn.close()


# =========================================================
# USER ANALYTICS DASHBOARD
# =========================================================

@app.route("/user_analytics_dashboard/<int:user_id>")
def user_analytics_dashboard(user_id):

    if (
        not admin_required()
        and current_user_id() != user_id
    ):

        return redirect(url_for("login"))

    selected_date = request.args.get("date")
    selected_month = request.args.get("month")
    selected_year = request.args.get("year")

    where_clause = "WHERE user_id=%s"

    values = [user_id]

    if selected_date:

        where_clause += " AND DATE(created_at)=%s"

        values.append(selected_date)

    elif selected_month:

        where_clause += (
            " AND DATE_FORMAT(created_at,'%%Y-%%m')=%s"
        )

        values.append(selected_month)

    elif selected_year:

        where_clause += " AND YEAR(created_at)=%s"

        values.append(selected_year)

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )

            user = cur.fetchone()

            cur.execute(
                f"""
                SELECT COUNT(*) AS total_chats
                FROM chats
                {where_clause}
                """,
                values
            )

            total_chats = cur.fetchone()["total_chats"]

            counts = {}

            for name in (
                "Positive",
                "Neutral",
                "Negative"
            ):

                cur.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM chats
                    {where_clause}
                    AND sentiment=%s
                    """,
                    values + [name]
                )

                counts[name] = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT
                    DATE(created_at) AS chat_day,
                    COUNT(*) AS total
                FROM chats
                {where_clause}
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at) ASC
                LIMIT 7
                """,
                values
            )

            chart_result = cur.fetchall()

        labels = [
            str(row["chat_day"])
            for row in chart_result
        ]

        chart_values = [
            row["total"]
            for row in chart_result
        ]

        def pct(value):

            return (
                round(
                    value / total_chats * 100,
                    1
                )
                if total_chats
                else 0
            )

        return render_template(
            "user_analytics_dashboard.html",
            user=user,
            total_chats=total_chats,
            positive=counts["Positive"],
            neutral=counts["Neutral"],
            negative=counts["Negative"],
            positive_pct=pct(counts["Positive"]),
            neutral_pct=pct(counts["Neutral"]),
            negative_pct=pct(counts["Negative"]),
            chart_labels=labels,
            chart_values=chart_values,
            selected_date=selected_date,
            selected_month=selected_month,
            selected_year=selected_year
        )

    finally:
        conn.close()


# =========================================================
# APPLICATION START
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=(
            os.getenv(
                "FLASK_DEBUG",
                "false"
            ).lower() == "true"
        )
    )
