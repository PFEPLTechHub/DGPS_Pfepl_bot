import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash
)
import mysql.connector
from mysql.connector import pooling

# ----------------- Config -----------------
load_dotenv()
APP_SECRET = os.getenv("FLASK_SECRET_KEY", "change-me")

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB   = os.getenv("MYSQL_DB", "tg_staffbot")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS", "root@303")

# ----------------- App -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = APP_SECRET

# ----------------- DB Pool -----------------
dbconfig = {
    "host": MYSQL_HOST,
    "port": MYSQL_PORT,
    "database": MYSQL_DB,
    "user": MYSQL_USER,
    "password": MYSQL_PASS,
    "autocommit": True,
}
cnxpool = pooling.MySQLConnectionPool(pool_name="mgrpool", pool_size=5, **dbconfig)

def db_conn():
    return cnxpool.get_connection()

# ----------------- Time Helpers -----------------
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

def today_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d")

def fmt_ist(dt_utc_naive):
    """Assume DB timestamps are UTC-naive; render as IST string."""
    if not dt_utc_naive:
        return "-"
    if dt_utc_naive.tzinfo is None:
        aware = dt_utc_naive.replace(tzinfo=UTC)
    else:
        aware = dt_utc_naive
    return aware.astimezone(IST).strftime("%d %b %Y %I:%M %p IST")

# ----------------- Auth -----------------
def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("manager_login"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

@app.route("/", methods=["GET"])
def root():
    return redirect(url_for("dashboard") if session.get("manager_login") else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    login_id = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""

    if not login_id or not password:
        flash("Please enter both login and password.", "danger")
        return render_template("login.html", login_prefill=login_id)

    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT login, password, telegram_id, is_active "
            "FROM manager_logins WHERE login = %s LIMIT 1",
            (login_id,)
        )
        row = cur.fetchone()

    if not row:
        flash("No account found for this login.", "danger")
        return render_template("login.html", login_prefill=login_id)

    if row["is_active"] != 1:
        flash("Account is inactive.", "danger")
        return render_template("login.html", login_prefill=login_id)

    if row["password"] != password:
        flash("Incorrect password.", "danger")
        return render_template("login.html", login_prefill=login_id)

    session["manager_login"] = row["login"]
    session["manager_tg"] = row["telegram_id"]
    session.permanent = True

    next_url = request.args.get("next") or url_for("dashboard")
    return redirect(next_url)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ----------------- Sidebar Pages -----------------
@app.route("/dashboard")
@login_required
def dashboard():
    """Track page with date filter & table (Sr No., Employee Name, Submission Time, Status)."""
    return render_template("dashboard.html", default_date=today_ist_str())

@app.route("/view-report")
@login_required
def view_report_page():
    return render_template("report_detail.html", report=None, flights=[], readonly=True)

@app.route("/edit-report")
@login_required
def edit_report_page():
    return render_template("report_detail.html", report=None, flights=[], readonly=False)

# ----------------- API: Track data -----------------
@app.route("/api/track")
@login_required
def api_track():
    date_str = request.args.get("date") or today_ist_str()
    mgr_tg_id = session["manager_tg"]

    # Get employees under this manager
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT u.telegram_id, u.first_name, u.last_name, u.username "
            "FROM users u "
            "WHERE u.role = 2 AND u.is_active = 1 AND u.manager_id = "
            "(SELECT id FROM users WHERE telegram_id = %s LIMIT 1) "
            "ORDER BY u.first_name, u.last_name, u.id",
            (mgr_tg_id,)
        )
        emps = cur.fetchall()

    tg_ids = [e["telegram_id"] for e in emps]
    names = {}
    for e in emps:
        fname = (e["first_name"] or "").strip()
        lname = (e["last_name"] or "").strip()
        names[e["telegram_id"]] = (fname + " " + lname).strip() or (e["username"] or f"tg:{e['telegram_id']}")

    # Get reports for the selected date
    report_by_tg = {}
    if tg_ids:
        placeholders = ",".join(["%s"] * len(tg_ids))
        q = (
            f"SELECT id, employee_telegram_id, created_at "
            f"FROM reports WHERE report_date = %s AND employee_telegram_id IN ({placeholders})"
        )
        with db_conn() as conn, conn.cursor(dictionary=True) as cur:
            cur.execute(q, [date_str] + tg_ids)
            for r in cur.fetchall():
                report_by_tg[r["employee_telegram_id"]] = {"id": r["id"], "created_at": r["created_at"]}

    rows = []
    for i, tg_id in enumerate(tg_ids, start=1):
        r = report_by_tg.get(tg_id)
        rows.append({
            "sr": i,
            "employee_telegram_id": tg_id,
            "name": names.get(tg_id, f"tg:{tg_id}"),
            "time": fmt_ist(r["created_at"]) if r else "-",
            "status": "Submitted" if r else "Not Submitted",
            "report_id": r["id"] if r else None
        })

    return jsonify({"ok": True, "date": date_str, "rows": rows})

# ----------------- Report detail (view/edit) -----------------
def _get_report(report_id: int):
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT r.* "
            "FROM reports r "
            "WHERE r.id = %s",
            (report_id,)
        )
        rep = cur.fetchone()
        if not rep:
            return None, []
        cur.execute(
            "SELECT id, flight_time_min, area_sq_km, uav_rover_file, drone_base_file_no "
            "FROM report_flights WHERE report_id = %s ORDER BY id",
            (report_id,)
        )
        flights = cur.fetchall()
        return rep, flights

@app.route("/report/<int:report_id>", methods=["GET"])
@login_required
def report_detail(report_id):
    rep, flights = _get_report(report_id)
    if not rep:
        flash("Report not found.", "danger")
        return redirect(url_for("dashboard"))
    return render_template("report_detail.html", report=rep, flights=flights, readonly=True)

@app.route("/report/<int:report_id>/edit", methods=["GET", "POST"])
@login_required
def report_edit(report_id):
    rep, flights = _get_report(report_id)
    if not rep:
        flash("Report not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("report_detail.html", report=rep, flights=flights, readonly=False)

    pilot = (request.form.get("pilot_name") or "").strip()
    copilot = (request.form.get("copilot_name") or "").strip()
    remark = (request.form.get("remark") or "").strip()
    try:
        base_h = float(request.form.get("base_height_m") or rep["base_height_m"])
    except Exception:
        base_h = rep["base_height_m"]

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE reports SET pilot_name = %s, copilot_name = %s, base_height_m = %s, remark = %s WHERE id = %s",
            (pilot or rep["pilot_name"], copilot or rep["copilot_name"], base_h, remark, report_id)
        )

    flash("Report updated.", "success")
    return redirect(url_for("report_detail", report_id=report_id))

# ----------------- Run -----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, debug=True)