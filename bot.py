import os
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv

import mysql.connector
from mysql.connector import pooling

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ----------------- Load config -----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without '@'
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB   = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS", "")
INVITE_DAYS_VALID = int(os.getenv("INVITE_DAYS_VALID", "7"))

# ----------------- DB Pool -----------------
dbconfig = {
    "host": MYSQL_HOST, "port": MYSQL_PORT,
    "database": MYSQL_DB, "user": MYSQL_USER, "password": MYSQL_PASS,
    "autocommit": True
}
cnxpool = pooling.MySQLConnectionPool(pool_name="staffpool", pool_size=5, **dbconfig)

def db_conn():
    return cnxpool.get_connection()

# ----------------- Constants -----------------
ROLE_MANAGER = 1
ROLE_EMPLOYEE = 2

# Conversation states for onboarding
ASK_FIRST, ASK_LAST, ASK_PHONE = range(3)

# ----------------- Helpers -----------------
def get_user_by_tg(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE telegram_id=%s", (telegram_id,))
        return cur.fetchone()

def get_user_by_id(user_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        return cur.fetchone()

def ensure_user_record(telegram_id, username=None):
    """Create a bare record if not exists (used for managers too)."""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE telegram_id=%s", (telegram_id,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO users (telegram_id, username, role, is_active) VALUES (%s,%s,%s,%s)",
            (telegram_id, username, ROLE_EMPLOYEE, 0)
        )
        return cur.lastrowid

def is_manager(telegram_id):
    u = get_user_by_tg(telegram_id)
    return bool(u and u["role"] == ROLE_MANAGER and u["is_active"] == 1)

def create_invitation(manager_id, invite_role=ROLE_EMPLOYEE):
    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=INVITE_DAYS_VALID)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invitations (token, manager_id, invite_role, expires_at, status) "
            "VALUES (%s,%s,%s,%s,'pending')",
            (token, manager_id, invite_role, expires_at)
        )
    return token, expires_at

def get_invitation(token):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM invitations WHERE token=%s", (token,))
        return cur.fetchone()

def mark_invitation_used(inv_id, user_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE invitations SET status='used', used_at=UTC_TIMESTAMP(), redeemed_by_user_id=%s WHERE id=%s",
            (user_id, inv_id)
        )

def create_approval(invited_user_id, manager_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO approvals (invited_user_id, manager_id, status) VALUES (%s,%s,'pending')",
            (invited_user_id, manager_id)
        )
        return cur.lastrowid

def set_user_profile(user_id, first_name, last_name, phone):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET first_name=%s, last_name=%s, phone=%s WHERE id=%s",
            (first_name, last_name, phone, user_id)
        )

def activate_user(user_id, manager_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_active=1, manager_id=%s WHERE id=%s",
            (manager_id, user_id)
        )

def update_approval(approval_id, status, decided_by):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE approvals SET status=%s, decided_at=UTC_TIMESTAMP(), decided_by=%s WHERE id=%s",
            (status, decided_by, approval_id)
        )

def get_pending_approvals(manager_id, limit=10):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT a.id AS approval_id, u.* "
            "FROM approvals a JOIN users u ON a.invited_user_id=u.id "
            "WHERE a.manager_id=%s AND a.status='pending' "
            "ORDER BY a.created_at ASC LIMIT %s",
            (manager_id, limit)
        )
        return cur.fetchall()

def get_manager_record(telegram_id):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT * FROM users WHERE telegram_id=%s AND role=%s", (telegram_id, ROLE_MANAGER))
        return cur.fetchone()

def list_employees(manager_id, limit=25):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT id, telegram_id, username, first_name, last_name, phone, is_active "
            "FROM users WHERE role=%s AND manager_id=%s ORDER BY created_at DESC LIMIT %s",
            (ROLE_EMPLOYEE, manager_id, limit)
        )
        return cur.fetchall()

# ----------------- Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles normal /start and deep-link /start <token>."""
    tg = update.effective_user
    text_args = context.args

    # Ensure record (so managers can be recognized even if not seeded)
    ensure_user_record(tg.id, tg.username)

    if text_args:
        token = text_args[0]
        inv = get_invitation(token)
        if not inv:
            await update.message.reply_text("❌ Invalid invite link.")
            return
        if inv["status"] != "pending":
            await update.message.reply_text("⚠️ This invite link is no longer valid.")
            return
        if datetime.utcnow() > inv["expires_at"]:
            await update.message.reply_text("⌛ This invite link has expired.")
            return

        # Create/attach user (pending profile + approval)
        with db_conn() as conn, conn.cursor(dictionary=True) as cur:
            # If the user already exists, reuse; else create
            cur.execute("SELECT * FROM users WHERE telegram_id=%s", (tg.id,))
            u = cur.fetchone()
            if not u:
                cur.execute(
                    "INSERT INTO users (telegram_id, username, role, is_active, manager_id) VALUES (%s,%s,%s,%s,%s)",
                    (tg.id, tg.username, inv["invite_role"], 0, inv["manager_id"])
                )
                user_id = cur.lastrowid
            else:
                user_id = u["id"]
                # Ensure role & manager are set from invitation if empty
                if u["role"] != inv["invite_role"] or u["manager_id"] is None:
                    cur2 = conn.cursor()
                    cur2.execute("UPDATE users SET role=%s, manager_id=%s WHERE id=%s",
                                 (inv["invite_role"], inv["manager_id"], user_id))

        # Mark invite used and open approval record
        mark_invitation_used(inv["id"], user_id)
        approval_id = create_approval(user_id, inv["manager_id"])

        # Store context for profile collection
        context.user_data["onboard_user_id"] = user_id
        context.user_data["approval_id"] = approval_id

        await update.message.reply_text("👋 Welcome! Let’s set up your profile.\n\nWhat is your *first name*?",
                                        parse_mode="Markdown")
        return ASK_FIRST

    # No token → just a normal start
    await update.message.reply_text(
        "Hi! Use /whoami to see your role. Managers can use /manage to invite & approve users."
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user_by_tg(update.effective_user.id)
    if not u:
        await update.message.reply_text("No record found. Use /start again.")
        return
    r = "Manager" if u["role"] == ROLE_MANAGER else "Employee"
    await update.message.reply_text(
        f"Your role: {r}\n"
        f"Active: {'Yes' if u['is_active']==1 else 'No'}\n"
        f"Manager ID: {u['manager_id'] or '-'}\n"
        f"DB User ID: {u['id']}"
    )

# -------- Onboarding (collect profile) --------
async def ask_first(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()[:100]
    await update.message.reply_text("Great. Your *last name*?", parse_mode="Markdown")
    return ASK_LAST

async def ask_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_name"] = update.message.text.strip()[:100]
    await update.message.reply_text("Phone number (digits only, country code optional)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = "".join(ch for ch in update.message.text if ch.isdigit() or ch == '+')[:32]
    context.user_data["phone"] = phone

    user_id = context.user_data.get("onboard_user_id")
    approval_id = context.user_data.get("approval_id")

    # Save profile
    set_user_profile(user_id, context.user_data["first_name"], context.user_data["last_name"], phone)

    # Notify user pending approval
    await update.message.reply_text("✅ Thanks! Your details are saved.\n"
                                    "⏳ Waiting for manager approval...")

    # Ping the manager with Approve/Reject buttons
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT manager_id FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        manager_id = row["manager_id"]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}:{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{approval_id}:{user_id}")
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=manager_id,  # manager's telegram_id must match users.telegram_id
            text=(f"👤 New user awaiting approval:\n"
                  f"User ID: {user_id}\n"
                  f"Name: {context.user_data['first_name']} {context.user_data['last_name']}\n"
                  f"Phone: {phone}"),
            reply_markup=kb
        )
    except Exception:
        # If manager hasn't started the bot, this will fail silently for now
        pass

    # Clear convo state
    context.user_data.clear()
    return ConversationHandler.END

# -------- Manager: Approve/Reject callbacks --------
async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    acting_tg = update.effective_user.id
    if not is_manager(acting_tg):
        await query.edit_message_text("Only managers can perform this action.")
        return

    data = query.data  # approve:<approval_id>:<user_id> or reject:...
    action, approval_id, user_id = data.split(":")
    approval_id = int(approval_id)
    user_id = int(user_id)

    mrec = get_manager_record(acting_tg)
    if not mrec:
        await query.edit_message_text("Manager record not found.")
        return

    # Activate or reject
    if action == "approve":
        activate_user(user_id, manager_id=mrec["id"])
        update_approval(approval_id, "approved", decided_by=mrec["id"])
        await query.edit_message_text(f"✅ Approved user {user_id}. They can now use the bot.")
        try:
            await context.bot.send_message(chat_id=get_user_by_id(user_id)["telegram_id"],
                                           text="🎉 Your account is approved. You can use the bot now.")
        except Exception:
            pass
    else:
        update_approval(approval_id, "rejected", decided_by=mrec["id"])
        await query.edit_message_text(f"❌ Rejected user {user_id}.")
        try:
            await context.bot.send_message(chat_id=get_user_by_id(user_id)["telegram_id"],
                                           text="⚠️ Your account was rejected by your manager.")
        except Exception:
            pass

# -------- Manager menu (/manage) --------
async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        await update.message.reply_text("You are not a manager.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Show Users", callback_data="mgr:show_users")],
        [InlineKeyboardButton("➕ Invite Users", callback_data="mgr:invite")],
        [InlineKeyboardButton("⏳ Pending Approvals", callback_data="mgr:pending")]
    ])
    await update.message.reply_text("Manager panel:", reply_markup=kb)

async def manager_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_manager(update.effective_user.id):
        await query.edit_message_text("You are not a manager.")
        return

    data = query.data
    tg = update.effective_user

    if data == "mgr:invite":
        mgr = get_manager_record(tg.id)
        token, expires_at = create_invitation(manager_id=mgr["id"], invite_role=ROLE_EMPLOYEE)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        await query.edit_message_text(
            "🔗 Share this invite link with your employee:\n"
            f"{link}\n\n"
            f"Expires at (UTC): {expires_at:%Y-%m-%d %H:%M:%S}"
        )

    elif data == "mgr:show_users":
        mgr = get_manager_record(tg.id)
        emps = list_employees(mgr["id"], limit=25)
        if not emps:
            await query.edit_message_text("No employees yet.")
            return
        lines = []
        for e in emps:
            lines.append(f"• {e['first_name'] or ''} {e['last_name'] or ''} "
                         f"(tg:{e['telegram_id'] or '-'}, active:{'Y' if e['is_active']==1 else 'N'})")
        await query.edit_message_text("👥 Employees (latest 25):\n" + "\n".join(lines))

    elif data == "mgr:pending":
        mgr = get_manager_record(tg.id)
        items = get_pending_approvals(mgr["id"])
        if not items:
            await query.edit_message_text("No pending approvals.")
            return
        # Show as a compact list; actions still on each approval Via auto DM messages. Here we just preview.
        lines = [f"• #{row['approval_id']} — {row['first_name'] or ''} {row['last_name'] or ''} (user {row['id']})"
                 for row in items]
        await query.edit_message_text("⏳ Pending approvals:\n" + "\n".join(lines))

# -------- Utility: help and cancel --------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/whoami – show your role\n"
        "/manage – manager panel (invite/show/pending)\n"
        "Managers share invite links; new users fill profile; manager approves."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Canceled.")
    return ConversationHandler.END

# ----------------- Main -----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Start & whoami & manage
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("manage", manage))
    app.add_handler(CommandHandler("help", help_cmd))

    # Manager button actions
    app.add_handler(CallbackQueryHandler(manager_buttons, pattern=r"^mgr:"))
    app.add_handler(CallbackQueryHandler(approval_callback, pattern=r"^(approve|reject):"))

    # Onboarding conversation
    conv = ConversationHandler(
        entry_points=[],
        states={
            ASK_FIRST: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first)],
            ASK_LAST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={}
    )
    app.add_handler(conv)

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
