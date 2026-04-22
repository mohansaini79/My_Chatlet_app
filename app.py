import os
import re
import secrets
import logging
import mimetypes
from datetime import datetime, timezone, timedelta
from functools import wraps
from collections import defaultdict, deque

# ✅ Eventlet monkey patching must be first
import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, session, redirect, url_for, flash, jsonify
)
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash
from bson.objectid import ObjectId
from bson.errors import InvalidId
from markupsafe import escape
from authlib.integrations.flask_client import OAuth

# ✅ Cloudinary imports
import cloudinary
import cloudinary.uploader

# -------------------------------------------------------------------
# Environment + Logging
# -------------------------------------------------------------------
load_dotenv()

log_level = logging.DEBUG if os.getenv("DEBUG", "False").lower() == "true" else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("chatlet")

# -------------------------------------------------------------------
# App Configuration
# -------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB request limit
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "False").lower() == "true"

# OAuth config
app.config["GOOGLE_CLIENT_ID"] = os.getenv("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.getenv("GOOGLE_CLIENT_SECRET", "")
app.config["GOOGLE_DISCOVERY_URL"] = "https://accounts.google.com/.well-known/openid-configuration"

# -------------------------------------------------------------------
# Optional Trusted Proxies / HTTPS
# -------------------------------------------------------------------
TRUST_PROXY = os.getenv("TRUST_PROXY", "False").lower() == "true"

# -------------------------------------------------------------------
# CORS (Socket.IO)
# -------------------------------------------------------------------
allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
if allowed_origins_raw == "*":
    ALLOWED_ORIGINS = "*"
else:
    ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_raw.split(",") if origin.strip()]

# -------------------------------------------------------------------
# Socket.IO with Eventlet
# -------------------------------------------------------------------
socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="eventlet",  # ✅ eventlet requested
    logger=log_level == logging.DEBUG,
    engineio_logger=log_level == logging.DEBUG,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=10 * 1024 * 1024
)

# -------------------------------------------------------------------
# OAuth setup (Google)
# -------------------------------------------------------------------
oauth = OAuth(app)
google_oauth_enabled = bool(app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"])

if google_oauth_enabled:
    oauth.register(
        name="google",
        server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        client_kwargs={"scope": "openid email profile"}
    )
    logger.info("✅ Google OAuth configured")
else:
    logger.warning("⚠️ Google OAuth not configured. GOOGLE_CLIENT_ID/SECRET missing.")

# -------------------------------------------------------------------
# Timezone helpers
# -------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))


def get_current_time():
    """Get current time in IST ISO format."""
    return datetime.now(IST).isoformat()


def utc_now():
    return datetime.now(timezone.utc)


# -------------------------------------------------------------------
# Input validation helpers
# -------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username or ""))


def is_valid_email(email: str) -> bool:
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


def safe_text(value: str, max_len: int=2000) -> str:
    if value is None:
        return ""
    value = value.strip()
    if len(value) > max_len:
        value = value[:max_len]
    return value


def auth_required_http(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api") or request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "Not authenticated"}), 401
            flash("Please login first!", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def auth_required_socket():
    username = session.get("username")
    if not username:
        emit("error", {"msg": "Not authenticated"})
        disconnect()
        return None
    return username


# -------------------------------------------------------------------
# Basic in-memory rate limiting (per process)
# -------------------------------------------------------------------
RATE_LIMIT_BUCKETS = defaultdict(lambda: deque(maxlen=100))
RATE_LIMIT_WINDOW_SECONDS = 60


def is_rate_limited(key: str, limit: int, window_seconds: int=RATE_LIMIT_WINDOW_SECONDS) -> bool:
    now_ts = utc_now().timestamp()
    bucket = RATE_LIMIT_BUCKETS[key]
    while bucket and (now_ts - bucket[0]) > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now_ts)
    return False


def client_ip() -> str:
    if TRUST_PROXY and request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    return request.remote_addr or "unknown"


def generate_username_from_email(email: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", email.split("@")[0])[:20] or "user"
    candidate = base
    suffix = 1
    while users_collection.find_one({"username": candidate}):
        candidate = f"{base}_{suffix}"
        suffix += 1
        if suffix > 9999:
            candidate = f"user_{secrets.token_hex(4)}"
            break
    return candidate[:30]


# -------------------------------------------------------------------
# Security headers
# -------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# -------------------------------------------------------------------
# Cloudinary setup
# -------------------------------------------------------------------
cloud_name = os.getenv("CLOUD_NAME")
api_key = os.getenv("CLOUDINARY_API_KEY")
api_secret = os.getenv("CLOUDINARY_API_SECRET")

CLOUDINARY_ENABLED = bool(cloud_name and api_key and api_secret)

if CLOUDINARY_ENABLED:
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True
    )
    logger.info("✅ Cloudinary configured: %s", cloud_name)
else:
    logger.warning("⚠️ Cloudinary credentials not fully configured. Upload features may fail.")

# -------------------------------------------------------------------
# File validation
# -------------------------------------------------------------------
ALLOWED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "tiff",
    "pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx",
    "zip", "rar", "7z", "tar", "gz",
    "mp3", "wav", "ogg", "flac", "aac", "m4a", "wma",
    "mp4", "avi", "mov", "wmv", "flv", "mkv", "webm", "mpeg", "mpg",
    "py", "js", "html", "css", "json", "xml", "csv"
}

ALLOWED_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "text/",
    "application/pdf",
    "application/json",
    "application/zip",
    "application/x-zip-compressed",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint"
)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_mimetype(filename: str, provided_mimetype: str="") -> bool:
    guessed, _ = mimetypes.guess_type(filename)
    mime_to_check = provided_mimetype or guessed or ""
    if not mime_to_check:
        return True
    return any(mime_to_check.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES)


def get_file_type(filename):
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    image_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "tiff", "ico"}
    video_exts = {"mp4", "avi", "mov", "wmv", "flv", "mkv", "webm", "mpeg", "mpg"}
    audio_exts = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"}
    doc_exts = {"pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx", "csv", "json", "xml"}
    if ext in image_exts:
        return "image"
    if ext in video_exts:
        return "video"
    if ext in audio_exts:
        return "audio"
    if ext in doc_exts:
        return "document"
    return "file"


def cloudinary_resource_type(filename: str) -> str:
    ftype = get_file_type(filename)
    if ftype == "image":
        return "image"
    if ftype in ("video", "audio"):
        return "video"
    return "raw"


# -------------------------------------------------------------------
# MongoDB setup
# -------------------------------------------------------------------
MAX_RETRIES = 3
mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise RuntimeError("MONGO_URI is not set in environment.")

db_name = os.getenv("DB_NAME", "chatapp")
client = None
db = None
users_collection = None
messages_collection = None
reactions_collection = None

for attempt in range(MAX_RETRIES):
    try:
        client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000,
            retryWrites=True
        )
        client.server_info()

        db = client[db_name]
        users_collection = db["users"]
        messages_collection = db["messages"]
        reactions_collection = db["reactions"]

        users_collection.create_index([("username", ASCENDING)], unique=True)
        users_collection.create_index([("email", ASCENDING)])
        users_collection.create_index([("google_sub", ASCENDING)], sparse=True, unique=True)
        users_collection.create_index([("reset_token", ASCENDING)], sparse=True)

        messages_collection.create_index([("room", ASCENDING), ("timestamp", DESCENDING)])
        messages_collection.create_index([("username", ASCENDING), ("timestamp", DESCENDING)])

        logger.info("✅ MongoDB connected successfully to database: %s", db_name)
        break
    except Exception as e:
        logger.error("❌ MongoDB connection attempt %s failed: %s", attempt + 1, e)
        if attempt == MAX_RETRIES - 1:
            logger.critical("❌ Could not connect to MongoDB after multiple attempts")
            raise
        eventlet.sleep(2)

# -------------------------------------------------------------------
# In-memory state
# -------------------------------------------------------------------
active_users = {}
typing_users = {}
unread_counts = {}


# -------------------------------------------------------------------
# HTTP routes
# -------------------------------------------------------------------
@app.route("/")
def index():
    if "username" in session:
        return redirect(url_for("chat"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        if is_rate_limited(f"login:{ip}", limit=15, window_seconds=60):
            flash("Too many login attempts. Please wait and try again.", "error")
            return render_template("login.html", google_oauth_enabled=google_oauth_enabled)

        form_data = request.form.to_dict(flat=True)
        username = safe_text(form_data["username"] if "username" in form_data else "", 30)
        password = form_data["password"] if "password" in form_data else ""

        if not username or not password:
            flash("Username and password are required!", "error")
            return render_template("login.html", google_oauth_enabled=google_oauth_enabled)

        try:
            user = users_collection.find_one({"username": username})
            if user and user.get("password") and check_password_hash(user["password"], password):
                session.clear()
                session.permanent = True
                session["username"] = user["username"]
                session["user_id"] = str(user["_id"])
                session["auth_provider"] = "local"
                session["csrf_token"] = secrets.token_urlsafe(24)

                users_collection.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"last_seen": get_current_time(), "online": True}}
                )
                logger.info("✅ User logged in: %s", user["username"])
                return redirect(url_for("chat"))

            flash("Invalid username or password!", "error")
            logger.warning("⚠️ Failed login attempt: %s", username)
        except Exception as e:
            logger.error("❌ Login error: %s", e)
            flash("An error occurred. Please try again.", "error")

    return render_template("login.html", google_oauth_enabled=google_oauth_enabled)


@app.route("/login/google")
def login_google():
    if not google_oauth_enabled:
        flash("Google login is not configured.", "error")
        return redirect(url_for("login"))
    try:
        redirect_uri = url_for("auth_google_callback", _external=True)
        return oauth.google.authorize_redirect(redirect_uri)
    except Exception as e:
        logger.error("❌ Google login redirect error: %s", e)
        flash("Failed to start Google login.", "error")
        return redirect(url_for("login"))


@app.route("/auth/google/callback")
def auth_google_callback():
    if not google_oauth_enabled:
        flash("Google login is not configured.", "error")
        return redirect(url_for("login"))

    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo")
        if not user_info:
            user_info = oauth.google.userinfo()

        google_sub = user_info.get("sub")
        email = (user_info.get("email") or "").lower().strip()
        name = (user_info.get("name") or "").strip()
        picture = (user_info.get("picture") or "").strip()
        email_verified = bool(user_info.get("email_verified"))

        if not google_sub or not email:
            flash("Google authentication failed: missing profile info.", "error")
            return redirect(url_for("login"))

        existing = users_collection.find_one({
            "$or": [{"google_sub": google_sub}, {"email": email}]
        })

        if existing:
            update_data = {
                "last_seen": get_current_time(),
                "online": True,
                "auth_provider": "google",
                "email_verified": email_verified
            }
            if picture:
                update_data["profile_picture"] = picture
            if not existing.get("google_sub"):
                update_data["google_sub"] = google_sub

            users_collection.update_one({"_id": existing["_id"]}, {"$set": update_data})
            user = users_collection.find_one({"_id": existing["_id"]})
        else:
            username = generate_username_from_email(email)
            user_doc = {
                "username": username,
                "password": "",  # OAuth users may not have password initially
                "email": email,
                "bio": "",
                "profile_picture": picture,
                "created_at": get_current_time(),
                "last_seen": get_current_time(),
                "online": True,
                "theme": "light",
                "chat_background_type": "default",
                "chat_background_value": "default",
                "auth_provider": "google",
                "google_sub": google_sub,
                "email_verified": email_verified,
                "display_name": name or username
            }
            inserted = users_collection.insert_one(user_doc)
            user = users_collection.find_one({"_id": inserted.inserted_id})

        session.clear()
        session.permanent = True
        session["username"] = user["username"]
        session["user_id"] = str(user["_id"])
        session["auth_provider"] = "google"
        session["csrf_token"] = secrets.token_urlsafe(24)

        logger.info("✅ Google login success: %s (%s)", user["username"], email)
        return redirect(url_for("chat"))

    except Exception as e:
        logger.error("❌ Google callback error: %s", e)
        flash("Google authentication failed. Please try again.", "error")
        return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        ip = client_ip()
        if is_rate_limited(f"signup:{ip}", limit=10, window_seconds=60):
            flash("Too many signup attempts. Please wait.", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

        username = safe_text(request.form.get("username", ""), 30)
        password = request.form.get("password", "")
        email = safe_text(request.form.get("email", ""), 150).lower()

        if not username or not password:
            flash("Username and password are required!", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

        if not is_valid_username(username):
            flash("Username must be 3-30 chars (letters, numbers, underscore).", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

        if len(password) < 8:
            flash("Password must be at least 8 characters!", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

        if not is_valid_email(email):
            flash("Invalid email format!", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

        try:
            users_collection.insert_one({
                "username": username,
                "password": generate_password_hash(password),
                "email": email,
                "bio": "",
                "profile_picture": "",
                "created_at": get_current_time(),
                "last_seen": get_current_time(),
                "online": False,
                "theme": "light",
                "chat_background_type": "default",
                "chat_background_value": "default",
                "auth_provider": "local",
                "email_verified": False
            })
            logger.info("✅ New user registered: %s", username)
            flash("Signup successful! Please login.", "success")
            return redirect(url_for("login"))
        except DuplicateKeyError:
            flash("Username already exists!", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)
        except Exception as e:
            logger.error("❌ Signup error: %s", e)
            flash("An error occurred. Please try again.", "error")
            return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)

    return render_template("signup.html", google_oauth_enabled=google_oauth_enabled)


@app.route("/logout")
def logout():
    username = session.get("username")
    session.clear()

    if username:
        try:
            users_collection.update_one(
                {"username": username},
                {"$set": {"online": False, "last_seen": get_current_time()}}
            )
            active_users.pop(username, None)
            unread_counts.pop(username, None)
            logger.info("✅ User logged out: %s", username)
        except Exception as e:
            logger.error("❌ Logout error: %s", e)

    return redirect(url_for("login"))


@app.route("/chat")
@auth_required_http
def chat():
    try:
        username = session["username"]
        user = users_collection.find_one(
            {"username": username},
            {"password": 0, "reset_token": 0, "reset_token_expiry": 0}
        )
        all_users = list(users_collection.find(
            {},
            {"password": 0, "reset_token": 0, "reset_token_expiry": 0}
        ))
        return render_template(
            "chat.html",
            username=username,
            user=user,
            all_users=all_users,
            csrf_token=session.get("csrf_token")
        )
    except Exception as e:
        logger.error("❌ Chat page error: %s", e)
        flash("An error occurred. Please try again.", "error")
        return redirect(url_for("login"))


@app.route("/edit_profile", methods=["GET", "POST"])
@auth_required_http
def edit_profile():
    username = session["username"]
    user = users_collection.find_one(
        {"username": username},
        {"password": 0, "reset_token": 0, "reset_token_expiry": 0}
    )

    if request.method == "POST":
        bio = safe_text(request.form.get("bio", ""), 500)
        theme = safe_text(request.form.get("theme", "light"), 20)
        if theme not in {"light", "dark"}:
            theme = "light"

        update_data = {"bio": bio, "theme": theme}

        profile_picture = request.files.get("profile_picture")
        if profile_picture and profile_picture.filename:
            if not CLOUDINARY_ENABLED:
                flash("Image upload service is not configured.", "error")
                return redirect(url_for("edit_profile"))

            filename = profile_picture.filename
            if not allowed_file(filename) or not allowed_mimetype(filename, profile_picture.mimetype):
                flash("Invalid file type!", "error")
                return redirect(url_for("edit_profile"))

            try:
                upload_result = cloudinary.uploader.upload(
                    profile_picture,
                    folder="chatapp/profiles",
                    public_id=f"{username}_profile",
                    overwrite=True,
                    resource_type="image",
                    transformation=[
                        {"width": 300, "height": 300, "crop": "fill", "gravity": "face"},
                        {"quality": "auto:low"},
                        {"fetch_format": "auto"}
                    ],
                    timeout=30
                )
                update_data["profile_picture"] = upload_result["secure_url"]
                logger.info("✅ Profile picture uploaded: %s", upload_result.get("public_id"))
            except Exception as e:
                logger.error("❌ Cloudinary profile upload error: %s", e)
                flash("Failed to upload profile picture!", "error")
                return redirect(url_for("edit_profile"))

        try:
            users_collection.update_one({"username": username}, {"$set": update_data})
            flash("Profile updated successfully!", "success")
            return redirect(url_for("chat"))
        except Exception as e:
            logger.error("❌ Profile update error: %s", e)
            flash("Failed to update profile!", "error")

    return render_template("edit_profile.html", user=user)


@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        ip = client_ip()
        if is_rate_limited(f"forgot:{ip}", limit=8, window_seconds=60):
            flash("Too many attempts. Please wait.", "error")
            return render_template("forgot_password.html")

        username = safe_text(request.form.get("username", ""), 30)
        email = safe_text(request.form.get("email", ""), 150).lower()

        if not username or not email:
            flash("Username and email are required!", "error")
            return render_template("forgot_password.html")

        try:
            user = users_collection.find_one({"username": username, "email": email})
            if user:
                if user.get("auth_provider") == "google" and not user.get("password"):
                    flash("This account uses Google login. Please sign in with Google.", "error")
                    return redirect(url_for("login"))

                reset_token = secrets.token_urlsafe(32)
                expiry_iso = (datetime.now(IST) + timedelta(hours=1)).isoformat()

                users_collection.update_one(
                    {"username": username},
                    {"$set": {"reset_token": reset_token, "reset_token_expiry": expiry_iso}}
                )
                logger.info("✅ Password reset token generated for: %s", username)
                return redirect(url_for("reset_password", token=reset_token))

            flash("If account details are valid, a reset link has been generated.", "success")
        except Exception as e:
            logger.error("❌ Forgot password error: %s", e)
            flash("An error occurred. Please try again.", "error")

    return render_template("forgot_password.html")


@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token = safe_text(token, 200)
    try:
        user = users_collection.find_one({
            "reset_token": token,
            "reset_token_expiry": {"$gt": datetime.now(IST).isoformat()}
        })
        if not user:
            flash("Invalid or expired reset link!", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not new_password or not confirm_password:
                flash("Both password fields are required!", "error")
                return render_template("reset_password.html", token=token)

            if new_password != confirm_password:
                flash("Passwords do not match!", "error")
                return render_template("reset_password.html", token=token)

            if len(new_password) < 8:
                flash("Password must be at least 8 characters!", "error")
                return render_template("reset_password.html", token=token)

            users_collection.update_one(
                {"_id": user["_id"], "reset_token": token},
                {
                    "$set": {
                        "password": generate_password_hash(new_password),
                        "auth_provider": user.get("auth_provider", "local")
                    },
                    "$unset": {"reset_token": "", "reset_token_expiry": ""}
                }
            )

            flash("✅ Password reset successful! Please login.", "success")
            logger.info("✅ Password reset completed for: %s", user.get("username"))
            return redirect(url_for("login"))

        return render_template("reset_password.html", token=token)
    except Exception as e:
        logger.error("❌ Reset password error: %s", e)
        flash("An error occurred. Please try again.", "error")
        return redirect(url_for("login"))


@app.route("/change_password", methods=["POST"])
@auth_required_http
def change_password():
    username = session["username"]

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        return jsonify({"success": False, "error": "All fields are required"}), 400

    if new_password != confirm_password:
        return jsonify({"success": False, "error": "Passwords do not match"}), 400

    if len(new_password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters"}), 400

    try:
        users_collection.update_one(
            {"username": username},
            {"$set": {"password": generate_password_hash(new_password)}}
        )
        logger.info("✅ Password changed: %s", username)
        return jsonify({"success": True, "message": "Password changed successfully"})
    except Exception as e:
        logger.error("❌ Password change error: %s", e)
        return jsonify({"success": False, "error": "An error occurred"}), 500


# -------------------------------------------------------------------
# Upload Routes
# -------------------------------------------------------------------
@app.route("/upload_attachment", methods=["POST"])
@auth_required_http
def upload_attachment():
    username = session["username"]

    if not CLOUDINARY_ENABLED:
        return jsonify({"success": False, "error": "Upload service not configured"}), 503

    ip = client_ip()
    if is_rate_limited(f"upload_single:{ip}", limit=20, window_seconds=60):
        return jsonify({"success": False, "error": "Too many uploads. Slow down."}), 429

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    filename = file.filename
    if not allowed_file(filename):
        return jsonify({"success": False, "error": "Invalid file extension"}), 400

    if not allowed_mimetype(filename, file.mimetype):
        return jsonify({"success": False, "error": "Invalid file type"}), 400

    file.seek(0, os.SEEK_END)
    file_size_bytes = file.tell()
    file.seek(0)
    if file_size_bytes > 5 * 1024 * 1024:
        return jsonify({"success": False, "error": "File too large. Max 5MB allowed"}), 400

    try:
        upload_result = cloudinary.uploader.upload(
            file,
            folder=f"chatapp/attachments/{username}",
            resource_type=cloudinary_resource_type(filename),
            use_filename=True,
            unique_filename=True,
            quality="auto:low",
            fetch_format="auto",
            timeout=60
        )

        return jsonify({
            "success": True,
            "file_url": upload_result["secure_url"],
            "file_name": filename,
            "file_size": upload_result.get("bytes", file_size_bytes),
            "file_type": get_file_type(filename),
            "file_format": upload_result.get("format", filename.rsplit(".", 1)[1].lower() if "." in filename else ""),
            "public_id": upload_result.get("public_id", "")
        })
    except Exception as e:
        logger.error("❌ Cloudinary upload error: %s", e)
        return jsonify({"success": False, "error": "Upload failed"}), 500


@app.route("/upload_multiple_attachments", methods=["POST"])
@auth_required_http
def upload_multiple_attachments():
    username = session["username"]

    if not CLOUDINARY_ENABLED:
        return jsonify({"success": False, "error": "Upload service not configured"}), 503

    ip = client_ip()
    if is_rate_limited(f"upload_multi:{ip}", limit=10, window_seconds=60):
        return jsonify({"success": False, "error": "Too many upload requests. Slow down."}), 429

    if "files[]" not in request.files:
        return jsonify({"success": False, "error": "No files provided"}), 400

    files = request.files.getlist("files[]")
    if not files:
        return jsonify({"success": False, "error": "No files selected"}), 400

    if len(files) > 5:
        return jsonify({"success": False, "error": "Maximum 5 files allowed"}), 400

    uploaded_files = []
    for file in files:
        try:
            if not file or not file.filename:
                continue

            filename = file.filename
            if not allowed_file(filename):
                continue
            if not allowed_mimetype(filename, file.mimetype):
                continue

            file.seek(0, os.SEEK_END)
            size_bytes = file.tell()
            file.seek(0)
            if size_bytes > 5 * 1024 * 1024:
                continue

            upload_result = cloudinary.uploader.upload(
                file,
                folder=f"chatapp/attachments/{username}",
                resource_type=cloudinary_resource_type(filename),
                use_filename=True,
                unique_filename=True,
                quality="auto:low",
                fetch_format="auto",
                timeout=60
            )

            uploaded_files.append({
                "file_url": upload_result["secure_url"],
                "file_name": filename,
                "file_size": upload_result.get("bytes", size_bytes),
                "file_type": get_file_type(filename),
                "file_format": upload_result.get("format", filename.rsplit(".", 1)[1].lower() if "." in filename else ""),
                "public_id": upload_result.get("public_id", "")
            })
        except Exception as e:
            logger.error("❌ Multi-upload item error: %s", e)

    if not uploaded_files:
        return jsonify({"success": False, "error": "Failed to upload files"}), 500

    return jsonify({"success": True, "files": uploaded_files, "count": len(uploaded_files)})


@app.route("/change_background", methods=["POST"])
@auth_required_http
def change_background():
    username = session["username"]

    try:
        background_type = safe_text(request.form.get("background_type", ""), 20)
        background_value = safe_text(request.form.get("background_value", ""), 1000)

        if background_type == "upload":
            if not CLOUDINARY_ENABLED:
                return jsonify({"success": False, "error": "Upload service not configured"}), 503
            file = request.files.get("background_image")
            if not file or not file.filename:
                return jsonify({"success": False, "error": "No image selected"}), 400
            if not allowed_file(file.filename) or not allowed_mimetype(file.filename, file.mimetype):
                return jsonify({"success": False, "error": "Invalid image type"}), 400

            upload_result = cloudinary.uploader.upload(
                file,
                folder="chatapp/backgrounds",
                public_id=f"{username}_bg",
                overwrite=True,
                resource_type="image",
                transformation=[{"quality": "auto:low"}, {"fetch_format": "auto"}],
                timeout=30
            )
            background_value = upload_result["secure_url"]
            background_type = "image"

        elif background_type == "url":
            if not background_value.startswith(("http://", "https://")):
                return jsonify({"success": False, "error": "Invalid image URL"}), 400
            background_type = "image"

        elif background_type == "default":
            background_value = "default"
        else:
            return jsonify({"success": False, "error": "Invalid background type"}), 400

        users_collection.update_one(
            {"username": username},
            {"$set": {"chat_background_type": background_type, "chat_background_value": background_value}}
        )

        return jsonify({
            "success": True,
            "message": "Background updated!",
            "background_type": background_type,
            "background_value": background_value
        })
    except Exception as e:
        logger.error("❌ Background change error: %s", e)
        return jsonify({"success": False, "error": "Failed to update background"}), 500


# -------------------------------------------------------------------
# Socket events
# -------------------------------------------------------------------
@socketio.on("connect")
def on_connect():
    username = session.get("username")
    logger.info("🔌 Socket connected: %s", username or "Anonymous")

    if not username:
        return

    join_room(username)
    active_users[username] = {
        "room": active_users.get(username, {}).get("room", "general"),
        "online": True,
        "last_seen": get_current_time()
    }

    try:
        users_collection.update_one({"username": username}, {"$set": {"online": True, "last_seen": get_current_time()}})
        user_list = []
        for u in list(active_users.keys()):
            user_data = users_collection.find_one({"username": u}, {"password": 0})
            if user_data:
                user_list.append({
                    "username": u,
                    "online": active_users[u]["online"],
                    "profile_picture": user_data.get("profile_picture", ""),
                    "last_seen": user_data.get("last_seen", "")
                })
        socketio.emit("update_users", {"users": user_list})
    except Exception as e:
        logger.error("❌ Connect handler error: %s", e)


@socketio.on("join")
def on_join(data):
    username = auth_required_socket()
    if not username:
        return

    room = safe_text((data or {}).get("room", ""), 200)
    if not room:
        emit("error", {"msg": "Invalid room"})
        return

    join_room(room)
    join_room(username)

    active_users[username] = {"room": room, "online": True, "last_seen": get_current_time()}
    unread_counts.setdefault(username, {})

    try:
        user_list = []
        for u in list(active_users.keys()):
            user_data = users_collection.find_one({"username": u}, {"password": 0})
            if user_data:
                user_list.append({
                    "username": u,
                    "online": active_users[u]["online"],
                    "profile_picture": user_data.get("profile_picture", ""),
                    "last_seen": user_data.get("last_seen", "")
                })
        socketio.emit("update_users", {"users": user_list})

        history = list(messages_collection.find({"room": room}).sort("timestamp", 1).limit(100))
        history_with_data = []
        for msg in history:
            history_with_data.append({
                "username": msg.get("username", ""),
                "message": msg.get("message", ""),
                "timestamp": msg.get("timestamp", ""),
                "room": msg.get("room", ""),
                "_id": str(msg.get("_id", "")),
                "attachment": msg.get("attachment"),
                "attachments": msg.get("attachments"),
                "reply_to": msg.get("reply_to"),
                "read_by": msg.get("read_by", [])
            })

        emit("load_history", {"messages": history_with_data})
        emit("status", {"msg": f"{username} has joined the chat."}, to=room)

        messages_collection.update_many(
            {"room": room, "username": {"$ne": username}},
            {"$addToSet": {"read_by": username}}
        )

        if room in unread_counts.get(username, {}):
            del unread_counts[username][room]
        emit("update_badge", {"count": 0}, to=username)

    except Exception as e:
        logger.error("❌ Join handler error: %s", e)


@socketio.on("start_private_chat")
def start_private_chat(data):
    username = auth_required_socket()
    if not username:
        return

    target_user = safe_text((data or {}).get("target_user", ""), 30)
    if not target_user or target_user == username:
        emit("error", {"msg": "Invalid target user"})
        return

    room = ":".join(sorted([username, target_user]))
    join_room(room)
    join_room(username)

    if username not in active_users:
        active_users[username] = {"room": room, "online": True, "last_seen": get_current_time()}
    else:
        active_users[username]["room"] = room

    try:
        history = list(messages_collection.find({"room": room}).sort("timestamp", 1).limit(100))
        history_with_data = []
        for msg in history:
            history_with_data.append({
                "username": msg.get("username", ""),
                "message": msg.get("message", ""),
                "timestamp": msg.get("timestamp", ""),
                "room": msg.get("room", ""),
                "_id": str(msg.get("_id", "")),
                "attachment": msg.get("attachment"),
                "attachments": msg.get("attachments"),
                "reply_to": msg.get("reply_to"),
                "read_by": msg.get("read_by", [])
            })

        target_user_data = users_collection.find_one({"username": target_user}, {"password": 0})
        emit("load_history", {
            "messages": history_with_data,
            "target_user_profile_picture": target_user_data.get("profile_picture", "") if target_user_data else ""
        })
        emit("status", {"msg": f"Private chat with {target_user} started."}, to=room)
    except Exception as e:
        logger.error("❌ Private chat error: %s", e)


@socketio.on("typing_start")
def handle_typing_start(data):
    username = auth_required_socket()
    if not username:
        return

    room = safe_text((data or {}).get("room", ""), 200)
    if not room:
        return

    typing_users.setdefault(room, set()).add(username)
    socketio.emit("user_typing", {"username": username, "room": room, "typing": True}, to=room)


@socketio.on("typing_stop")
def handle_typing_stop(data):
    username = auth_required_socket()
    if not username:
        return

    room = safe_text((data or {}).get("room", ""), 200)
    if not room:
        return

    if room in typing_users:
        typing_users[room].discard(username)
    socketio.emit("user_typing", {"username": username, "room": room, "typing": False}, to=room)


@socketio.on("message")
def handle_message(data):
    username = auth_required_socket()
    if not username:
        return

    data = data or {}
    room = safe_text(data.get("room", ""), 200)
    message_text = safe_text(data.get("message", ""), 2000)
    if not room:
        emit("error", {"msg": "Invalid room"})
        return

    msg_doc = {
        "username": username,
        "message": str(escape(message_text)),
        "timestamp": get_current_time(),
        "room": room,
        "read_by": [username]
    }

    attachments = data.get("attachments")
    attachment = data.get("attachment")
    if isinstance(attachments, list) and attachments:
        msg_doc["attachments"] = attachments[:5]
    elif attachment:
        msg_doc["attachment"] = attachment

    try:
        result = messages_collection.insert_one(msg_doc)
        msg_id = str(result.inserted_id)

        if room in typing_users:
            typing_users[room].discard(username)
            socketio.emit("user_typing", {"username": username, "room": room, "typing": False}, to=room)

        emit_data = {
            "username": username,
            "message": msg_doc["message"],
            "timestamp": msg_doc["timestamp"],
            "room": room,
            "_id": msg_id,
            "current_user": username,
            "read_by": [username]
        }
        if "attachments" in msg_doc:
            emit_data["attachments"] = msg_doc["attachments"]
        elif "attachment" in msg_doc:
            emit_data["attachment"] = msg_doc["attachment"]

        socketio.emit("message", emit_data, to=room)

        if room == "general":
            for user in list(active_users.keys()):
                if user == username:
                    continue
                unread_counts.setdefault(user, {})
                unread_counts[user][room] = unread_counts[user].get(room, 0) + 1
                socketio.emit("notification", {
                    "message": message_text[:50],
                    "room": room,
                    "count": unread_counts[user][room],
                    "sender": username
                }, to=user)
        else:
            room_users = room.split(":")
            if len(room_users) == 2:
                target_user = room_users[0] if room_users[1] == username else room_users[1]
                unread_counts.setdefault(target_user, {})
                unread_counts[target_user][room] = unread_counts[target_user].get(room, 0) + 1
                socketio.emit("notification", {
                    "message": message_text[:50],
                    "room": room,
                    "count": unread_counts[target_user][room],
                    "sender": username
                }, to=target_user)

    except Exception as e:
        logger.error("❌ Message handler error: %s", e)
        emit("error", {"msg": "Failed to send message"})


@socketio.on("delete_message")
def handle_delete_message(data):
    username = auth_required_socket()
    if not username:
        return

    message_id = (data or {}).get("message_id")
    room = safe_text((data or {}).get("room", ""), 200)
    if not message_id or not room:
        emit("error", {"msg": "Invalid request"})
        return

    try:
        oid = ObjectId(message_id)
        message = messages_collection.find_one({"_id": oid, "room": room})
        if not message or message.get("username") != username:
            emit("error", {"msg": "Unauthorized"})
            return

        messages_collection.delete_one({"_id": oid})
        socketio.emit("delete_message", {"message_id": message_id}, to=room)
        logger.info("🗑️ Message deleted: %s", message_id)
    except (InvalidId, TypeError):
        emit("error", {"msg": "Invalid message id"})
    except Exception as e:
        logger.error("❌ Delete message error: %s", e)
        emit("error", {"msg": "Failed to delete message"})


@socketio.on("edit_message")
def handle_edit_message(data):
    username = auth_required_socket()
    if not username:
        return

    data = data or {}
    message_id = data.get("message_id")
    room = safe_text(data.get("room", ""), 200)
    new_message = safe_text(data.get("new_message", ""), 2000)

    if not message_id or not room or not new_message:
        emit("error", {"msg": "Invalid request"})
        return

    try:
        oid = ObjectId(message_id)
        message = messages_collection.find_one({"_id": oid, "room": room})
        if not message or message.get("username") != username:
            emit("error", {"msg": "Unauthorized"})
            return

        edit_time = get_current_time()
        messages_collection.update_one(
            {"_id": oid},
            {"$set": {"message": str(escape(new_message)), "edited": True, "edited_at": edit_time}}
        )

        socketio.emit("edit_message", {
            "message_id": message_id,
            "new_message": str(escape(new_message)),
            "timestamp": edit_time,
            "edited": True
        }, to=room)
        logger.info("✏️ Message edited: %s", message_id)
    except (InvalidId, TypeError):
        emit("error", {"msg": "Invalid message id"})
    except Exception as e:
        logger.error("❌ Edit message error: %s", e)
        emit("error", {"msg": "Failed to edit message"})


@socketio.on("leave")
def on_leave(data):
    username = session.get("username")
    if not username:
        return

    room = safe_text((data or {}).get("room", ""), 200)
    if not room:
        return

    leave_room(room)
    emit("status", {"msg": f"{username} has left the chat."}, to=room)
    logger.info("👋 %s left: %s", username, room)


@socketio.on("disconnect")
def on_disconnect():
    username = session.get("username")
    if not username:
        return

    if username in active_users:
        active_users[username]["online"] = False

    try:
        users_collection.update_one(
            {"username": username},
            {"$set": {"online": False, "last_seen": get_current_time()}}
        )
    except Exception as e:
        logger.error("❌ Disconnect DB update error: %s", e)

    unread_counts.pop(username, None)

    for room in list(typing_users.keys()):
        if username in typing_users[room]:
            typing_users[room].discard(username)
            socketio.emit("user_typing", {"username": username, "room": room, "typing": False}, to=room)

    socketio.emit("user_offline", {"username": username})
    logger.info("🔌 Disconnected: %s", username)


# -------------------------------------------------------------------
# Error handlers + utility routes
# -------------------------------------------------------------------
@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error("❌ Internal error: %s", error)
    return render_template("500.html"), 500


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "timestamp": get_current_time()}), 200


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("DEBUG", "False").lower() == "true"
    host = os.getenv("HOST", "0.0.0.0")

    logger.info("🚀 Starting Chatlet server (Eventlet + Google OAuth + Cloudinary)")
    logger.info("📊 Debug mode: %s", debug_mode)
    logger.info("🌐 Host: %s:%s", host, port)

    socketio.run(app, debug=debug_mode, host=host, port=port)
