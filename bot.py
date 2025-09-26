# bot.py
import os
import re
import uuid
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from mysql.connector import pooling

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

# ----------------- Logging -----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("staffbot")

# ----------------- Load config -----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without '@'
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS", "")
INVITE_DAYS_VALID = int(os.getenv("INVITE_DAYS_VALID", "1"))  # invitation lifetime (days)

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
ROLE_MANAGER = 1
ROLE_EMPLOYEE = 2
APPROVAL_WINDOW_HOURS = 24

# Conversation states (post-approval profile)
ASK_FIRST, ASK_LAST, ASK_PHONE = range(3)

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

def is_manager(telegram_id):
    u = get_user_by_tg(telegram_id)
    return bool(u and u["role"] == ROLE_MANAGER and u["is_active"] == 1)

def get_manager_record(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE telegram_id=%s AND role=%s", (telegram_id, ROLE_MANAGER))
        return cur.fetchone()

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
    logger.info("Invitation created | token=%s manager_id=%s expires_at_utc=%s", token, manager_id, expires_at_utc)
    return token, exp_utc

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
    logger.info("Invitation marked used | invitation_id=%s user_id=%s", inv_id, user_id)

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
        jr_id = cur.lastrowid
    logger.info(
        "Join request created | jr_id=%s tg=%s manager_id=%s invitation_id=%s",
        jr_id, telegram_id, manager_id, invitation_id
    )
    return jr_id

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
    logger.info("Join request status updated | jr_id=%s status=%s decided_by=%s", jr_id, status, decided_by)

def set_join_profile_field(jr_id, field, value):
    if field not in ("first_name", "last_name", "phone"):
        logger.warning("Ignored setting invalid join_profile field | field=%s", field)
        return
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE join_requests SET {field}=%s WHERE id=%s", (value, jr_id))
    logger.info("Join request field set | jr_id=%s %s=%s", jr_id, field, value)

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
        changed = cur.rowcount > 0
    logger.info("Deactivate employee | user_id=%s manager_id=%s success=%s", user_id, manager_id, changed)
    return changed

def get_telegram_id_by_user_row_id(row_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT telegram_id FROM users WHERE id=%s", (row_id,))
        r = cur.fetchone()
        return r[0] if r else None

# ----------------- UI helpers -----------------
def render_main_menu(telegram_id: int):
    u = get_user_by_tg(telegram_id)
    if not u:
        text = (
            "Welcome! If you’re an employee, please join via your invite link.\n"
            "If Start didn’t work, send your invite code using `/use <code>`."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Help", callback_data="main:help")]])
        return text, kb

    if u["role"] == ROLE_MANAGER and u["is_active"] == 1:
        text = "👋 Manager menu — choose an option:"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚙️ Manage Users", callback_data="mgr:panel")],
                [InlineKeyboardButton("ℹ️ Help", callback_data="main:help")],
            ]
        )
        return text, kb

    if u["is_active"] == 1:
        text = "👋 Employee menu — choose an option:"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Submit Report", callback_data="emp:report")],
                [InlineKeyboardButton("ℹ️ Help", callback_data="main:help")],
            ]
        )
    else:
        text = "⏳ Your account is pending manager approval."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Help", callback_data="main:help")]])
    return text, kb

async def show_main_menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = render_main_menu(update.effective_user.id)
    if update.message:
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)

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
    """Verify invite token, open a join_request (pending), notify manager immediately."""
    try:
        inv = get_invitation(token)
        if not inv or inv["status"] not in ("pending",):
            if update.message:
                await update.message.reply_text("❌ Invalid or inactive invite. Ask your manager for a new link.")
            else:
                await update.callback_query.edit_message_text("❌ Invalid or inactive invite. Ask your manager for a new link.")
            logger.warning("Invite invalid or inactive | token=%s", token)
            return

        if token_expired(inv):
            msg = f"⌛ Invite expired (IST: {fmt_ist(inv['expires_at'].replace(tzinfo=UTC))}). Ask your manager for a new link."
            if update.message:
                await update.message.reply_text(msg)
            else:
                await update.callback_query.edit_message_text(msg)
            logger.info("Invite expired | token=%s expires_at=%s", token, inv["expires_at"])
            return

        tg = update.effective_user
        existing = find_pending_request(tg.id, inv["id"])
        if existing:
            jr_id = existing["id"]
            logger.info("Join request already pending | jr_id=%s tg=%s token=%s", jr_id, tg.id, token)
        else:
            jr_id = create_join_request_min(
                telegram_id=tg.id,
                username=tg.username,
                manager_id=inv["manager_id"],
                invite_role=inv["invite_role"],
                invitation_id=inv["id"],
            )

        # Inform employee
        if update.message:
            await update.message.reply_text("✅ Request sent to your manager. You’ll be notified when it’s approved.")
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text="✅ Request sent to your manager. You’ll be notified when it’s approved.")

        # Notify manager
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
        try:
            await context.bot.send_message(
                chat_id=mgr_tg,
                text=(
                    f"👤 New join request (#{jr_id})\n"
                    f"tg: {tg.id}  (@{tg.username or '-'})\n"
                    f"Requested: {fmt_ist(created_naive.replace(tzinfo=UTC))}\n"
                    f"Expires: {fmt_ist(deadline)} ({human_left(deadline)})"
                ),
                reply_markup=kb,
            )
            logger.info("Manager notified | manager_tg=%s jr_id=%s", mgr_tg, jr_id)
        except Exception as e:
            logger.exception("Failed to notify manager | manager_tg=%s jr_id=%s error=%s", mgr_tg, jr_id, e)

    except Exception as e:
        logger.exception("open_request_with_token failed | token=%s error=%s", token, e)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id,
                                               text="⚠️ Something went wrong. Please try again.")
            except Exception:
                pass

# ----------------- Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start and deep-link /start <token> (opens request, not profile)."""
    args = context.args
    if args:
        token = args[0].strip()
        logger.info("/start with arg | user=%s token=%s", update.effective_user.id, token)
        if UUID_RE.match(token):
            await open_request_with_token(update, context, token)
            return
    # No token flow
    u = get_user_by_tg(update.effective_user.id)
    if u:
        await show_main_menu_message(update, context)
    else:
        msg = (
            "Hi! To join as an employee, please use your invite link.\n\n"
            "If Start didn’t ask you, send your invite code using:\n"
            "`/use <paste-your-code>`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/use <invite-code>`", parse_mode="Markdown")
        return
    token = context.args[0].strip()
    logger.info("/use | user=%s token=%s", update.effective_user.id, token)
    if not UUID_RE.match(token):
        await update.message.reply_text("❌ That doesn't look like a valid invite code.")
        return
    await open_request_with_token(update, context, token)

async def detect_uuid_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If a user pastes a raw UUID, treat it as an invite code."""
    txt = (update.message.text or "").strip()
    if UUID_RE.match(txt):
        logger.info("Raw UUID detected in chat | user=%s token=%s", update.effective_user.id, txt)
        await open_request_with_token(update, context, txt)
        return
    await show_main_menu_message(update, context)

# -------- Manager: Approve/Reject join request --------
async def join_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_manager(update.effective_user.id):
        await query.edit_message_text("Only managers can perform this action.")
        return

    _, action, jr_id_s = query.data.split(":")
    jr_id = int(jr_id_s)
    mrec = get_manager_record(update.effective_user.id)
    jr = get_join_request(jr_id)

    if not jr or jr["manager_id"] != mrec["id"] or jr["status"] != "pending":
        await query.edit_message_text("This request is no longer pending.")
        logger.info("Approve/reject ignored | jr_id=%s manager=%s", jr_id, mrec["id"])
        return

    # Check invitation validity at decision time
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE id=%s", (jr["invitation_id"],))
        inv = cur.fetchone()
    if not inv or token_expired(inv):
        update_join_request_status(jr_id, "rejected", decided_by=mrec["id"])
        await query.edit_message_text(
            "⛔ Invitation expired. Ask the user to use a fresh invite.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ New Invite", callback_data="mgr:invite")],
                    [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
                ]
            ),
        )
        logger.info("Decision failed due to expired invite | jr_id=%s inv_id=%s", jr_id, jr["invitation_id"])
        return

    if action == "approve":
        update_join_request_status(jr_id, "approved", decided_by=mrec["id"])
        # Ask user to start profile now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧾 Start Profile", callback_data=f"prof:start:{jr_id}")]])
        try:
            await context.bot.send_message(
                chat_id=jr["telegram_id"],
                text="✅ Your request has been approved. Please complete your profile to finish joining.",
                reply_markup=kb,
            )
            logger.info("Approval sent to user | jr_id=%s user_tg=%s", jr_id, jr["telegram_id"])
        except Exception as e:
            logger.exception("Failed to notify user of approval | jr_id=%s error=%s", jr_id, e)

        await query.edit_message_text(
            "Approved. The user has been asked to complete their profile.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]),
        )
    else:
        update_join_request_status(jr_id, "rejected", decided_by=mrec["id"])
        try:
            await context.bot.send_message(
                chat_id=jr["telegram_id"],
                text="❌ Your join request was rejected by your manager.",
            )
            logger.info("Rejection sent to user | jr_id=%s user_tg=%s", jr_id, jr["telegram_id"])
        except Exception as e:
            logger.exception("Failed to notify user of rejection | jr_id=%s error=%s", jr_id, e)
        await query.edit_message_text(
            "Rejected.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]),
        )

# -------- Profile flow (after approval) --------
async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry via callback prof:start:<jr_id> from the employee."""
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

    # If already active, don't ask again
    if user_active_by_tg(jr["telegram_id"]):
        await query.edit_message_text("✅ Your profile is already completed.")
        logger.info("Profile already completed | tg=%s jr_id=%s", jr["telegram_id"], jr_id)
        return ConversationHandler.END

    # Ensure invitation still valid
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE id=%s", (jr["invitation_id"],))
        inv = cur.fetchone()
    if not inv or token_expired(inv):
        await query.edit_message_text("⛔ The invite expired. Ask your manager for a new one.")
        logger.info("Profile start failed due to expired invite | jr_id=%s", jr_id)
        return ConversationHandler.END

    context.user_data["profile_jr_id"] = jr_id
    await query.edit_message_text("Your *first name*?", parse_mode="Markdown")
    logger.info("Profile collection started | jr_id=%s", jr_id)
    return ASK_FIRST

async def ask_first(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await update.message.reply_text("Session expired. Tap the *Start Profile* button again.", parse_mode="Markdown")
        return ConversationHandler.END
    first_name = (update.message.text or "").strip()[:100]
    set_join_profile_field(jr_id, "first_name", first_name)
    await update.message.reply_text("Your *last name*?", parse_mode="Markdown")
    return ASK_LAST

async def ask_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await update.message.reply_text("Session expired. Tap the *Start Profile* button again.", parse_mode="Markdown")
        return ConversationHandler.END
    last_name = (update.message.text or "").strip()[:100]
    set_join_profile_field(jr_id, "last_name", last_name)
    await update.message.reply_text("Your phone number (digits only, country code optional)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr_id = context.user_data.get("profile_jr_id")
    if not jr_id:
        await update.message.reply_text("Session expired. Tap the *Start Profile* button again.", parse_mode="Markdown")
        return ConversationHandler.END

    phone = "".join(ch for ch in (update.message.text or "") if ch.isdigit() or ch == "+")[:32]
    set_join_profile_field(jr_id, "phone", phone)

    # Finalize → create/update users row now
    jr = get_join_request(jr_id)
    if not jr or jr["status"] != "approved":
        await update.message.reply_text("This request is no longer valid.")
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
                logger.info("Existing user updated from profile | user_id=%s jr_id=%s", user_id, jr_id)
            else:
                cur.execute(
                    "INSERT INTO users "
                    "(telegram_id, username, role, is_active, manager_id, first_name, last_name, phone) "
                    "VALUES (%s,%s,%s,1,%s,%s,%s,%s)",
                    (jr["telegram_id"], jr.get("username"), jr["invite_role"], jr["manager_id"],
                     jr.get("first_name"), jr.get("last_name"), phone),
                )
                user_id = cur.lastrowid
                logger.info("New user created from profile | user_id=%s jr_id=%s", user_id, jr_id)

        # Mark invitation used AFTER user is created
        mark_invitation_used(jr["invitation_id"], user_id)

        await update.message.reply_text("🎉 Your records have been inserted. You can use the bot now.")
        context.user_data.clear()
        await show_main_menu_message(update, context)
    except Exception as e:
        logger.exception("Finalizing profile failed | jr_id=%s error=%s", jr_id, e)
        await update.message.reply_text("⚠️ Something went wrong saving your profile. Please try again.")
        context.user_data.clear()
    return ConversationHandler.END

# -------- Manager panel --------
def manager_panel_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Show Users", callback_data="mgr:show_users")],
            [InlineKeyboardButton("➕ Invite Users", callback_data="mgr:invite")],
            [InlineKeyboardButton("⏳ Pending Approvals", callback_data="mgr:pending")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")],
        ]
    )

async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        await update.message.reply_text("You are not a manager.")
        return
    await update.message.reply_text("Manager panel:", reply_markup=manager_panel_kb())

# ===== Helpers for Show Users redesigned flow =====
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

    if not is_manager(update.effective_user.id):
        await query.edit_message_text("You are not a manager.")
        return

    tg = update.effective_user
    mgr = get_manager_record(tg.id)
    data = query.data
    logger.info("Manager button | manager_tg=%s action=%s", tg.id, data)

    if data in ("mgr:panel", "mgr:back"):
        await query.edit_message_text("Manager panel:", reply_markup=manager_panel_kb())
        return

    if data == "mgr:invite":
        token, exp_utc = create_invitation(manager_id=mgr["id"], invite_role=ROLE_EMPLOYEE)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        text = (
            "🔗 Share this invite with your employee:\n"
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
        items = get_pending_join_requests(mgr["id"])
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
            f"Pending approvals: {len(items)} (each request shown below).",
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

            text = (
                f"Req #{jr['id']} — tg:{jr['telegram_id']} (@{jr['username'] or '-'})\n"
                f"Requested: {fmt_ist(jr['created_at'].replace(tzinfo=UTC))}\n"
                f"Expires: {fmt_ist(deadline)} ({human_left(deadline)})"
            )

            if still_ok:
                buttons = [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"jr:approve:{jr['id']}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"jr:reject:{jr['id']}"),
                    ],
                    [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
                ]
            else:
                buttons = [
                    [InlineKeyboardButton("⛔ Expired — Re-Invite", callback_data="mgr:invite")],
                    [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
                ]
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    # ======= Redesigned "Show Users" =======
    if data == "mgr:show_users":
        emps = list_employees(mgr["id"], limit=25)
        if not emps:
            await query.edit_message_text(
                "No employees yet.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]
                ),
            )
            return

        # Build a clean list: First Last — Active: Yes/No
        lines = [_format_employee_line(e) for e in emps]
        buttons = [
            [InlineKeyboardButton("🗑️ Deactivate", callback_data="mgr:deactivate:list")],
            [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")],
        ]

        await query.edit_message_text(
            "👥 Employees (latest 25):\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Deactivate list (selection screen)
    if data == "mgr:deactivate:list":
        actives = list_active_employees(mgr["id"], limit=50)
        if not actives:
            await query.edit_message_text(
                "No active employees to deactivate.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back to Users", callback_data="mgr:show_users")],
                     [InlineKeyboardButton("⬅️ Manager Panel", callback_data="mgr:panel")]]
                ),
            )
            return

        # Build selectable list of active employees (buttons)
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
        ok = deactivate_employee(uid, mgr["id"])
        msg = "✅ User deactivated." if ok else "⚠️ Could not deactivate (wrong manager or already inactive)."
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
        if u and u["role"] == ROLE_MANAGER and u["is_active"] == 1:
            txt = (
                "Help for Managers:\n"
                "• Create invites and approve within 24 hours.\n"
                "• Approval asks the employee to complete their profile.\n"
                "• Employees submit daily reports which you review."
            )
        else:
            txt = (
                "Help for Employees:\n"
                "• This is a report bot.\n"
                "• Use *Submit Report* to fill your daily report based on your work data.\n"
                "• Your manager receives your report each day."
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]])
        await query.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")
        return

    if data == "emp:report":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]])
        await query.edit_message_text(
            "📝 This is a report bot.\n"
            "Tap the report action to fill your **daily** report based on your work data. "
            "Once submitted, your manager will receive it.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

# -------- Utility --------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user_by_tg(update.effective_user.id)
    if u and u["role"] == ROLE_MANAGER and u["is_active"] == 1:
        txt = (
            "Help for Managers:\n"
            "• Create invites and approve within 24 hours.\n"
            "• Approval asks the employee to complete their profile.\n"
            "• Employees submit daily reports which you review."
        )
    else:
        txt = (
            "Help for Employees:\n"
            "• This is a report bot.\n"
            "• Use *Submit Report* to fill your daily report based on your work data.\n"
            "• If your invite link's Start didn’t work, send `/use <code>`."
        )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main:menu")]])
    await update.message.reply_text(txt, reply_markup=kb, parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Canceled.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error | update=%s error=%s", update, context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ An error occurred. Please try again.")
    except Exception:
        pass

# ----------------- Main -----------------
def main():
    logger.info("Starting bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("use", use_cmd))  # deep-link fallback

    # Manager callbacks (panel + invite + pending + show/deactivate flow)
    app.add_handler(CallbackQueryHandler(manager_buttons, pattern=r"^mgr:"))
    # Join request approve/reject
    app.add_handler(CallbackQueryHandler(join_request_callback, pattern=r"^jr:(approve|reject):\d+$"))
    # Profile entry button (after approval)
    profile_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(profile_start, pattern=r"^prof:start:\d+$")],
        states={
            ASK_FIRST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first)],
            ASK_LAST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(profile_conv)

    # Main menu callbacks (help/report/home)
    app.add_handler(CallbackQueryHandler(main_menu_callbacks, pattern=r"^(main:|emp:)"))

    # Raw UUID pasted by a user
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_uuid_text))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
