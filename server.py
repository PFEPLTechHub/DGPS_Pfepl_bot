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
from mysql.connector import pooling, errors as mysql_errors

# ------------------ Config & Logging ------------------
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")  # required by /api/verify and /api/reports
MYSQL_HOST  = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT  = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB    = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER  = os.getenv("MYSQL_USER", "root")
MYSQL_PASS  = os.getenv("MYSQL_PASS", "")
WEBAPP_DIR  = os.path.join(os.path.dirname(__file__), "webapp")

logger = logging.getLogger("report_webapp")
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(_sh)

if not os.path.isdir(WEBAPP_DIR):
    os.makedirs(WEBAPP_DIR, exist_ok=True)

# ------------------ DB Pool ------------------
def create_pool():
    return pooling.MySQLConnectionPool(
        pool_name="reportpool",
        pool_size=5,
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DB,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        autocommit=True,
    )

try:
    cnxpool = create_pool()
except Exception as e:
    cnxpool = None
    logger.error("MySQL pool creation failed: %s", e)

def db_conn():
    if cnxpool is None:
        raise HTTPException(status_code=500, detail="DB pool not initialized")
    return cnxpool.get_connection()

# ------------------ Telegram WebApp verify ------------------
def _get_secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in pairs:
            raise HTTPException(status_code=401, detail="Missing hash")
        received_hash = pairs.pop("hash")

        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
        secret_key = _get_secret_key(bot_token)
        computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            raise HTTPException(status_code=401, detail="Invalid initData signature")

        if "auth_date" in pairs:
            auth_ts = int(pairs["auth_date"])
            age = int(datetime.now(timezone.utc).timestamp()) - auth_ts
            if age > max_age_sec:
                raise HTTPException(status_code=401, detail="initData expired")

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
app = FastAPI(title="Report WebApp", version="1.0")

# Serve static WebApp
app.mount("/webapp", StaticFiles(directory=WEBAPP_DIR), name="webapp")

@app.on_event("startup")
async def startup_log():
    logger.info("=== WebApp starting ===")
    logger.info("BOT_TOKEN set: %s", "YES" if BOT_TOKEN else "NO (verify will fail)")
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    logger.info("WEBAPP_DIR: %s (index.html: %s)", WEBAPP_DIR, "FOUND" if os.path.isfile(index_path) else "MISSING")

    # Light DB ping
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        logger.info("DB: OK (connected to %s:%s/%s)", MYSQL_HOST, MYSQL_PORT, MYSQL_DB)
    except Exception as e:
        logger.error("DB ping failed: %s", e)

@app.get("/")
async def root_index():
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return JSONResponse({"ok": True, "message": "Place your WebApp at /webapp/index.html"})

@app.get("/api/health")
async def health():
    return {"ok": True, "service": "webapp", "db": MYSQL_DB}

# Verify session & that the user is an ACTIVE EMPLOYEE (role=2)
@app.post("/api/verify")
async def api_verify(req: Request):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")
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

# Create report (writes to reports + report_flights)
@app.post("/api/reports")
async def create_report(req: Request):
    """
    Body:
    {
      "init_data": "<tg webapp initData>",
      "report": {
        "report_date": "YYYY-MM-DD",
        "site_id": 1,
        "drone_id": 2,
        "base_height_m": 12.5,
        "pilot_name": "A",
        "copilot_name": "B",
        "dgps_used": ["R4S - 314","DA2 - 739"],
        "dgps_operators": ["Alice","Bob"],
        "grid_numbers": ["H43R12A17","H43R12A22"],
        "gcp_points": ["CP1","CP2"],
        "remark": "text"
      },
      "flights": [
        {"flight_time_min": 12, "area_sq_km": 0.12, "uav_rover_file": "X", "drone_base_file_no": "Y"},
        ...
      ]
    }
    """
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    body = await req.json()
    init_data = body.get("init_data", "")
    verified = verify_init_data(init_data, BOT_TOKEN)
    tg_id = int(verified["user"]["id"])

    payload = body.get("report") or {}
    flights = body.get("flights") or []

    # Basic validation
    required_fields = [
        "report_date", "site_id", "drone_id", "base_height_m",
        "pilot_name", "copilot_name", "dgps_used", "dgps_operators",
        "grid_numbers", "gcp_points", "remark"
    ]
    missing = [f for f in required_fields if f not in payload]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {', '.join(missing)}")
    if not isinstance(flights, list) or len(flights) == 0:
        raise HTTPException(status_code=400, detail="At least one flight required")

    # Ensure user is active employee
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT role, is_active FROM users WHERE telegram_id=%s", (tg_id,))
        u = cur.fetchone()
    if not u or u["role"] != 2 or u["is_active"] != 1:
        raise HTTPException(status_code=403, detail="Only active employees can submit reports")

    # Insert
    try:
        with db_conn() as conn:
            conn.start_transaction()
            with conn.cursor() as cur:
                # reports (NOTE: employee_telegram_id column name)
                cur.execute(
                    """
                    INSERT INTO reports (
                        employee_telegram_id, report_date, site_id, drone_id, base_height_m,
                        pilot_name, copilot_name,
                        dgps_used_json, dgps_operators_json, grid_numbers_json, gcp_points_json,
                        remark
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        tg_id,
                        payload["report_date"],
                        int(payload["site_id"]),
                        int(payload["drone_id"]),
                        float(payload["base_height_m"]),
                        payload["pilot_name"].strip(),
                        payload["copilot_name"].strip(),
                        json.dumps(payload["dgps_used"], ensure_ascii=False),
                        json.dumps(payload["dgps_operators"], ensure_ascii=False),
                        json.dumps(payload["grid_numbers"], ensure_ascii=False),
                        json.dumps(payload["gcp_points"], ensure_ascii=False),
                        payload["remark"].strip(),
                    ),
                )
                report_id = cur.lastrowid

                # report_flights
                for f in flights:
                    cur.execute(
                        """
                        INSERT INTO report_flights
                        (report_id, flight_time_min, area_sq_km, uav_rover_file, drone_base_file_no)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        (
                            report_id,
                            int(f["flight_time_min"]),
                            float(f["area_sq_km"]),
                            f["uav_rover_file"].strip(),
                            f["drone_base_file_no"].strip(),
                        ),
                    )
            conn.commit()

        return {"ok": True, "report_id": report_id}

    except mysql_errors.IntegrityError as e:
        # Duplicate (unique uq_emp_date) → 409 Conflict
        if getattr(e, "errno", None) == 1062:
            logger.warning("Duplicate report (same employee & date) | tg=%s date=%s", tg_id, payload["report_date"])
            raise HTTPException(status_code=409, detail="You already submitted a report for this date.")
        logger.exception("create_report integrity error")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.exception("create_report failed")
        raise HTTPException(status_code=500, detail=str(e))

# ------------------ Run with `python server.py` ------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting Uvicorn on 0.0.0.0:%s", port)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
