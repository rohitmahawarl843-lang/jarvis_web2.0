"""
Jarvis AI — Web Version
Login optional (guests get no persistence). Supports file analysis,
optional Web Search tool, optional Code Execution tool, and
MULTIPLE Gemini API keys with automatic fallback on quota errors.
"""
import os
import io
import json
import uuid
import secrets
import hashlib
import sqlite3
import mimetypes
import itertools
import threading
from contextlib import contextmanager
from typing import List

from fastapi import FastAPI, Request, Response, Form, File, UploadFile
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from google import genai
from google.genai import types

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "jarvis.db")

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

SYSTEM_PROMPT = """You are Jarvis, a friendly AI assistant.
You speak in Hinglish naturally. Be helpful and concise.
Agar user koi file (image, video, PDF, spreadsheet, document) attach kare,
uska data dhyan se dekho/padho aur uske sawaal ka sahi jawab do ya data ka
summary/analysis do.
Agar Web Search tool available hai, current/real-time info (news, weather,
prices) ke liye zaroor use karo. Agar Code Execution tool available hai,
code ko actually chala kar result verify karo, sirf likh kar mat do."""

guest_sessions = {}

# ---------------- Database ----------------

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db_conn() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        db.commit()

init_db()

# ---------------- Auth helpers ----------------

def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return salt, pwd_hash


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, computed = hash_password(password, salt)
    return secrets.compare_digest(computed, expected_hash)


def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    with db_conn() as db:
        row = db.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
            (token,),
        ).fetchone()
    return row


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

app = FastAPI(title="Jarvis AI Web")
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"))


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
async def signup(response: Response, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if len(username) < 3 or len(password) < 4:
        return JSONResponse({"error": "Username kam se kam 3 aur password kam se kam 4 characters ka ho."}, status_code=400)

    salt, pwd_hash = hash_password(password)
    try:
        with db_conn() as db:
            db.execute("INSERT INTO users (username, salt, password_hash) VALUES (?, ?, ?)", (username, salt, pwd_hash))
            db.commit()
            user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "Ye username pehle se liya gaya hai."}, status_code=400)

    token = secrets.token_urlsafe(32)
    with db_conn() as db:
        db.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        db.commit()

    response.set_cookie("session_token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return {"username": username}


@app.post("/api/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    with db_conn() as db:
        row = db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()

    if not row or not verify_password(password, row["salt"], row["password_hash"]):
        return JSONResponse({"error": "Username ya password galat hai."}, status_code=400)

    token = secrets.token_urlsafe(32)
    with db_conn() as db:
        db.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, row["id"]))
        db.commit()

    response.set_cookie("session_token", token, httponly=True, max_age=60 * 60 * 24 * 30, samesite="lax")
    return {"username": row["username"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        with db_conn() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (token,))
            db.commit()
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
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, title, created_at FROM chats WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/chats")
def create_chat(request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    chat_id = str(uuid.uuid4())
    with db_conn() as db:
        db.execute("INSERT INTO chats (id, user_id, title) VALUES (?, ?, ?)", (chat_id, user["id"], "New chat"))
        db.commit()
    return {"id": chat_id, "title": "New chat"}


@app.get("/api/chats/{chat_id}/messages")
def get_messages(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    with db_conn() as db:
        chat_row = db.execute("SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"])).fetchone()
        if not chat_row:
            return JSONResponse({"error": "Chat not found"}, status_code=404)
        rows = db.execute(
            "SELECT role, text, created_at FROM messages WHERE chat_id=? ORDER BY id ASC", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.put("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: Request, title: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return unauth()
    with db_conn() as db:
        db.execute("UPDATE chats SET title=? WHERE id=? AND user_id=?", (title.strip()[:60] or "New chat", chat_id, user["id"]))
        db.commit()
    return {"status": "ok"}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return unauth()
    with db_conn() as db:
        db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"]))
        db.commit()
    return {"status": "deleted"}


# ---------------- Main chat endpoint ----------------

@app.post("/api/chat")
async def chat(
    request: Request,
    message: str = Form(""),
    chat_id: str = Form(...),
    mode: str = Form("none"),
    files: List[UploadFile] = File(default=[]),
):
    user = get_current_user(request)

    if not providers:
        def err_stream():
            yield f"data: {json.dumps({'error': 'Server par koi bhi AI API key (Gemini/OpenAI) set nahi hai.'})}\n\n"
        return StreamingResponse(err_stream(), media_type="text/event-stream")

    text = (message or "").strip()
    real_files = [f for f in files if f and f.filename]

    if not text and not real_files:
        def empty_stream():
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")

    chat_row = None
    if user:
        with db_conn() as db:
            chat_row = db.execute("SELECT * FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"])).fetchone()
        if not chat_row:
            def nf_stream():
                yield f"data: {json.dumps({'error': 'Chat nahi mila.'})}\n\n"
            return StreamingResponse(nf_stream(), media_type="text/event-stream")

        if DAILY_MESSAGE_LIMIT > 0:
            with db_conn() as db:
                count = db.execute(
                    """SELECT COUNT(*) c FROM messages m JOIN chats c2 ON m.chat_id = c2.id
                       WHERE c2.user_id=? AND m.role='user' AND date(m.created_at) = date('now')""",
                    (user["id"],),
                ).fetchone()["c"]
            if count >= DAILY_MESSAGE_LIMIT:
                def limit_stream():
                    yield f"data: {json.dumps({'error': f'Aaj ka {DAILY_MESSAGE_LIMIT} messages ka limit khatam ho gaya. Kal try karo.'})}\n\n"
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

    if user:
        with db_conn() as db:
            history_rows = db.execute(
                "SELECT role, text FROM messages WHERE chat_id=? ORDER BY id ASC", (chat_id,)
            ).fetchall()
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
        with db_conn() as db:
            db.execute("INSERT INTO messages (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, "user", display_text))
            if chat_row["title"] == "New chat":
                db.execute("UPDATE chats SET title=? WHERE id=?", (display_text[:40], chat_id))
            db.commit()
    else:
        history.append({"role": "user", "text": display_text})

    tools = get_tools(mode)

    def save_ai_message(final_text):
        if user:
            with db_conn() as db:
                db.execute("INSERT INTO messages (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, "ai", final_text))
                db.commit()
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
                yield f"data: {json.dumps({'error': 'Yeh feature (file/search/code) abhi sirf Gemini ke saath kaam karta hai, aur koi Gemini key configured nahi hai.'})}\n\n"
                return

            config_kwargs = dict(
                system_instruction=SYSTEM_PROMPT,
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
                            provider["client"], contents, SYSTEM_PROMPT, config_kwargs["max_output_tokens"]
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
                    yield f"data: {json.dumps({'error': 'Sab configured API keys ki quota abhi khatam hai. Thodi der baad try karo.'})}\n\n"
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
