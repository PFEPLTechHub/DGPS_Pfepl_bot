import os
import hmac
import json
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import mysql.connector
from mysql.connector import pooling

# ------------------ Config & Logging ------------------
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")  # used for Telegram WebApp initData verification
MYSQL_HOST  = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT  = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB    = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER  = os.getenv("MYSQL_USER", "root")
MYSQL_PASS  = os.getenv("MYSQL_PASS", "")

WEBAPP_DIR  = os.path.join(os.path.dirname(__file__), "webapp")

logger = logging.getLogger("report_webapp")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(handler)

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is empty; /api/verify will always fail.")

# ------------------ DB Pool ------------------
cnxpool = pooling.MySQLConnectionPool(
    pool_name="reportpool",
    pool_size=5,
    host=MYSQL_HOST,
    port=MYSQL_PORT,
    database=MYSQL_DB,
    user=MYSQL_USER,
    password=MYSQL_PASS,
    autocommit=True,
)

def db_conn():
    return cnxpool.get_connection()

# ------------------ Telegram WebApp verify ------------------
def _get_secret_key(bot_token: str) -> bytes:
    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> dict:
    """
    Validates Telegram WebApp initData. Returns parsed dict including 'user' (dict).
    Raises HTTPException if invalid.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in pairs:
            raise HTTPException(status_code=401, detail="Missing hash")
        received_hash = pairs.pop("hash")

        # Build data_check_string (keys sorted, 'k=v' joined by \n)
        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))

        secret_key = _get_secret_key(bot_token)
        computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            raise HTTPException(status_code=401, detail="Invalid initData signature")

        # Expiry check
        if "auth_date" in pairs:
            try:
                auth_ts = int(pairs["auth_date"])
                age = int(datetime.now(timezone.utc).timestamp()) - auth_ts
                if age > max_age_sec:
                    raise HTTPException(status_code=401, detail="initData expired")
            except ValueError:
                raise HTTPException(status_code=401, detail="Invalid auth_date")

        # Parse 'user'
        user_raw = pairs.get("user")
        user = json.loads(user_raw) if user_raw else None
        if not user or "id" not in user:
            raise HTTPException(status_code=401, detail="Missing user")

        pairs["user"] = user
        return pairs
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("verify_init_data failed")
        raise HTTPException(status_code=401, detail=f"initData parse error: {e}")

# ------------------ FastAPI app ------------------
app = FastAPI(title="Report WebApp Shell", version="1.0")

# Serve static WebApp
if not os.path.isdir(WEBAPP_DIR):
    os.makedirs(WEBAPP_DIR, exist_ok=True)
app.mount("/webapp", StaticFiles(directory=WEBAPP_DIR), name="webapp")

@app.get("/")
async def root_index():
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return JSONResponse({"ok": True, "message": "Place your WebApp in /webapp/index.html"})

@app.get("/api/health")
async def health():
    return {"ok": True, "service": "webapp", "db": MYSQL_DB}

# Verify the WebApp session & that the user is an ACTIVE EMPLOYEE (role=2)
@app.post("/api/verify")
async def api_verify(req: Request):
    body = await req.json()
    init_data = body.get("init_data")
    verified = verify_init_data(init_data, BOT_TOKEN)

    tg_id = int(verified["user"]["id"])
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT id, role, is_active, first_name, last_name FROM users WHERE telegram_id=%s", (tg_id,))
        u = cur.fetchone()

    if not u:
        raise HTTPException(status_code=403, detail="User not found in system")
    if u["role"] != 2 or u["is_active"] != 1:
        raise HTTPException(status_code=403, detail="Only active employees can use this WebApp")

    full_name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
    return {"ok": True, "telegram_id": tg_id, "name": full_name}

# Masters for dropdowns
@app.get("/api/masters")
async def get_masters():
    try:
        with db_conn() as conn, conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT id, name FROM master_sites WHERE is_active=1 ORDER BY name")
            sites = cur.fetchall()
            cur.execute("SELECT id, name FROM master_drones WHERE is_active=1 ORDER BY name")
            drones = cur.fetchall()
        return {"ok": True, "sites": sites, "drones": drones}
    except Exception as e:
        logger.exception("masters failed")
        raise HTTPException(status_code=500, detail=str(e))
