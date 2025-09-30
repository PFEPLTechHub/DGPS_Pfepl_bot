import os
import uuid
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, get_flashed_messages
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

# Setup logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ----------------- App -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = APP_SECRET

# Register fmt_ist as a Jinja2 global function
def fmt_ist(dt_utc_naive):
    """Display timestamp as-is, assuming it's stored in IST."""
    if not dt_utc_naive:
        return "-"
    return dt_utc_naive.strftime("%d %b %Y %I:%M %p IST")
app.jinja_env.globals['fmt_ist'] = fmt_ist

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

# ----------------- Check for session_token column -----------------
def has_session_token_column():
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM manager_logins LIKE 'session_token'")
            return bool(cur.fetchone())
    except Exception as e:
        logger.warning(f"Error checking session_token column: {e}")
        return False

SESSION_TOKEN_ENABLED = has_session_token_column()

# ----------------- Time Helpers -----------------
def today_ist_str():
    return datetime.now().strftime("%Y-%m-%d")

# ----------------- Auth -----------------
def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("manager_login"):
            return redirect(url_for("login", next=request.path))
        if SESSION_TOKEN_ENABLED:
            try:
                with db_conn() as conn, conn.cursor(dictionary=True) as cur:
                    cur.execute(
                        "SELECT session_token FROM manager_logins WHERE login = %s LIMIT 1",
                        (session["manager_login"],)
                    )
                    row = cur.fetchone()
                    if not row or row["session_token"] != session.get("session_token"):
                        session.clear()
                        flash("Session expired. Please log in again.", "danger")
                        return redirect(url_for("login", next=request.path))
            except Exception as e:
                logger.warning(f"Session validation error: {e}")
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
            + (", session_token" if SESSION_TOKEN_ENABLED else "") +
            " FROM manager_logins WHERE login = %s LIMIT 1",
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

    # Generate new session token if enabled
    new_token = str(uuid.uuid4()) if SESSION_TOKEN_ENABLED else None
    if SESSION_TOKEN_ENABLED:
        try:
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE manager_logins SET session_token = %s WHERE login = %s",
                    (new_token, login_id)
                )
        except Exception as e:
            logger.warning(f"Failed to update session_token: {e}")

    # Fetch manager's name from users table
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT first_name, last_name "
            "FROM users WHERE telegram_id = %s LIMIT 1",
            (row["telegram_id"],)
        )
        user = cur.fetchone()

    # Set manager name (fallback to login if user not found)
    manager_name = (
        f"{user['first_name']} {user['last_name']}".strip() 
        if user and user["first_name"] 
        else row["login"]
    )

    session["manager_login"] = row["login"]
    session["manager_tg"] = row["telegram_id"]
    session["manager_name"] = manager_name
    if SESSION_TOKEN_ENABLED:
        session["session_token"] = new_token
    session.permanent = True

    next_url = request.args.get("next") or url_for("dashboard")
    return redirect(next_url)

@app.route("/logout")
def logout():
    if session.get("manager_login") and SESSION_TOKEN_ENABLED:
        try:
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE manager_logins SET session_token = NULL WHERE login = %s",
                    (session["manager_login"],)
                )
        except Exception as e:
            logger.warning(f"Failed to clear session_token: {e}")
    session.clear()
    return redirect(url_for("login"))

# ----------------- Flash Messages API -----------------
@app.route("/api/flash-messages", methods=["GET"])
@login_required
def get_flash_messages():
    messages = get_flashed_messages(with_categories=True)
    return jsonify({"messages": messages})

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

@app.route("/edit-report", methods=["GET", "POST"])
@login_required
def edit_report_page():
    """Show date picker and employee dropdown to filter reports."""
    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        cur.execute(
            "SELECT u.telegram_id, u.first_name, u.last_name, u.username "
            "FROM users u "
            "WHERE u.role = 2 AND u.is_active = 1 AND u.manager_id = "
            "(SELECT id FROM users WHERE telegram_id = %s LIMIT 1) "
            "ORDER BY u.first_name, u.last_name, u.id",
            (session["manager_tg"],)
        )
        emps = cur.fetchall()

    employees = [
        {
            "telegram_id": e["telegram_id"],
            "name": (f"{e['first_name']} {e['last_name']}".strip() or e["username"] or f"tg:{e['telegram_id']}")
        }
        for e in emps
    ]

    selected_date = request.form.get("date") or request.args.get("date") or today_ist_str()
    selected_employee = request.form.get("employee") or ""

    return render_template(
        "edit_report.html",
        employees=employees,
        selected_date=selected_date,
        selected_employee=selected_employee
    )

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
        })

    return jsonify({"ok": True, "date": date_str, "rows": rows})

# ----------------- API: Reports by date and employee -----------------
@app.route("/api/reports")
@login_required
def api_reports():
    date_str = request.args.get("date") or today_ist_str()
    employee_tg_id = request.args.get("employee") or ""
    mgr_tg_id = session["manager_tg"]

    if not employee_tg_id:
        return jsonify({"ok": True, "reports": []})

    with db_conn() as conn, conn.cursor(dictionary=True) as cur:
        # Verify employee is under this manager
        cur.execute(
            "SELECT 1 FROM users u "
            "WHERE u.telegram_id = %s AND u.role = 2 AND u.is_active = 1 AND u.manager_id = "
            "(SELECT id FROM users WHERE telegram_id = %s LIMIT 1) LIMIT 1",
            (employee_tg_id, mgr_tg_id)
        )
        if not cur.fetchone():
            return jsonify({"ok": True, "reports": []})

        # Fetch reports
        cur.execute(
            "SELECT id, report_date, site_name, drone_name, pilot_name, copilot_name, "
            "base_height_m, created_at, dgps_used_json, dgps_operators_json, "
            "grid_numbers_json, gcp_points_json, total_area_sq_km, total_time_min, remark "
            "FROM reports WHERE report_date = %s AND employee_telegram_id = %s",
            (date_str, employee_tg_id)
        )
        reports = cur.fetchall()

    return jsonify({"ok": True, "reports": reports})

# ----------------- Report detail (view/edit/delete) -----------------
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

@app.route("/report/<int:report_id>/preview", methods=["GET"])
@login_required
def report_preview(report_id):
    rep, flights = _get_report(report_id)
    if not rep:
        flash("Report not found.", "danger")
        return "", 404
    return render_template("report_detail.html", report=rep, flights=flights)

@app.route("/report/<int:report_id>/edit", methods=["GET", "POST"])
@login_required
def report_edit(report_id):
    rep, flights = _get_report(report_id)
    if not rep:
        flash("Report not found.", "danger")
        return redirect(url_for("edit_report_page"))

    if request.method == "GET":
        return render_template("report_edit.html", report=rep, flights=flights)

    # Collect and validate form data
    report_date = (request.form.get("report_date") or "").strip()
    pilot = (request.form.get("pilot_name") or "").strip()
    copilot = (request.form.get("copilot_name") or "").strip()
    remark = (request.form.get("remark") or "").strip()
    dgps_used = (request.form.get("dgps_used") or "").strip()
    dgps_operators = (request.form.get("dgps_operators") or "").strip()
    grid_numbers = (request.form.get("grid_numbers") or "").strip()
    gcp_points = (request.form.get("gcp_points") or "").strip()
    try:
        base_h = float(request.form.get("base_height_m") or rep["base_height_m"])
    except Exception:
        base_h = rep["base_height_m"]

    # Validate inputs
    errors = []
    if not report_date:
        errors.append("Report date is required.")
    if not pilot:
        errors.append("Pilot name is required.")
    if not copilot:
        errors.append("Copilot name is required.")
    if not remark:
        errors.append("Remark is required.")
    if not dgps_used:
        errors.append("DGPS used is required.")
    if not dgps_operators:
        errors.append("DGPS operators is required.")
    if not grid_numbers:
        errors.append("Grid numbers is required.")
    if not gcp_points:
        errors.append("GCP points is required.")

    # Validate flights
    flight_ids = request.form.getlist("flight_id[]")
    flight_times = request.form.getlist("flight_time[]")
    flight_areas = request.form.getlist("flight_area[]")
    flight_ubxs = request.form.getlist("flight_ubx[]")
    flight_bases = request.form.getlist("flight_base[]")
    flights_data = []
    for i in range(len(flight_times)):
        try:
            flight_id = flight_ids[i] if i < len(flight_ids) and flight_ids[i] else None
            time = float(flight_times[i]) if flight_times[i] else 0
            area = float(flight_areas[i]) if flight_areas[i] else 0
            ubx = (flight_ubxs[i] or "").strip()
            base = (flight_bases[i] or "").strip()
            if time < 1:
                errors.append(f"Flight {i+1}: Time must be ≥ 1.")
            if area <= 0:
                errors.append(f"Flight {i+1}: Area must be > 0.")
            if not ubx:
                errors.append(f"Flight {i+1}: UBX required.")
            if not base:
                errors.append(f"Flight {i+1}: Base file required.")
            flights_data.append({
                "id": flight_id,
                "flight_time_min": time,
                "area_sq_km": area,
                "uav_rover_file": ubx,
                "drone_base_file_no": base
            })
        except ValueError:
            errors.append(f"Flight {i+1}: Invalid time or area.")

    if errors:
        for err in errors:
            flash(err, "danger")
        return render_template("report_edit.html", report=rep, flights=flights)

    # Update report
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET report_date = %s, pilot_name = %s, copilot_name = %s, base_height_m = %s, "
                "dgps_used_json = %s, dgps_operators_json = %s, grid_numbers_json = %s, "
                "gcp_points_json = %s, remark = %s, total_time_min = %s, total_area_sq_km = %s "
                "WHERE id = %s",
                (
                    report_date, pilot, copilot, base_h,
                    dgps_used, dgps_operators, grid_numbers, gcp_points,
                    remark,
                    sum(f["flight_time_min"] for f in flights_data),
                    sum(f["area_sq_km"] for f in flights_data),
                    report_id
                )
            )
            # Update flights
            cur.execute("DELETE FROM report_flights WHERE report_id = %s", (report_id,))
            for f in flights_data:
                cur.execute(
                    "INSERT INTO report_flights (report_id, flight_time_min, area_sq_km, uav_rover_file, drone_base_file_no) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (report_id, f["flight_time_min"], f["area_sq_km"], f["uav_rover_file"], f["drone_base_file_no"])
                )
    except Exception as e:
        logger.error(f"Failed to update report {report_id}: {e}")
        flash("Failed to update report due to a server error.", "danger")
        return jsonify({"ok": False, "message": "Failed to update report."})

    flash("Report updated successfully.", "success")
    return jsonify({"ok": True, "message": "Report updated successfully."})

@app.route("/report/<int:report_id>/delete", methods=["POST"])
@login_required
def report_delete(report_id):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM reports WHERE id = %s", (report_id,))
            # report_flights are cascaded by FK constraint
    except Exception as e:
        logger.error(f"Failed to delete report {report_id}: {e}")
        flash("Failed to delete report due to a server error.", "danger")
        return jsonify({"ok": False, "message": "Failed to delete report."})
    flash("Report deleted successfully.", "success")
    return jsonify({"ok": True, "message": "Report deleted successfully."})

# ----------------- Run -----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, debug=True)