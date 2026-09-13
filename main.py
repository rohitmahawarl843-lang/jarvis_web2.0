"""
World AI — Web Version
Login optional (guests get no persistence). Supports file analysis,
optional Web Search tool, optional Code Execution tool, and
MULTIPLE Gemini API keys with automatic fallback on quota errors.
"""
import os
import io
import re
import json
import uuid
import random
import secrets
import hashlib
import socket
import smtplib
import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import itertools
import threading
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from typing import List

# ---------------- Force IPv4 for ALL outbound connections ----------------
# Railway (aur kai dusre hosts) ka network kabhi IPv6 route try karta hai jo
# kaam nahi karta, jisse Gemini/Mongo/koi bhi external call 30-60s tak slow
# ho jaata hai jab tak IPv4 par fallback na ho. Yeh globally IPv4 force
# karke woh delay hamesha ke liye khatam kar deta hai.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

from fastapi import FastAPI, Request, Response, Form, File, UploadFile
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import certifi

from google import genai
from google.genai import types

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- MongoDB (Atlas) ----------------
# Railway/host par MONGO_URI env variable set karo, jaisa Atlas ne diya tha:
# mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable set nahi hai. MongoDB Atlas connection string set karo.")

mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
try:
    mongo_db = mongo_client.get_default_database()
    if mongo_db is None:
        raise Exception("no default db in URI")
except Exception:
    mongo_db = mongo_client["jarvis"]  # URI mein db name nahi diya, "jarvis" use karo

users_col = mongo_db["users"]
sessions_col = mongo_db["sessions"]
chats_col = mongo_db["chats"]
messages_col = mongo_db["messages"]
skills_col = mongo_db["skills"]
projects_col = mongo_db["projects"]
pending_signups_col = mongo_db["pending_signups"]

# ---------------- Signup OTP verification (Gmail email / mobile SMS) ----------------
# Naya account banane se pehle email ya mobile OTP verify karna zaroori hai.
# Gmail se OTP bhejne ke liye ek Gmail account chahiye jisme 2-Step Verification
# on ho, aur uska 16-digit "App Password" (normal Gmail password nahi chalega):
#   GMAIL_USER = "youraccount@gmail.com"
#   GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
# Mobile OTP ke liye Fast2SMS ka "Quick SMS" OTP route use ho raha hai (India ke
# liye sabse aasan/free option, DLT registration ki zaroorat nahi):
#   FAST2SMS_API_KEY = "<fast2sms dashboard se API key>"
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY", "").strip()
OTP_EXPIRY_SECONDS = 5 * 60
OTP_RESEND_COOLDOWN_SECONDS = 45
OTP_MAX_ATTEMPTS = 5

# ---------------- Multiple API keys support (multi-provider) ----------------
# Gemini: GEMINI_API_KEYS="key1,key2,key3" (comma separated) on Railway.
# GEMINI_API_KEY (single, old variable) still works as a fallback.
_raw_keys = os.environ.get("GEMINI_API_KEYS", "").strip()
if _raw_keys:
    API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
else:
    single = os.environ.get("GEMINI_API_KEY", "").strip()
    API_KEYS = [single] if single else []

clients = [genai.Client(api_key=k) for k in API_KEYS]
client = clients[0] if clients else None  # kept for /health check compatibility

# OpenAI (ChatGPT): OPENAI_API_KEYS="key1,key2" (comma separated).
# OPENAI_API_KEY (single) bhi chalega.
_raw_openai_keys = os.environ.get("OPENAI_API_KEYS", "").strip()
if _raw_openai_keys:
    OPENAI_KEYS = [k.strip() for k in _raw_openai_keys.split(",") if k.strip()]
else:
    single_oa = os.environ.get("OPENAI_API_KEY", "").strip()
    OPENAI_KEYS = [single_oa] if single_oa else []

openai_clients = [OpenAI(api_key=k) for k in OPENAI_KEYS] if (OpenAI and OPENAI_KEYS) else []

# Sab providers ek hi list mein — fallback isi list ke round-robin se hota hai.
# Naya platform add karna ho to bas yahan ek naya entry pattern jod do.
providers = (
    [{"type": "gemini", "client": c} for c in clients]
    + [{"type": "openai", "client": c} for c in openai_clients]
)

_rr_lock = threading.Lock()
_rr_counter = itertools.count()


def next_start_index(pool_size: int):
    if pool_size <= 0:
        return 0
    with _rr_lock:
        return next(_rr_counter) % pool_size


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
DAILY_MESSAGE_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "0"))

SYSTEM_PROMPT = """You are World AI, a friendly and helpful AI assistant.
Always reply in the SAME language the user writes in — if they write in Hindi, reply in Hindi;
if English, reply in English; if Hinglish (mixed Hindi-English), reply in Hinglish; and so on
for any other language. Match their language naturally, don't force any one language.
Be helpful and concise.
If the user attaches a file (image, video, PDF, spreadsheet, document), carefully look at/read
its content and answer their question properly or give a summary/analysis of the data.
If the Web Search tool is available, use it for current/real-time info (news, weather, prices).
If the Code Execution tool is available, actually run the code to verify the result — don't just
write it without running it."""

# ---------------- Reply-language override (Settings > Language) ----------------
# "auto" (default) = purana behavior, jo bhi language user likhe usi mein reply.
# Koi aur code diya ho to us fixed language mein hi reply karo, chahe user kisi
# aur language mein type kare.
LANGUAGE_INSTRUCTIONS = {
    "auto": "",
    "en": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to English (United States) in "
          "Settings. Always reply in English, regardless of what language the user writes in.",
    "fr": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to French (France) in Settings. "
          "Always reply in French, regardless of what language the user writes in.",
    "de": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to German (Germany) in Settings. "
          "Always reply in German, regardless of what language the user writes in.",
    "hi": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Hindi (India) in Settings. "
          "Always reply in Hindi (Devanagari script), regardless of what language the user writes in.",
    "id": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Indonesian (Indonesia) in "
          "Settings. Always reply in Indonesian, regardless of what language the user writes in.",
    "it": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Italian (Italy) in Settings. "
          "Always reply in Italian, regardless of what language the user writes in.",
    "ja": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Japanese (Japan) in Settings. "
          "Always reply in Japanese, regardless of what language the user writes in.",
    "ko": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Korean (South Korea) in "
          "Settings. Always reply in Korean, regardless of what language the user writes in.",
    "pt": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Portuguese (Brazil) in "
          "Settings. Always reply in Portuguese, regardless of what language the user writes in.",
    "es-lat": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Spanish (Latin America) "
              "in Settings. Always reply in Latin American Spanish, regardless of what language the user "
              "writes in.",
    "es-es": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Spanish (Spain) in "
             "Settings. Always reply in Spain Spanish, regardless of what language the user writes in.",
    "hinglish": "\n\nIMPORTANT OVERRIDE: The user has set their reply language to Hinglish in Settings. "
                "Always reply in Hinglish (mixed Hindi-English, Roman script), regardless of what "
                "language the user writes in.",
}


def build_system_prompt(language: str) -> str:
    return SYSTEM_PROMPT + LANGUAGE_INSTRUCTIONS.get((language or "auto").strip(), "")

guest_sessions = {}

# ---------------- Database (MongoDB) ----------------

def init_db():
    users_col.create_index("username", unique=True)
    sessions_col.create_index("token", unique=True)
    chats_col.create_index("chat_id", unique=True)
    chats_col.create_index("user_id")
    messages_col.create_index("chat_id")
    skills_col.create_index("skill_id", unique=True)
    skills_col.create_index("user_id")
    projects_col.create_index("project_id", unique=True)
    projects_col.create_index("user_id")
    pending_signups_col.create_index("signup_id", unique=True)
    # TTL index: MongoDB khud hi expire_at time nikalne ke baad ye documents
    # delete kar deta hai (background task), koi manual cleanup nahi chahiye.
    pending_signups_col.create_index("expires_at", expireAfterSeconds=0)

init_db()


def now_utc():
    return datetime.now(timezone.utc)

# ---------------- Auth helpers ----------------

def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return salt, pwd_hash


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, computed = hash_password(password, salt)
    return secrets.compare_digest(computed, expected_hash)


# ---------------- OTP helpers (signup verification) ----------------

def generate_otp() -> str:
    return str(secrets.randbelow(900_000) + 100_000)  # 6-digit, 100000-999999


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def is_valid_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def is_valid_mobile(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return len(digits) == 10 and digits[0] in "6789"  # Indian mobile number format


def send_otp_email(to_email: str, otp: str):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("Gmail OTP configured nahi hai (GMAIL_USER / GMAIL_APP_PASSWORD env var missing).")
    msg = MIMEText(f"Your World AI verification code is: {otp}\n\nYe code {OTP_EXPIRY_SECONDS // 60} minute mein expire ho jayega.\nAgar aapne ye request nahi ki, is email ko ignore karo.")
    msg["Subject"] = f"World AI verification code: {otp}"
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [to_email], msg.as_string())


def send_otp_sms(to_mobile: str, otp: str):
    if not FAST2SMS_API_KEY:
        raise RuntimeError("Mobile OTP configured nahi hai (FAST2SMS_API_KEY env var missing).")
    digits = re.sub(r"\D", "", to_mobile)[-10:]
    params = urllib.parse.urlencode({
        "authorization": FAST2SMS_API_KEY,
        "route": "otp",
        "variables_values": otp,
        "numbers": digits,
    })
    url = f"https://www.fast2sms.com/dev/bulkV2?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Fast2SMS non-2xx responses ka body me hi exact reason hota hai
        # (galat API key, wallet balance, KYC pending, waghera) — HTTPError
        # khud sirf "HTTP Error 400: Bad Request" dikhata, isliye body padhte hain.
        body = e.read().decode(errors="ignore")
        try:
            err_msg = json.loads(body).get("message")
        except Exception:
            err_msg = None
        if isinstance(err_msg, list):
            err_msg = ", ".join(str(m) for m in err_msg)
        raise RuntimeError(err_msg or body or f"Fast2SMS ne request reject ki (HTTP {e.code}).")

    result = json.loads(body)
    if not result.get("return"):
        msg = result.get("message")
        if isinstance(msg, list):
            msg = ", ".join(str(m) for m in msg)
        raise RuntimeError(msg or "SMS bhejne mein error aayi.")


def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    session = sessions_col.find_one({"token": token})
    if not session:
        return None
    user = users_col.find_one({"_id": session["user_id"]})
    if not user:
        return None
    return {"id": user["_id"], "username": user["username"]}


def unauth():
    return JSONResponse({"error": "Login required"}, status_code=401)


# ---------------- File handling ----------------

INLINE_PREFIXES = ("image/", "video/", "audio/")
INLINE_EXACT = {
    "application/pdf", "text/plain", "text/csv",
    "text/html", "text/xml", "text/rtf", "text/markdown",
}


def make_preview(filename: str, df):
    try:
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        chart = None
        if numeric_cols:
            col = numeric_cols[0]
            sample = df[col].head(10).fillna(0).tolist()
            chart = {"label": col, "values": [float(x) for x in sample]}
        return {
            "filename": filename,
            "columns": [str(c) for c in df.columns.tolist()],
            "rows": df.head(5).fillna("").astype(str).values.tolist(),
            "chart": chart,
        }
    except Exception:
        return None


def build_file_part(filename: str, content_type: str, data: bytes):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    mime = (content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";")[0].strip()

    if ext in ("xlsx", "xls"):
        try:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(data))
            csv_text = df.to_csv(index=False)
            preview = make_preview(filename, df)
            return types.Part.from_text(text=f"[Uploaded spreadsheet: {filename}]\n{csv_text[:6000]}"), preview
        except Exception as e:
            return types.Part.from_text(text=f"[Spreadsheet '{filename}' padhne mein error: {e}]"), None

    if ext == "csv":
        try:
            import pandas as pd
            df = pd.read_csv(io.BytesIO(data))
            preview = make_preview(filename, df)
            return types.Part.from_bytes(data=data, mime_type="text/csv"), preview
        except Exception:
            return types.Part.from_bytes(data=data, mime_type="text/csv"), None

    if ext == "docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
            return types.Part.from_text(text=f"[Uploaded document: {filename}]\n{text[:8000]}"), None
        except Exception as e:
            return types.Part.from_text(text=f"[Document '{filename}' padhne mein error: {e}]"), None

    if mime.startswith(INLINE_PREFIXES) or mime in INLINE_EXACT:
        return types.Part.from_bytes(data=data, mime_type=mime), None

    try:
        text = data.decode("utf-8", errors="ignore")
        return types.Part.from_text(text=f"[Uploaded file: {filename}]\n{text[:8000]}"), None
    except Exception:
        return types.Part.from_text(text=f"[File '{filename}' (type: {mime}) analyze nahi ho saka]"), None


def get_tools(mode: str):
    if mode == "search":
        return [types.Tool(google_search=types.GoogleSearch())]
    if mode == "code":
        return [types.Tool(code_execution=types.ToolCodeExecution())]
    return None


def is_quota_error(err: Exception) -> bool:
    s = str(err).lower()
    return (
        "429" in s or "resource_exhausted" in s or "quota" in s
        or "rate limit" in s or "rate_limit" in s or "insufficient_quota" in s
    )


def contents_to_openai_messages(contents, system_prompt):
    """Gemini-style `contents` list ko OpenAI ke messages format mein badalta hai.
    (Sirf text — file attachments/tools OpenAI path mein support nahi hain.)"""
    messages = [{"role": "system", "content": system_prompt}]
    for c in contents:
        role = "user" if c.role == "user" else "assistant"
        text_parts = [getattr(p, "text", None) for p in c.parts]
        text_parts = [t for t in text_parts if t]
        if text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
    return messages


def stream_openai_text(openai_client, contents, system_prompt, max_tokens):
    """OpenAI se streaming text chunks yield karta hai (Gemini stream jaisa hi shape)."""
    messages = contents_to_openai_messages(contents, system_prompt)
    stream = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_completion_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


def extract_piece(chunk):
    piece = ""
    try:
        if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
            for part in chunk.candidates[0].content.parts:
                if getattr(part, "text", None):
                    piece += part.text
                elif getattr(part, "executable_code", None):
                    piece += f"\n```python\n{part.executable_code.code}\n```\n"
                elif getattr(part, "code_execution_result", None):
                    piece += f"\n**Output:**\n```\n{part.code_execution_result.output}\n```\n"
        elif getattr(chunk, "text", None):
            piece = chunk.text
    except Exception:
        if getattr(chunk, "text", None):
            piece = chunk.text
    return piece


# ---------------- App ----------------

app = FastAPI(title="World AI Web")
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(
        os.path.join(APP_DIR, "static", "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_keys_configured": len(API_KEYS),
        "openai_keys_configured": len(OPENAI_KEYS),
        "api_keys_configured": len(API_KEYS) + len(OPENAI_KEYS),
    }


# ---------------- Auth routes ----------------

@app.post("/api/signup")
async def signup(username: str = Form(...), password: str = Form(...)):
    # Security update: ab account seedha nahi banta — pehle email/mobile OTP
    # verify karna zaroori hai. Purane accounts is change se affect nahi hote,
    # sirf naya account banane ka route yahan se badla hai.
    return JSONResponse(
        {"error": "Signup ab email ya mobile OTP verify karke hota hai. /api/signup/request-otp use karo."},
        status_code=400,
    )


@app.post("/api/signup/request-otp")
async def signup_request_otp(username: str = Form(...), password: str = Form(...),
                              method: str = Form(...), contact: str = Form(...)):
    username = username.strip()
    method = method.strip().lower()
    contact = contact.strip()

    if len(username) < 3 or len(password) < 4:
        return JSONResponse({"error": "Username kam se kam 3 aur password kam se kam 4 characters ka hona chahiye."}, status_code=400)
    if method not in ("email", "mobile"):
        return JSONResponse({"error": "Invalid verification method."}, status_code=400)
    if method == "email" and not is_valid_email(contact):
        return JSONResponse({"error": "Sahi email address daalo."}, status_code=400)
    if method == "mobile" and not is_valid_mobile(contact):
        return JSONResponse({"error": "Sahi 10-digit mobile number daalo."}, status_code=400)
    if users_col.find_one({"username": username}):
        return JSONResponse({"error": "Ye username pehle se liya ja chuka hai."}, status_code=400)

    contact_key = contact.lower() if method == "email" else re.sub(r"\D", "", contact)[-10:]

    # Resend cooldown — same contact ke liye bahut jaldi-jaldi OTP na bheja jaye.
    recent = pending_signups_col.find_one(
        {"contact_key": contact_key, "created_at": {"$gt": now_utc() - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)}}
    )
    if recent:
        wait = OTP_RESEND_COOLDOWN_SECONDS - int((now_utc() - recent["created_at"]).total_seconds())
        return JSONResponse({"error": f"Thoda ruko — {max(wait, 1)} second baad dubara try karo."}, status_code=429)

    otp = generate_otp()
    salt, pwd_hash = hash_password(password)
    signup_id = secrets.token_urlsafe(24)

    try:
        if method == "email":
            send_otp_email(contact, otp)
        else:
            send_otp_sms(contact, otp)
    except Exception as e:
        return JSONResponse({"error": f"OTP bhejne mein problem aayi: {e}"}, status_code=502)

    # Purana pending signup isi contact ke liye hata do (agar tha), phir naya banao.
    pending_signups_col.delete_many({"contact_key": contact_key})
    pending_signups_col.insert_one({
        "signup_id": signup_id,
        "username": username,
        "salt": salt,
        "password_hash": pwd_hash,
        "method": method,
        "contact": contact,
        "contact_key": contact_key,
        "otp_hash": hash_otp(otp),
        "attempts": 0,
        "created_at": now_utc(),
        "expires_at": now_utc() + timedelta(seconds=OTP_EXPIRY_SECONDS),
    })

    return {"signup_id": signup_id, "expires_in": OTP_EXPIRY_SECONDS}


@app.post("/api/signup/verify-otp")
async def signup_verify_otp(response: Response, signup_id: str = Form(...), otp: str = Form(...)):
    pending = pending_signups_col.find_one({"signup_id": signup_id})
    if not pending:
        return JSONResponse({"error": "OTP expire ho gaya ya signup request nahi mila. Dubara try karo."}, status_code=400)

    if pending["attempts"] >= OTP_MAX_ATTEMPTS:
        pending_signups_col.delete_one({"_id": pending["_id"]})
        return JSONResponse({"error": "Bahut zyada galat attempts. Dubara signup shuru karo."}, status_code=400)

    if not secrets.compare_digest(hash_otp(otp.strip()), pending["otp_hash"]):
        pending_signups_col.update_one({"_id": pending["_id"]}, {"$inc": {"attempts": 1}})
        return JSONResponse({"error": "Galat OTP. Dubara try karo."}, status_code=400)

    if users_col.find_one({"username": pending["username"]}):
        pending_signups_col.delete_one({"_id": pending["_id"]})
        return JSONResponse({"error": "Ye username abhi-abhi kisi aur ne le liya. Dusra username try karo."}, status_code=400)

    try:
        result = users_col.insert_one({
            "username": pending["username"],
            "salt": pending["salt"],
            "password_hash": pending["password_hash"],
            "verified_via": pending["method"],
            "verified_contact": pending["contact"],
            "created_at": now_utc(),
        })
        user_id = result.inserted_id
    except DuplicateKeyError:
        return JSONResponse({"error": "Ye username abhi-abhi kisi aur ne le liya. Dusra username try karo."}, status_code=400)

    pending_signups_col.delete_one({"_id": pending["_id"]})

    token = secrets.token_urlsafe(32)
    sessions_col.insert_one({"token": token, "user_id": user_id, "created_at": now_utc()})
    response.set_cookie("session_token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return {"username": pending["username"]}


@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    row = users_col.find_one({"username": username.strip()})

    if not row or not verify_password(password, row["salt"], row["password_hash"]):
        return JSONResponse({"error": "Incorrect username or password."}, status_code=400)

    token = secrets.token_urlsafe(32)
    sessions_col.insert_one({"token": token, "user_id": row["_id"], "created_at": now_utc()})

    response.set_cookie("session_token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return {"username": row["username"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        sessions_col.delete_one({"token": token})
    response.delete_cookie("session_token")
    return {"status": "logged out"}


@app.get("/api/me")
def me(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    return {"username": user["username"]}


# ---------------- Chat CRUD (logged-in users only) ----------------

@app.get("/api/chats")
def list_chats(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    # Archived chats list mein nahi dikhte (Archive option se hide ho jaate hain).
    rows = chats_col.find({"user_id": user["id"], "archived": {"$ne": True}}).sort([("pinned", -1), ("created_at", -1)])
    return [
        {
            "id": r["chat_id"], "title": r["title"], "created_at": r["created_at"],
            "pinned": r.get("pinned", False), "project_id": r.get("project_id"),
        }
        for r in rows
    ]


@app.post("/api/chats")
def create_chat(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_id = str(uuid.uuid4())
    chats_col.insert_one({
        "chat_id": chat_id,
        "user_id": user["id"],
        "title": "New chat",
        "created_at": now_utc(),
        "pinned": False,
    })
    return {"id": chat_id, "title": "New chat"}


@app.post("/api/chats/{chat_id}/pin")
def toggle_pin(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_row = chats_col.find_one({"chat_id": chat_id, "user_id": user["id"]})
    if not chat_row:
        return JSONResponse({"error": "Chat not found"}, status_code=404)
    new_pinned = not chat_row.get("pinned", False)
    chats_col.update_one({"chat_id": chat_id, "user_id": user["id"]}, {"$set": {"pinned": new_pinned}})
    return {"status": "ok", "pinned": new_pinned}


@app.post("/api/chats/{chat_id}/archive")
def toggle_archive(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_row = chats_col.find_one({"chat_id": chat_id, "user_id": user["id"]})
    if not chat_row:
        return JSONResponse({"error": "Chat not found"}, status_code=404)
    new_archived = not chat_row.get("archived", False)
    chats_col.update_one({"chat_id": chat_id, "user_id": user["id"]}, {"$set": {"archived": new_archived}})
    return {"status": "ok", "archived": new_archived}


# ---------------- Skills (saved prompt templates) ----------------

@app.get("/api/skills")
def list_skills(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    rows = skills_col.find({"user_id": user["id"]}).sort("created_at", -1)
    return [{"id": r["skill_id"], "name": r["name"], "prompt": r["prompt"]} for r in rows]


@app.post("/api/skills")
async def create_skill(request: Request, name: str = Form(...), prompt: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return unauth()
    name = name.strip()[:60]
    prompt = prompt.strip()
    if not name or not prompt:
        return JSONResponse({"error": "Name and prompt dono chahiye."}, status_code=400)
    skill_id = str(uuid.uuid4())
    skills_col.insert_one({
        "skill_id": skill_id, "user_id": user["id"], "name": name,
        "prompt": prompt, "created_at": now_utc(),
    })
    return {"id": skill_id, "name": name, "prompt": prompt}


@app.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    skills_col.delete_one({"skill_id": skill_id, "user_id": user["id"]})
    return {"status": "deleted"}


# ---------------- Projects (chat folders) ----------------

@app.get("/api/projects")
def list_projects(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    rows = projects_col.find({"user_id": user["id"]}).sort("created_at", -1)
    return [{"id": r["project_id"], "name": r["name"]} for r in rows]


@app.post("/api/projects")
async def create_project(request: Request, name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return unauth()
    name = name.strip()[:60]
    if not name:
        return JSONResponse({"error": "Project ka naam chahiye."}, status_code=400)
    project_id = str(uuid.uuid4())
    projects_col.insert_one({"project_id": project_id, "user_id": user["id"], "name": name, "created_at": now_utc()})
    return {"id": project_id, "name": name}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    projects_col.delete_one({"project_id": project_id, "user_id": user["id"]})
    chats_col.update_many({"project_id": project_id, "user_id": user["id"]}, {"$set": {"project_id": None}})
    return {"status": "deleted"}


@app.post("/api/chats/{chat_id}/project")
async def set_chat_project(chat_id: str, request: Request, project_id: str = Form(None)):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_row = chats_col.find_one({"chat_id": chat_id, "user_id": user["id"]})
    if not chat_row:
        return JSONResponse({"error": "Chat not found"}, status_code=404)
    pid = project_id if project_id else None
    chats_col.update_one({"chat_id": chat_id, "user_id": user["id"]}, {"$set": {"project_id": pid}})
    return {"status": "ok", "project_id": pid}


@app.get("/api/chats/{chat_id}/messages")
def get_messages(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_row = chats_col.find_one({"chat_id": chat_id, "user_id": user["id"]})
    if not chat_row:
        return JSONResponse({"error": "Chat not found"}, status_code=404)
    rows = messages_col.find({"chat_id": chat_id}).sort("_id", 1)
    return [
        {"role": r["role"], "text": r["text"], "created_at": r["created_at"], "files": r.get("files", [])}
        for r in rows
    ]


@app.put("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: Request, title: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return unauth()
    chats_col.update_one(
        {"chat_id": chat_id, "user_id": user["id"]},
        {"$set": {"title": title.strip()[:60] or "New chat"}},
    )
    return {"status": "ok"}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    messages_col.delete_many({"chat_id": chat_id})
    chats_col.delete_one({"chat_id": chat_id, "user_id": user["id"]})
    return {"status": "deleted"}


# ---------------- Main chat endpoint ----------------

@app.post("/api/chat")
async def chat(
    request: Request,
    message: str = Form(""),
    chat_id: str = Form(...),
    mode: str = Form("none"),
    language: str = Form("auto"),
    files: List[UploadFile] = File(default=[]),
):
    user = get_current_user(request)
    system_prompt = build_system_prompt(language)

    if not providers:
        def err_stream():
            yield f"data: {json.dumps({'error': 'No AI API key (Gemini/OpenAI) is configured on the server.'})}\n\n"
        return StreamingResponse(err_stream(), media_type="text/event-stream")

    text = (message or "").strip()
    real_files = [f for f in files if f and f.filename]

    if not text and not real_files:
        def empty_stream():
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")

    chat_row = None
    if user:
        chat_row = chats_col.find_one({"chat_id": chat_id, "user_id": user["id"]})
        if not chat_row:
            def nf_stream():
                yield f"data: {json.dumps({'error': 'Chat not found.'})}\n\n"
            return StreamingResponse(nf_stream(), media_type="text/event-stream")

        if DAILY_MESSAGE_LIMIT > 0:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            user_chat_ids = [c["chat_id"] for c in chats_col.find({"user_id": user["id"]}, {"chat_id": 1})]
            count = messages_col.count_documents({
                "chat_id": {"$in": user_chat_ids},
                "role": "user",
                "created_at": {"$gte": today_start},
            })
            if count >= DAILY_MESSAGE_LIMIT:
                def limit_stream():
                    yield f"data: {json.dumps({'error': f'You have reached the daily limit of {DAILY_MESSAGE_LIMIT} messages. Please try again tomorrow.'})}\n\n"
                return StreamingResponse(limit_stream(), media_type="text/event-stream")

    file_parts = []
    previews = []
    for f in real_files:
        data = await f.read()
        part, preview = build_file_part(f.filename, f.content_type, data)
        file_parts.append(part)
        if preview:
            previews.append(preview)

    display_text = text or "Attached file(s) ka data analyze karo."
    attached_filenames = [f.filename for f in real_files]

    if user:
        history_rows = messages_col.find({"chat_id": chat_id}).sort("_id", 1)
        history = [{"role": r["role"], "text": r["text"]} for r in history_rows]
    else:
        history = guest_sessions.setdefault(chat_id, [])

    contents = []
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=m["text"])]))

    current_parts = list(file_parts) + [types.Part.from_text(text=display_text)]
    contents.append(types.Content(role="user", parts=current_parts))

    if user:
        messages_col.insert_one({
            "chat_id": chat_id, "role": "user", "text": display_text,
            "files": attached_filenames, "created_at": now_utc(),
        })
        if chat_row["title"] == "New chat":
            chats_col.update_one({"chat_id": chat_id}, {"$set": {"title": display_text[:40]}})
    else:
        history.append({"role": "user", "text": display_text, "files": attached_filenames})

    tools = get_tools(mode)

    def save_ai_message(final_text):
        if user:
            messages_col.insert_one({"chat_id": chat_id, "role": "ai", "text": final_text, "created_at": now_utc()})
        else:
            history.append({"role": "ai", "text": final_text})

    def event_stream():
        full_text = ""
        yielded_any = False
        last_err = None
        last_chunk = None

        # File attachments aur tools (search/code) sirf Gemini format mein
        # support hain — is case mein sirf Gemini keys hi pool mein rakho.
        if tools or file_parts:
            usable_providers = [p for p in providers if p["type"] == "gemini"]
        else:
            usable_providers = providers

        start_idx = next_start_index(len(usable_providers))

        try:
            if previews:
                yield f"data: {json.dumps({'previews': previews})}\n\n"

            if not usable_providers:
                yield f"data: {json.dumps({'error': 'This feature (file/search/code) currently only works with Gemini, and no Gemini key is configured.'})}\n\n"
                return

            config_kwargs = dict(
                system_instruction=system_prompt,
                max_output_tokens=1500 if mode == "code" else 800,
                temperature=0.7,
            )
            if tools:
                config_kwargs["tools"] = tools

            for attempt in range(len(usable_providers)):
                provider = usable_providers[(start_idx + attempt) % len(usable_providers)]
                try:
                    if provider["type"] == "gemini":
                        stream = provider["client"].models.generate_content_stream(
                            model=MODEL,
                            contents=contents,
                            config=types.GenerateContentConfig(**config_kwargs),
                        )
                        for chunk in stream:
                            last_chunk = chunk
                            piece = extract_piece(chunk)
                            if piece:
                                full_text += piece
                                yielded_any = True
                                yield f"data: {json.dumps({'chunk': piece})}\n\n"
                    else:  # openai
                        for piece in stream_openai_text(
                            provider["client"], contents, system_prompt, config_kwargs["max_output_tokens"]
                        ):
                            full_text += piece
                            yielded_any = True
                            yield f"data: {json.dumps({'chunk': piece})}\n\n"
                    last_err = None
                    break  # success
                except Exception as e:
                    last_err = e
                    if is_quota_error(e) and not yielded_any:
                        continue  # try next key/provider
                    else:
                        break  # real error, or partial output already sent — don't retry

            if last_err:
                if is_quota_error(last_err):
                    yield f"data: {json.dumps({'error': 'All configured API keys have run out of quota. Please try again later.'})}\n\n"
                else:
                    yield f"data: {json.dumps({'error': str(last_err)})}\n\n"
                return

            try:
                if mode == "search" and last_chunk and last_chunk.candidates and last_chunk.candidates[0].grounding_metadata:
                    gm = last_chunk.candidates[0].grounding_metadata
                    sources = []
                    for gc in (gm.grounding_chunks or []):
                        if getattr(gc, "web", None) and gc.web.uri:
                            sources.append((gc.web.title or gc.web.uri, gc.web.uri))
                    if sources:
                        src_text = "\n\n**Sources:**\n" + "\n".join(f"- [{t}]({u})" for t, u in sources[:6])
                        full_text += src_text
                        yield f"data: {json.dumps({'chunk': src_text})}\n\n"
            except Exception:
                pass

            save_ai_message(full_text)
            yield f"data: {json.dumps({'done': True})}\n\n"
        except GeneratorExit:
            if full_text:
                save_ai_message(full_text + "\n\n[User ne rok diya]")
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
