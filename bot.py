# bot.py
import os
import re
import uuid
import logging
import asyncio
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from mysql.connector import pooling, errors as mysql_errors

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, RetryAfter, NetworkError

# ----------------- Logging -----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("staffbot")

# ----------------- Load config -----------------
load_dotenv()
BOT_TOKEN    = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without '@'

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB   = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS", "")

INVITE_DAYS_VALID = int(os.getenv("INVITE_DAYS_VALID", "1"))
WEBAPP_URL        = os.getenv("WEBAPP_URL", "").strip()

# Timezones
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# ----------------- DB Pool -----------------
dbconfig = {
    "host": MYSQL_HOST,
    "port": MYSQL_PORT,
    "database": MYSQL_DB,
    "user": MYSQL_USER,
    "password": MYSQL_PASS,
    "autocommit": True,
}
cnxpool = pooling.MySQLConnectionPool(pool_name="staffpool", pool_size=5, **dbconfig)

def db_conn():
    return cnxpool.get_connection()

# ----------------- Constants -----------------
ROLE_ADMIN    = 0
ROLE_MANAGER  = 1
ROLE_EMPLOYEE = 2

# Conversation states
ASK_FIRST, ASK_LAST, ASK_PHONE = range(3)
ASK_MGR_LOGIN, ASK_MGR_PASS = range(3, 5)  # <-- NEW (3,4)

# Masters text-entry states
MASTERS_ADD_NAME, MASTERS_RENAME_NAME = range(100, 102)

# ----------------- DB helpers -----------------
def get_user_by_tg(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE telegram_id=%s", (telegram_id,))
        return cur.fetchone()

def get_user_by_id(user_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        return cur.fetchone()

def user_active_by_tg(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE telegram_id=%s AND is_active=1", (telegram_id,))
        return cur.fetchone()

def is_admin(telegram_id):
    u = get_user_by_tg(telegram_id)
    return bool(u and u["role"] == ROLE_ADMIN and u["is_active"] == 1)

def is_manager(telegram_id):
    u = get_user_by_tg(telegram_id)
    return bool(u and u["role"] == ROLE_MANAGER and u["is_active"] == 1)

def has_staff_privileges(telegram_id):
    u = get_user_by_tg(telegram_id)
    return bool(u and u["is_active"] == 1 and u["role"] in (ROLE_ADMIN, ROLE_MANAGER))

def get_staff_record(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT * FROM users WHERE telegram_id=%s AND is_active=1 AND role IN (%s,%s)",
            (telegram_id, ROLE_ADMIN, ROLE_MANAGER),
        )
        return cur.fetchone()

def manager_login_by_tg(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT * FROM manager_logins WHERE telegram_id=%s ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        )
        return cur.fetchone()

def is_login_taken(login_id: str) -> bool:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM manager_logins WHERE login=%s LIMIT 1", (login_id,))
        return cur.fetchone() is not None

def create_manager_login(login_id: str, password: str, telegram_id: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO manager_logins (login, password, telegram_id, is_active) VALUES (%s,%s,%s,1)",
            (login_id.strip(), password, telegram_id),
        )

def create_invitation(manager_id, invite_role=ROLE_EMPLOYEE):
    token = str(uuid.uuid4())
    expires_at_utc = datetime.now(UTC) + timedelta(days=INVITE_DAYS_VALID)
    expires_at_db = expires_at_utc.replace(tzinfo=None)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invitations (token, manager_id, invite_role, expires_at, status) "
            "VALUES (%s,%s,%s,%s,'pending')",
            (token, manager_id, invite_role, expires_at_db),
        )
    logger.info("Invitation created | token=%s manager_id=%s role=%s expires_at_utc=%s",
                token, manager_id, invite_role, expires_at_utc)
    return token, expires_at_utc

def get_invitation(token):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE token=%s", (token,))
        return cur.fetchone()

def mark_invitation_used(inv_id, user_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE invitations SET status='used', used_at=UTC_TIMESTAMP(), redeemed_by_user_id=%s WHERE id=%s",
            (user_id, inv_id),
        )

# ---- join_requests ----
def find_pending_request(telegram_id, invitation_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT * FROM join_requests WHERE telegram_id=%s AND invitation_id=%s AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (telegram_id, invitation_id),
        )
        return cur.fetchone()

def create_join_request_min(telegram_id, username, manager_id, invite_role, invitation_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO join_requests "
            "(telegram_id, username, manager_id, invite_role, invitation_id, status) "
            "VALUES (%s,%s,%s,%s,%s,'pending')",
            (telegram_id, username, manager_id, invite_role, invitation_id),
        )
        return cur.lastrowid

def get_join_request(jr_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM join_requests WHERE id=%s", (jr_id,))
        return cur.fetchone()

def get_pending_join_requests(manager_id, limit=25):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT * FROM join_requests WHERE manager_id=%s AND status='pending' "
            "ORDER BY created_at ASC LIMIT %s",
            (manager_id, limit),
        )
        return cur.fetchall()

def update_join_request_status(jr_id, status, decided_by):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE join_requests SET status=%s, decided_at=UTC_TIMESTAMP(), decided_by=%s WHERE id=%s",
            (status, decided_by, jr_id),
        )

def set_join_profile_field(jr_id, field, value):
    if field not in ("first_name", "last_name", "phone"):
        return
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE join_requests SET {field}=%s WHERE id=%s", (value, jr_id))

# ---- employees list / deactivate ----
def list_employees(manager_id, limit=25):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT id, telegram_id, username, first_name, last_name, phone, is_active "
            "FROM users WHERE role=%s AND manager_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (ROLE_EMPLOYEE, manager_id, limit),
        )
        return cur.fetchall()

def list_active_employees(manager_id, limit=50):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT id, first_name, last_name FROM users "
            "WHERE role=%s AND manager_id=%s AND is_active=1 "
            "ORDER BY first_name, last_name, id LIMIT %s",
            (ROLE_EMPLOYEE, manager_id, limit),
        )
        return cur.fetchall()

def deactivate_employee(user_id, manager_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_active=0 WHERE id=%s AND role=%s AND manager_id=%s",
            (user_id, ROLE_EMPLOYEE, manager_id),
        )
        return cur.rowcount > 0

def get_telegram_id_by_user_row_id(row_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT telegram_id FROM users WHERE id=%s", (row_id,))
        r = cur.fetchone()
        return r[0] if r else None

# -------- Masters (Sites & Drones) helpers --------
def _table_for(kind: str) -> str:
    return "master_sites" if kind == "sites" else "master_drones"

def _label_for(kind: str) -> str:
    return "Site" if kind == "sites" else "Drone"

def masters_list(kind: str):
    table = _table_for(kind)
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(f"SELECT id, name, is_active FROM {table} ORDER BY name")
        return cur.fetchall()

def masters_add(kind: str, name: str):
    table = _table_for(kind)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO {table} (name, is_active) VALUES (%s, 1)", (name.strip(),))
        return cur.lastrowid

def masters_rename(kind: str, rec_id: int, new_name: str):
    table = _table_for(kind)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET name=%s WHERE id=%s", (new_name.strip(), rec_id))
        return cur.rowcount > 0

def masters_toggle(kind: str, rec_id: int):
    table = _table_for(kind)
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(f"SELECT is_active FROM {table} WHERE id=%s", (rec_id,))
        row = cur.fetchone()
        if not row:
            return False, None
        new_val = 0 if row["is_active"] == 1 else 1
        cur.execute(f"UPDATE {table} SET is_active=%s WHERE id=%s", (new_val, rec_id))
        return True, new_val

def masters_delete(kind: str, rec_id: int):
    table = _table_for(kind)
    with db_conn() as conn, conn.cursor() as cur:
        try:
            cur.execute(f"DELETE FROM {table} WHERE id=%s", (rec_id,))
            return True, None
        except mysql_errors.IntegrityError as e:
            # In use by reports (FK restriction)
            return False, e

# ----------------- Network-safe sending helpers -----------------
async def safe_send_message(bot, chat_id, text, retries=2, **kwargs):
    attempt = 0
    while True:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            wait_s = int(getattr(e, "retry_after", 2)) + 1
            logger.warning("RetryAfter when send_message | wait=%ss", wait_s)
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError) as e:
            attempt += 1
            logger.warning("Send message timeout/network error (attempt %s/%s): %s", attempt, retries, e)
            if attempt > retries:
                raise
            await asyncio.sleep(2)

async def reply_text_safe(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    if update.message:
        try:
            return await update.message.reply_text(text, **kwargs)
        except (RetryAfter, TimedOut, NetworkError):
            pass
    return await safe_send_message(context.bot, update.effective_chat.id, text, **kwargs)

# ----------------- UI helpers -----------------
def render_main_menu(telegram_id: int):
    u = get_user_by_tg(telegram_id)
    if not u:
        text = (
            "Welcome! If you’re an employee, please join via your invite link.\n"
            "If Start didn’t work, send your invite code using /use <code>."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Help", callback_data="main:help")]])
        return text, kb

    if u["is_active"] == 1 and u["role"] in (ROLE_ADMIN, ROLE_MANAGER):
        label = "Admin" if u["role"] == ROLE_ADMIN else "Manager"
        text = f"{label} menu — choose an option:"
        rows = [
            [InlineKeyboardButton("⚙️ Manage Users", callback_data="mgr:panel")],
            [InlineKeyboardButton("🧩 Masters (Sites & Drones)", callback_data="mgr:masters")],
        ]
        # Managers see Profile (admins don’t)
        if u["role"] == ROLE_MANAGER:
            rows.insert(1, [InlineKeyboardButton("👤 Profile", callback_data="mgr:profile")])

        rows.append([InlineKeyboardButton("ℹ️ Help", callback_data="main:help")])
        kb = InlineKeyboardMarkup(rows)
        return text, kb


    if u["is_active"] == 1 and u["role"] == ROLE_EMPLOYEE:
        text = "Employee menu — choose an option:"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Submit Report", callback_data="emp:report")],
                [InlineKeyboardButton("ℹ️ Help", callback_data="main:help")],
            ]
        )
    else:
        text = "Your account is pending manager approval."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Help", callback_data="main:help")]])
    return text, kb

async def show_main_menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = render_main_menu(update.effective_user.id)
    await reply_text_safe(update, context, text, reply_markup=kb)

def fmt_ist(dt_utc: datetime) -> str:
    return dt_utc.astimezone(IST).strftime("%d %b %Y %I:%M %p IST")

def human_left(dt_utc: datetime) -> str:
    delta = dt_utc - datetime.now(UTC)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expired"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m and not d: parts.append(f"{m}m")
    return "in " + " ".join(parts) if parts else "in <1m"

def token_expired(inv_row) -> bool:
    return datetime.now(UTC) > inv_row["expires_at"].replace(tzinfo=UTC)

# ----------------- Token intake -----------------
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

async def open_request_with_token(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    try:
        inv = get_invitation(token)
        if not inv or inv["status"] not in ("pending",):
            await reply_text_safe(update, context, "Invalid or inactive invite. Ask your manager/admin for a new link.")
            return

        if token_expired(inv):
            msg = f"Invite expired (IST: {fmt_ist(inv['expires_at'].replace(tzinfo=UTC))}). Ask for a new link."
            await reply_text_safe(update, context, msg)
            return

        tg = update.effective_user
        existing = find_pending_request(tg.id, inv["id"])
        if existing:
            jr_id = existing["id"]
        else:
            jr_id = create_join_request_min(
                telegram_id=tg.id,
                username=tg.username,
                manager_id=inv["manager_id"],
                invite_role=inv["invite_role"],
                invitation_id=inv["id"],
            )

        await reply_text_safe(update, context, "Request sent. You’ll be notified when it’s approved.")

        mgr_tg = get_telegram_id_by_user_row_id(inv["manager_id"])
        created_naive = get_join_request(jr_id)["created_at"]
        deadline = inv["expires_at"].replace(tzinfo=UTC)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"jr:approve:{jr_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"jr:reject:{jr_id}"),
                ],
                [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
            ]
        )
        role_txt = "as MANAGER" if inv["invite_role"] == ROLE_MANAGER else "as EMPLOYEE"
        try:
            await safe_send_message(
                context.bot,
                chat_id=mgr_tg,
                text=(
                    f"New join request (#{jr_id}) {role_txt}\n"
                    f"tg: {tg.id}  (@{tg.username or '-'})\n"
                    f"Requested: {fmt_ist(created_naive.replace(tzinfo=UTC))}\n"
                    f"Expires: {fmt_ist(deadline)} ({human_left(deadline)})"
                ),
                reply_markup=kb,
            )
        except Exception:
            pass

    except Exception:
        await reply_text_safe(update, context, "Something went wrong. Please try again.")

# ----------------- Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        token = args[0].strip()
        if UUID_RE.match(token):
            await open_request_with_token(update, context, token)
            return
    u = get_user_by_tg(update.effective_user.id)
    if u:
        await show_main_menu_message(update, context)
    else:
        await reply_text_safe(
            update, context,
            "Hi! To join as an employee/manager, please use your invite link.\n\n"
            "If Start didn’t ask you, send your invite code using:\n"
            "/use <paste-your-code>"
        )

async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply_text_safe(update, context, "Usage: /use <invite-code>")
        return
    token = context.args[0].strip()
    if not UUID_RE.match(token):
        await reply_text_safe(update, context, "That doesn't look like a valid invite code.")
        return
    await open_request_with_token(update, context, token)

async def detect_uuid_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if UUID_RE.match(txt):
        await open_request_with_token(update, context, txt)
        return
    await show_main_menu_message(update, context)

# -------- Manager/Admin: Approve/Reject join request --------
async def join_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not has_staff_privileges(update.effective_user.id):
        await query.edit_message_text("Only managers/admins can perform this action.")
        return

    _, action, jr_id_s = query.data.split(":")
    jr_id = int(jr_id_s)
    actor = get_staff_record(update.effective_user.id)
    jr = get_join_request(jr_id)

    if not jr or jr["manager_id"] != actor["id"] or jr["status"] != "pending":
        await query.edit_message_text("This request is no longer pending.")
        return

    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE id=%s", (jr["invitation_id"],))
        inv = cur.fetchone()
    if not inv or token_expired(inv):
        update_join_request_status(jr_id, "rejected", decided_by=actor["id"])
        await query.edit_message_text(
            "Invitation expired. Ask the user to use a fresh invite.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ New Invite", callback_data="mgr:invite")],
                    [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
                ]
            ),
        )
        return

    if action == "approve":
        update_join_request_status(jr_id, "approved", decided_by=actor["id"])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧾 Start Profile", callback_data=f"prof:start:{jr_id}")]])
        try:
            await safe_send_message(
                context.bot,
                chat_id=jr["telegram_id"],
                text="Your request has been approved. Please complete your profile to finish joining.",
                reply_markup=kb,
            )
        except Exception:
            pass

        await query.edit_message_text(
            "Approved. The user has been asked to complete their profile.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]),
        )
    else:
        update_join_request_status(jr_id, "rejected", decided_by=actor["id"])
        try:
            await safe_send_message(context.bot, chat_id=jr["telegram_id"], text="Your join request was rejected.")
        except Exception:
            pass
        await query.edit_message_text(
            "Rejected.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]),
        )

# -------- Profile flow (after approval) --------
async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, _, jr_id_s = query.data.split(":")
    jr_id = int(jr_id_s)
    jr = get_join_request(jr_id)
    if not jr or jr["status"] != "approved":
        await query.edit_message_text("This request is not approved or no longer valid.")
        return ConversationHandler.END

    if jr["telegram_id"] != update.effective_user.id:
        await query.edit_message_text("This profile link is not for you.")
        return ConversationHandler.END

    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE id=%s", (jr["invitation_id"],))
        inv = cur.fetchone()
    if not inv or token_expired(inv):
        await query.edit_message_text("The invite expired. Ask your manager/admin for a new one.")
        return ConversationHandler.END

    context.user_data["profile_jr_id"] = jr_id
    await query.edit_message_text("Your first name?")
    return ASK_FIRST

async def ask_first(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await reply_text_safe(update, context, "Session expired. Tap the Start Profile button again.")
        return ConversationHandler.END
    first_name = (update.message.text or "").strip()[:100]
    set_join_profile_field(jr_id, "first_name", first_name)
    await reply_text_safe(update, context, "Your last name?")
    return ASK_LAST

async def ask_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await reply_text_safe(update, context, "Session expired. Tap the Start Profile button again.")
        return ConversationHandler.END
    last_name = (update.message.text or "").strip()[:100]
    set_join_profile_field(jr_id, "last_name", last_name)
    await reply_text_safe(update, context, "Your phone number (digits only, country code optional)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await reply_text_safe(update, context, "Session expired. Tap the Start Profile button again.")
        return ConversationHandler.END

    phone = "".join(ch for ch in (update.message.text or "") if ch.isdigit() or ch == "+")[:32]
    set_join_profile_field(jr_id, "phone", phone)

    jr = get_join_request(jr_id)
    if not jr or jr["status"] != "approved":
        await reply_text_safe(update, context, "This request is no longer valid.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE telegram_id=%s", (jr["telegram_id"],))
            row = cur.fetchone()
            if row:
                user_id = row[0]
                cur.execute(
                    "UPDATE users "
                    "SET role=%s, manager_id=%s, username=%s, first_name=%s, last_name=%s, phone=%s, is_active=1 "
                    "WHERE id=%s",
                    (jr["invite_role"], jr["manager_id"], jr.get("username"),
                     jr.get("first_name"), jr.get("last_name"), phone, user_id),
                )
            else:
                cur.execute(
                    "INSERT INTO users "
                    "(telegram_id, username, role, is_active, manager_id, first_name, last_name, phone) "
                    "VALUES (%s,%s,%s,1,%s,%s,%s,%s)",
                    (jr["telegram_id"], jr.get("username"), jr["invite_role"], jr["manager_id"],
                     jr.get("first_name"), jr.get("last_name"), phone),
                )
                user_id = cur.lastrowid

        mark_invitation_used(jr["invitation_id"], user_id)
        # === MANAGER-ONLY next steps ===
        if jr["invite_role"] == ROLE_MANAGER:
            await reply_text_safe(
                update, context,
                "Set your **Login ID** (must be unique, use letters/numbers/._-, up to 100 chars).",
            )
            return ASK_MGR_LOGIN

        await reply_text_safe(update, context, "Your records have been inserted. You can use the bot now.")
        context.user_data.clear()
        await show_main_menu_message(update, context)
    except Exception as e:
        logger.exception("Finalizing profile failed | jr_id=%s error=%s", jr_id, e)
        await reply_text_safe(update, context, "Something went wrong saving your profile. Please try again.")
        context.user_data.clear()
    return ConversationHandler.END

def _valid_login_id(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", s or ""))

async def ask_mgr_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Requires: is_login_taken(login_id) helper from Step 2
    login_id = (update.message.text or "").strip()
    if not _valid_login_id(login_id):
        await reply_text_safe(update, context,
                              "Invalid login. Use only letters/numbers/._- and up to 100 characters. Send again.")
        return ASK_MGR_LOGIN
    if is_login_taken(login_id):
        await reply_text_safe(update, context, "That login is already taken. Send a different login ID.")
        return ASK_MGR_LOGIN

    context.user_data["mgr_login"] = login_id
    await reply_text_safe(update, context, "Great. Now send the **Password** for this login.")
    return ASK_MGR_PASS

async def ask_mgr_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Requires: create_manager_login(login, password, telegram_id) helper from Step 2
    pwd = (update.message.text or "").strip()
    if not pwd:
        await reply_text_safe(update, context, "Password cannot be empty. Send a password.")
        return ASK_MGR_PASS

    tg_id = update.effective_user.id
    login_id = context.user_data.get("mgr_login")
    if not login_id:
        await reply_text_safe(update, context, "Session lost. Please start profile again.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        if is_login_taken(login_id):
            await reply_text_safe(update, context, "That login was just taken. Send a different login ID.")
            return ASK_MGR_LOGIN

        create_manager_login(login_id, pwd, tg_id)

    except mysql_errors.IntegrityError as e:
        # Handle UNIQUE(login) violation (race condition)
        if getattr(e, "errno", None) == 1062:
            await reply_text_safe(update, context, "That login was just taken. Send a different login ID.")
            return ASK_MGR_LOGIN
        logger.exception("IntegrityError creating manager login | tg=%s err=%s", tg_id, e)
        await reply_text_safe(update, context, "Could not save login. Please try again.")
        return ASK_MGR_PASS

    except Exception as e:
        logger.exception("Creating manager login failed | tg=%s err=%s", tg_id, e)
        await reply_text_safe(update, context, "Could not save login. Please try again.")
        return ASK_MGR_PASS


    await reply_text_safe(
        update, context,
        f"✅ Manager login created.\n\nLogin: `{login_id}`\nPassword: `{pwd}`",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    await show_main_menu_message(update, context)
    return ConversationHandler.END

# -------- Masters UI --------
def masters_root_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏷 Sites", callback_data="masters:pick:sites")],
            [InlineKeyboardButton("🚁 Drones", callback_data="masters:pick:drones")],
            [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
        ]
    )

def masters_kind_menu_kb(kind: str):
    label = _label_for(kind)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"➕ Add {label}", callback_data=f"masters:add:{kind}")],
            [InlineKeyboardButton(f"📋 List {label}s", callback_data=f"masters:list:{kind}")],
            [InlineKeyboardButton(f"✏️ Rename {label}", callback_data=f"masters:rename:list:{kind}")],
            [InlineKeyboardButton(f"🔄 Toggle Active", callback_data=f"masters:toggle:list:{kind}")],
            [InlineKeyboardButton(f"🗑️ Delete {label}", callback_data=f"masters:delete:list:{kind}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="mgr:masters")],
        ]
    )

def masters_list_back_kb(kind: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"masters:pick:{kind}")]])

def masters_items_kb(kind: str, action: str):
    """Build a keyboard listing items for rename/toggle/delete."""
    items = masters_list(kind)
    rows = []
    if not items:
        rows.append([InlineKeyboardButton("(No records)", callback_data=f"masters:pick:{kind}")])
    else:
        for it in items:
            status = "✅" if it["is_active"] == 1 else "🚫"
            text = f"{status} {it['name']}"
            if action == "rename":
                cb = f"masters:rename:{kind}:{it['id']}"
            elif action == "toggle":
                cb = f"masters:toggle:{kind}:{it['id']}"
            elif action == "delete":
                cb = f"masters:delask:{kind}:{it['id']}"
            else:
                cb = f"masters:pick:{kind}"
            rows.append([InlineKeyboardButton(text, callback_data=cb)])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"masters:pick:{kind}")])
    return InlineKeyboardMarkup(rows)

async def masters_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All inline flows for masters (except add/rename text entry which is in a conversation)."""
    query = update.callback_query
    await query.answer()
    if not has_staff_privileges(update.effective_user.id):
        await query.edit_message_text("You are not authorized.")
        return

    data = query.data

    if data == "mgr:masters":
        await query.edit_message_text("Masters — choose what to manage:", reply_markup=masters_root_kb())
        return

    if data.startswith("masters:pick:"):
        _, _, kind = data.split(":")
        await query.edit_message_text(f"Manage {_label_for(kind)}s:", reply_markup=masters_kind_menu_kb(kind))
        return

    # List (clean — no #id, only Back)
    if data.startswith("masters:list:"):
        _, _, kind = data.split(":")
        items = masters_list(kind)
        if not items:
            txt = f"No {_label_for(kind)}s yet."
        else:
            lines = [f"{'✅' if r['is_active']==1 else '🚫'} {r['name']}" for r in items]
            txt = "\n".join(lines)
        await query.edit_message_text(txt or "No items.", reply_markup=masters_list_back_kb(kind))
        return

    # Add → ask for name (conversation starts; handled by ConversationHandler entry points)
    if data.startswith("masters:add:"):
        _, _, kind = data.split(":")
        context.user_data["masters_kind"] = kind
        context.user_data["masters_action"] = "add"
        await query.edit_message_text(f"Send the new {_label_for(kind)} name.")
        return MASTERS_ADD_NAME

    # Rename → choose item list
    if data.startswith("masters:rename:list:"):
        _, _, _, kind = data.split(":")
        await query.edit_message_text(
            f"Select a {_label_for(kind)} to rename:",
            reply_markup=masters_items_kb(kind, "rename")
        )
        return

    # Rename → picked item, ask for new name (conversation)
    if data.startswith("masters:rename:"):
        _, _, kind, id_str = data.split(":")
        context.user_data["masters_kind"] = kind
        context.user_data["masters_action"] = "rename"
        context.user_data["masters_id"] = int(id_str)
        await query.edit_message_text(f"Send the new name for this {_label_for(kind)}.")
        return MASTERS_RENAME_NAME

    # Toggle → choose item list
    if data.startswith("masters:toggle:list:"):
        _, _, _, kind = data.split(":")
        await query.edit_message_text(
            f"Toggle active — select {_label_for(kind)}:",
            reply_markup=masters_items_kb(kind, "toggle")
        )
        return

    # Toggle → flip and show result
    if data.startswith("masters:toggle:"):
        _, _, kind, id_str = data.split(":")
        ok, new_val = masters_toggle(kind, int(id_str))
        status = "active" if new_val == 1 else "inactive"
        msg = f"Updated: {_label_for(kind)} is now {status}." if ok else "Not found."
        await query.edit_message_text(msg, reply_markup=masters_kind_menu_kb(kind))
        return

    # Delete → choose item list
    if data.startswith("masters:delete:list:"):
        _, _, _, kind = data.split(":")
        await query.edit_message_text(
            f"Delete {_label_for(kind)} — select item:",
            reply_markup=masters_items_kb(kind, "delete")
        )
        return

    # Delete → confirm
    if data.startswith("masters:delask:"):
        _, _, kind, id_str = data.split(":")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"masters:del:{kind}:{id_str}")],
                [InlineKeyboardButton("⬅️ Cancel", callback_data=f"masters:pick:{kind}")],
            ]
        )
        await query.edit_message_text("Are you sure you want to delete this item?", reply_markup=kb)
        return

    # Delete → perform
    if data.startswith("masters:del:"):
        _, _, kind, id_str = data.split(":")
        ok, err = masters_delete(kind, int(id_str))
        if ok:
            await query.edit_message_text("Deleted.", reply_markup=masters_kind_menu_kb(kind))
        else:
            await query.edit_message_text(
                f"Cannot delete: this {_label_for(kind)} is referenced by existing reports.",
                reply_markup=masters_kind_menu_kb(kind)
            )
        return

# --- Masters conversation: add name / rename name ---
async def masters_add_name_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = context.user_data.get("masters_kind")
    if not kind:
        await reply_text_safe(update, context, "Session expired. Open Masters again.")
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await reply_text_safe(update, context, "Name cannot be empty. Send a valid name.")
        return MASTERS_ADD_NAME
    try:
        masters_add(kind, name)
        await reply_text_safe(update, context, f"{_label_for(kind)} added.")
    except mysql_errors.IntegrityError as e:
        if getattr(e, "errno", None) == 1062:
            await reply_text_safe(update, context, "That name already exists. Try another.")
        else:
            await reply_text_safe(update, context, f"DB error: {e}")
    finally:
        context.user_data.pop("masters_kind", None)
        context.user_data.pop("masters_action", None)
    await reply_text_safe(update, context, f"Manage {_label_for(kind)}s:", reply_markup=masters_kind_menu_kb(kind))
    return ConversationHandler.END

async def masters_rename_name_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = context.user_data.get("masters_kind")
    rec_id = context.user_data.get("masters_id")
    if not kind or not rec_id:
        await reply_text_safe(update, context, "Session expired. Open Masters again.")
        return ConversationHandler.END
    new_name = (update.message.text or "").strip()
    if not new_name:
        await reply_text_safe(update, context, "Name cannot be empty. Send a valid name.")
        return MASTERS_RENAME_NAME
    try:
        ok = masters_rename(kind, rec_id, new_name)
        msg = "Updated." if ok else "Not found."
        await reply_text_safe(update, context, msg)
    except mysql_errors.IntegrityError as e:
        if getattr(e, "errno", None) == 1062:
            await reply_text_safe(update, context, "That name already exists. Try another.")
        else:
            await reply_text_safe(update, context, f"DB error: {e}")
    finally:
        context.user_data.pop("masters_kind", None)
        context.user_data.pop("masters_action", None)
        context.user_data.pop("masters_id", None)
    await reply_text_safe(update, context, f"Manage {_label_for(kind)}s:", reply_markup=masters_kind_menu_kb(kind))
    return ConversationHandler.END

# -------- Manager Users panel (existing) --------
def manager_panel_kb(actor):
    rows = [
        [InlineKeyboardButton("👥 Show Users", callback_data="mgr:show_users")],
        [InlineKeyboardButton("🧩 Masters (Sites & Drones)", callback_data="mgr:masters")],
        [InlineKeyboardButton("➕ Invite Users", callback_data="mgr:invite")],
        [InlineKeyboardButton("⏳ Pending Approvals", callback_data="mgr:pending")],
    ]
    if actor and actor["role"] == ROLE_ADMIN:
        rows.insert(2, [InlineKeyboardButton("➕ Invite Manager", callback_data="mgr:invite_mgr")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")])
    return InlineKeyboardMarkup(rows)

def _format_employee_line(e):
    fname = (e["first_name"] or "").strip()
    lname = (e["last_name"] or "").strip()
    name = (fname + " " + lname).strip() or "Unnamed"
    active = "Yes" if e["is_active"] == 1 else "No"
    return f"• {name} — Active: {active}"

def _employee_name_by_id(user_id):
    u = get_user_by_id(user_id)
    if not u:
        return f"User #{user_id}"
    fname = (u["first_name"] or "").strip()
    lname = (u["last_name"] or "").strip()
    name = (fname + " " + lname).strip() or f"User #{user_id}"
    return name

async def manager_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not has_staff_privileges(update.effective_user.id):
        await query.edit_message_text("You are not authorized.")
        return

    tg = update.effective_user
    actor = get_staff_record(tg.id)
    data = query.data

    if data in ("mgr:panel", "mgr:back"):
        await query.edit_message_text("Management panel:", reply_markup=manager_panel_kb(actor))
        return

    # Invite EMPLOYEE
    if data == "mgr:invite":
        token, exp_utc = create_invitation(manager_id=actor["id"], invite_role=ROLE_EMPLOYEE)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        text = (
            "Share this invite with your employee:\n"
            f"{link}\n\n"
            f"Expires (IST): {fmt_ist(exp_utc)}\n"
            f"Time left: {human_left(exp_utc)}\n\n"
            "If Start didn’t ask them, they can send:\n"
            f"/use {token}"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)
        return

    # Invite MANAGER (Admins only)
    if data == "mgr:invite_mgr":
        if actor["role"] != ROLE_ADMIN:
            await query.edit_message_text("Only admins can invite managers.", reply_markup=manager_panel_kb(actor))
            return
        token, exp_utc = create_invitation(manager_id=actor["id"], invite_role=ROLE_MANAGER)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        text = (
            "Share this invite to add a Manager:\n"
            f"{link}\n\n"
            f"Expires (IST): {fmt_ist(exp_utc)}\n"
            f"Time left: {human_left(exp_utc)}\n\n"
            "If Start didn’t ask them, they can send:\n"
            f"/use {token}"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)
        return

    if data == "mgr:pending":
        items = get_pending_join_requests(actor["id"])
        if not items:
            await query.edit_message_text(
                "No pending approvals.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                     [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]]
                ),
            )
            return

        await query.edit_message_text(
            f"Pending approvals: {len(items)}.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                 [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]]
            ),
        )

        for jr in items:
            with db_conn() as conn, conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT * FROM invitations WHERE id=%s", (jr["invitation_id"],))
                inv = cur.fetchone()
            deadline = inv["expires_at"].replace(tzinfo=UTC)
            still_ok = datetime.now(UTC) <= deadline

            role_txt = "Manager" if jr["invite_role"] == ROLE_MANAGER else "Employee"
            text = (
                f"Req #{jr['id']} — {role_txt}\n"
                f"tg:{jr['telegram_id']} (@{jr['username'] or '-'})\n"
                f"Requested: {fmt_ist(jr['created_at'].replace(tzinfo=UTC))}\n"
                f"Expires: {fmt_ist(deadline)} ({human_left(deadline)})"
            )

            if still_ok:
                buttons = [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"jr:approve:{jr['id']}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"jr:reject:{jr['id']}"),
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                ]
            else:
                buttons = [
                    [InlineKeyboardButton("⛔ Expired — Re-Invite", callback_data=("mgr:invite_mgr" if jr["invite_role"]==ROLE_MANAGER else "mgr:invite"))],
                    [InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")],
                ]
            await safe_send_message(
                context.bot,
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    if data == "mgr:profile":
        u = get_user_by_tg(update.effective_user.id)
        if not u or u["role"] != ROLE_MANAGER or u["is_active"] != 1:
            await query.edit_message_text("Only active managers can view profile.")
            return
        rec = manager_login_by_tg(update.effective_user.id)  # helper from Step 2
        if not rec:
            await query.edit_message_text(
                "No manager login is set yet.\nFinish your profile flow to create one.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")]])
            )
            return
        txt = f"👤 Your Manager Login\nLogin: `{rec['login']}`\nPassword: `{rec['password']}`"
        await query.edit_message_text(
            txt,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="mgr:panel")]])
        )
        return


    # ======= Show Users =======
    if data == "mgr:show_users":
        emps = list_employees(actor["id"], limit=25)
        if not emps:
            await query.edit_message_text(
                "No employees yet.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]
                ),
            )
            return

        lines = [_format_employee_line(e) for e in emps]
        buttons = [
            [InlineKeyboardButton("🗑️ Deactivate", callback_data="mgr:deactivate:list")],
            [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
        ]

        await query.edit_message_text(
            "Employees (latest 25):\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Deactivate list (selection screen)
    if data == "mgr:deactivate:list":
        actives = list_active_employees(actor["id"], limit=50)
        if not actives:
            await query.edit_message_text(
                "No active employees to deactivate.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back to Users", callback_data="mgr:show_users")],
                     [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]
                ),
            )
            return

        rows = []
        for emp in actives:
            name = ((emp["first_name"] or "") + " " + (emp["last_name"] or "")).strip() or f"User #{emp['id']}"
            rows.append([InlineKeyboardButton(name, callback_data=f"mgr:delask:{emp['id']}")])

        rows.append([InlineKeyboardButton("⬅️ Back to Users", callback_data="mgr:show_users")])
        rows.append([InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")])

        await query.edit_message_text(
            "Select an employee to deactivate:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("mgr:delask:"):
        _, _, uid = data.split(":")
        uid = int(uid)
        name = _employee_name_by_id(uid)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, deactivate", callback_data=f"mgr:del:{uid}")],
                [InlineKeyboardButton("⬅️ Back to Selection", callback_data="mgr:deactivate:list")],
                [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
            ]
        )
        await query.edit_message_text(
            f"Are you sure you want to deactivate {name}?",
            reply_markup=kb,
        )
        return

    if data.startswith("mgr:del:"):
        _, _, uid = data.split(":")
        uid = int(uid)
        ok = deactivate_employee(uid, actor["id"])
        msg = "User deactivated." if ok else "Could not deactivate (wrong manager or already inactive)."
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🗑️ Deactivate another", callback_data="mgr:deactivate:list")],
                    [InlineKeyboardButton("⬅️ Back to Users", callback_data="mgr:show_users")],
                    [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
                ]
            ),
        )
        return

# -------- Main menu callbacks (help/report/home) --------
async def main_menu_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main:menu":
        text, kb = render_main_menu(update.effective_user.id)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "main:help":
        u = get_user_by_tg(update.effective_user.id)
        if u and u["is_active"] == 1 and u["role"] in (ROLE_ADMIN, ROLE_MANAGER):
            extra = "Admins can also invite managers."
            txt = (
                "Help for Admins/Managers:\n"
                "• Create invites and approve within 24 hours.\n"
                "• Approval asks the user to complete their profile.\n"
                "• Employees submit daily reports which you review.\n"
                f"• {extra}"
            )
        else:
            txt = (
                "Help for Employees:\n"
                "• This is a report bot.\n"
                "• Use Submit Report to fill your daily report based on your work data.\n"
                "• Your manager receives your report each day."
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]])
        await query.edit_message_text(txt, reply_markup=kb)
        return

    if data == "emp:report":
        # Only active employees get the WebApp button
        u = get_user_by_tg(update.effective_user.id)
        if not (u and u["is_active"] == 1 and u["role"] == ROLE_EMPLOYEE):
            await query.edit_message_text("Only active employees can submit reports.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]]))
            return

        if not WEBAPP_URL or not WEBAPP_URL.startswith("http"):
            await query.edit_message_text(
                "WebApp URL not configured. Please contact your administrator.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]]),
            )
            return

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Open Report", web_app=WebAppInfo(url=WEBAPP_URL))],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")],
            ]
        )
        await query.edit_message_text(
            "Tap to open the report WebApp and submit your daily report.",
            reply_markup=kb,
        )
        return

# -------- Utility --------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user_by_tg(update.effective_user.id)
    if u and u["is_active"] == 1 and u["role"] in (ROLE_ADMIN, ROLE_MANAGER):
        extra = "Admins can also invite managers."
        txt = (
            "Help for Admins/Managers:\n"
            "• Create invites and approve within 24 hours.\n"
            "• Approval asks the user to complete their profile.\n"
            "• Employees submit daily reports which you review.\n"
            f"• {extra}"
        )
    else:
        txt = (
            "Help for Employees:\n"
            "• This is a report bot.\n"
            "• Use Submit Report to fill your daily report based on your work data.\n"
            "• If your invite link's Start didn’t work, send /use <code>."
        )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]])
    await reply_text_safe(update, context, txt, reply_markup=kb)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply_text_safe(update, context, "Canceled.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error | update=%s error=%s", update, context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await safe_send_message(context.bot, chat_id=update.effective_chat.id, text="An error occurred. Please try again.")
    except Exception:
        pass

# ----------------- Main -----------------
def main():
    logger.info("Starting bot...")

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    # --- Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("use", use_cmd))

    # --- Masters: Conversation FIRST (handles add & rename text entry)
    masters_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(masters_buttons, pattern=r"^masters:add:(sites|drones)$"),
            CallbackQueryHandler(masters_buttons, pattern=r"^masters:rename:(sites|drones):\d+$"),
        ],
        states={
            MASTERS_ADD_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, masters_add_name_msg)],
            MASTERS_RENAME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, masters_rename_name_msg)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(masters_conv)

    # --- Masters: generic buttons AFTER, and EXCLUDE add/rename actual edits
    app.add_handler(CallbackQueryHandler(
        masters_buttons,
        pattern=r"^(mgr:masters|masters:(pick:.*|list:.*|rename:list:.*|toggle(:|:list:.*|:.*)|delete(:|:list:.*|:.*)|delask:.*|del:.*))$"
    ))

    # --- Staff panel + invites + pending + users
    app.add_handler(CallbackQueryHandler(manager_buttons, pattern=r"^mgr:(?!masters)"))

    # --- Join request approve/reject
    app.add_handler(CallbackQueryHandler(join_request_callback, pattern=r"^jr:(approve|reject):\d+$"))

    # --- Profile flow
    profile_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(profile_start, pattern=r"^prof:start:\d+$")],
        states={
            ASK_FIRST:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first)],
            ASK_LAST:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last)],
            ASK_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            # NEW states for managers:
            ASK_MGR_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_mgr_login)],
            ASK_MGR_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_mgr_pass)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(profile_conv)

    # --- Main menu callbacks (help/report/home)
    app.add_handler(CallbackQueryHandler(main_menu_callbacks, pattern=r"^(main:|emp:)"))

    # --- Fallback text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_uuid_text))

    # --- Errors
    app.add_error_handler(error_handler)

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
