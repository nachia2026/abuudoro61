"""
KISAUNI PRIMARY SCHOOL - RESULT MANAGEMENT SYSTEM
Main Flask application.
"""
import os
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, send_file, send_from_directory, abort
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import (
    Config, grade_for_score, remark_for, detect_gender, ROLE_HEADMASTER, ROLE_CLASS_TEACHER,
    EXAM_TYPES, DEFAULT_ACADEMIC_YEAR, ORDINAL_WORDS, NUM_STANDARDS, subjects_for_standard,
    now, now_str, local_from_timestamp, EAT,
)
from database import (
    get_db, init_db, log_action, get_setting,
    create_backup, list_backups, restore_backup, restore_backup_from_upload, delete_backup, should_run_periodic_backup,
    days_since_last_offsite_download, AUDIT_LOG_RETENTION_DAYS,
)
from pdf_report import build_student_report_pdf, build_class_result_pdf

app = Flask(__name__)
app.config.from_object(Config)


class BrowserSessionInterface(SecureCookieSessionInterface):
    """Same as Flask's default session handling (the idle-timeout via
    PERMANENT_SESSION_LIFETIME + SESSION_REFRESH_EACH_REQUEST still applies
    server-side, unchanged), EXCEPT the session cookie is never sent with an
    Expires/Max-Age attribute. That makes it a true "session cookie" in the
    browser's eyes - closing the browser completely discards it, so the user
    must log in again next time regardless of whether the idle timeout was
    reached. (Before this, session.permanent=True made the cookie persistent
    across a full browser close/reopen for up to IDLE_TIMEOUT_MINUTES.)"""

    def get_expiration_time(self, app, session):
        return None


app.session_interface = BrowserSessionInterface()

# Ensure the data directory (and any subfolders) exist before touching the
# database - important in production where DATA_DIR points at a mounted
# persistent volume that may be empty on first deploy.
os.makedirs(os.path.dirname(Config.DATABASE), exist_ok=True)
os.makedirs(Config.REPORTS_DIR, exist_ok=True)
os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
os.makedirs(Config.BACKUP_DIR, exist_ok=True)

init_db()
print(f"[Kisauni] Database file: {Config.DATABASE}")
print(f"[Kisauni] Backups folder: {Config.BACKUP_DIR}")

# ---------------------------------------------------------------------------
# AUTOMATIC SELF-BACKUP
# A backup is taken once right away (covers "app just started"), and then
# a background thread keeps the system backed up on its own for as long as
# it keeps running - no one has to remember to do it manually. This is on
# top of the safety backup taken automatically before year-end promotion
# (see promote_students below).
# ---------------------------------------------------------------------------
if should_run_periodic_backup():
    create_backup(reason="startup")


def _periodic_backup_worker():
    while True:
        time.sleep(3600)  # check hourly; create_backup itself is cheap/rare
        try:
            if should_run_periodic_backup():
                create_backup(reason="daily")
        except Exception as exc:  # never let the backup thread crash the app
            print(f"[Kisauni] Automatic backup failed: {exc}")


threading.Thread(target=_periodic_backup_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def headmaster_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != ROLE_HEADMASTER:
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def generate_temp_password(length=8):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# SECURITY: force password change for temporary passwords, and prevent
# cached/back-button access to protected pages after logout.
# ---------------------------------------------------------------------------
@app.before_request
def enforce_password_change():
    if "user_id" in session:
        if request.endpoint in ("change_password", "logout", "static", "login", None):
            return
        conn = get_db()
        row = conn.execute(
            "SELECT must_change_password FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
        conn.close()
        if row and row["must_change_password"]:
            flash("You must change your temporary password before continuing.", "warning")
            return redirect(url_for("change_password"))


@app.after_request
def add_no_cache_headers(response):
    # Prevents the browser from serving a cached authenticated page (via the
    # back/forward button) after the user has logged out.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Pending Review",
    "under_review": "Pending Review",
    "approved": "Approved",
    "returned": "Returned for Correction",
}


def get_status_history(conn, exam_id, class_id):
    """Return the full audit trail (submit / review / approve / return) for
    a given exam+class, most recent first — this is the permanent history
    the Headmaster and Class Teacher can both see (not a fleeting notice)."""
    exact = f"exam={exam_id} class={class_id}"
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action IN "
        "('SUBMIT_RESULTS','START_REVIEW','APPROVE_RESULTS','RETURN_RESULTS') "
        "AND (details = ? OR details LIKE ?) ORDER BY id DESC",
        (exact, exact + ":%"),
    ).fetchall()
    return rows


_LOG_DETAILS_EXAM_CLASS_RE = re.compile(r"^exam=(\d+) class=(\d+)(.*)$")


def humanize_log_details(conn, details):
    """Display-only translation of the raw 'exam=<id> class=<id>' detail
    text that ENTER_MARKS/SUBMIT_RESULTS/START_REVIEW/APPROVE_RESULTS/
    RETURN_RESULTS write to the audit log, into a readable exam+class
    label (e.g. 'SECOND MID TERM 2026 - Standard Two A').

    This intentionally does NOT change what gets WRITTEN to audit_log -
    get_status_history() above still matches against the exact raw
    'exam=<id> class=<id>' text, so changing the stored format would
    silently break that lookup. This only reformats a copy for on-screen
    display (Dashboard "Recent Activities" and the Audit Log page).
    """
    match = _LOG_DETAILS_EXAM_CLASS_RE.match(details or "")
    if not match:
        return details
    exam_id, class_id, rest = int(match.group(1)), int(match.group(2)), match.group(3)
    exam = conn.execute(
        "SELECT exam_type, academic_year FROM examinations WHERE id=?", (exam_id,)
    ).fetchone()
    cls = conn.execute("SELECT name FROM classes WHERE id=?", (class_id,)).fetchone()
    exam_label = f"{exam['exam_type']} {exam['academic_year']}" if exam else f"Exam #{exam_id}"
    class_label = cls["name"] if cls else f"Class #{class_id}"
    return f"{exam_label} - {class_label}{rest}"


def humanize_logs(conn, rows):
    """Convert a list of audit_log Row objects into plain dicts with their
    'details' field passed through humanize_log_details() for display."""
    out = []
    for row in rows:
        d = dict(row)
        d["details"] = humanize_log_details(conn, d.get("details"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# LOGO / UPLOADED MEDIA
# A logo_path in settings is either:
#   "images/xxx.png"  -> the default logo bundled with the app code itself
#                         (served the normal Flask-static way)
#   "uploads/xxx.png" -> a logo the Headmaster uploaded from Settings - real
#                         school data, stored on DATA_DIR (persistent volume
#                         in production) and served via /media/<file> below,
#                         so it survives redeploys exactly like the database.
# ---------------------------------------------------------------------------
def resolve_logo_url(logo_path):
    logo_path = logo_path or "images/logo.png"
    if logo_path.startswith("uploads/"):
        return url_for("serve_uploaded_media", filename=logo_path[len("uploads/"):])
    return url_for("static", filename=logo_path)


def resolve_user_photo_url(photo_path):
    """Returns None (so templates fall back to the initials avatar) when the
    user has never uploaded a profile photo - never guesses a default image."""
    if not photo_path:
        return None
    if photo_path.startswith("uploads/"):
        return url_for("serve_uploaded_media", filename=photo_path[len("uploads/"):])
    return None


def resolve_logo_abs_path(logo_path):
    """Absolute filesystem path for the current logo - used when building PDFs."""
    logo_path = logo_path or "images/logo.png"
    if logo_path.startswith("uploads/"):
        return os.path.join(Config.UPLOAD_DIR, logo_path[len("uploads/"):])
    return os.path.join(Config.STATIC_IMAGES_DIR, os.path.basename(logo_path))


@app.errorhandler(404)
def handle_404(exc):
    return render_template(
        "error.html",
        title="Page Not Found",
        heading="Page Not Found",
        message="The page you are looking for does not exist or may have moved.",
        back_url=url_for("index"),
    ), 404


@app.errorhandler(500)
def handle_500(exc):
    print(f"[Kisauni] 500 error: {exc}")
    return render_template(
        "error.html",
        title="Server Error",
        heading="Error Occurred",
        message="Something went wrong on our side. Please try again later. "
                "If the problem continues, contact the system administrator.",
        back_url=url_for("index"),
    ), 500


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Catch-all safety net: covers database lock/corruption issues, lost
    # internet/host hiccups, and any other error we did not anticipate, so
    # the user never sees a raw stack trace/blank page - just a clear,
    # friendly message telling them what to do next.
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    print(f"[Kisauni] Unhandled error: {exc}")
    return render_template(
        "error.html",
        title="Error Occurred",
        heading="Error Occurred",
        message="Error occurred, please try again later. If you keep seeing this, "
                "check your internet connection or contact the system administrator.",
        back_url=url_for("index"),
    ), 500


@app.route("/media/<path:filename>")
def serve_uploaded_media(filename):
    """Serves school-uploaded files (currently just the custom logo) from
    the persistent DATA_DIR/uploads folder - separate from Flask's normal
    /static/ folder, which lives with the app code and is not persistent."""
    return send_from_directory(Config.UPLOAD_DIR, filename)


def asset_version(rel_path):
    """Returns the static file's last-modified time as an integer, used as
    a cache-busting ?v= query string on CSS/JS links. Browsers (mobile
    ones especially) can keep serving an old cached style.css/script.js
    even after we deploy a fix, since Flask's default static URLs never
    change - this makes the URL itself change whenever the file's content
    changes, forcing a fresh download instead of a stale cached copy."""
    try:
        full_path = os.path.join(app.static_folder, rel_path)
        return int(os.path.getmtime(full_path))
    except OSError:
        return 0


@app.context_processor
def inject_globals():
    # Wrapped in try/except so that a temporary database hiccup (locked file,
    # disk hiccup on the free host, etc.) never turns into a hard crash on
    # every single page - the layout still renders with sensible defaults
    # and the user sees a normal page (or our friendly error page) instead
    # of a blank/broken screen.
    try:
        conn = get_db()
        school_name = get_setting(conn, "school_name", "KISAUNI PRIMARY SCHOOL")
        academic_year = get_setting(conn, "academic_year", "2026")
        logo_path = get_setting(conn, "logo_path", "images/logo.png")
        conn.close()
    except Exception as exc:
        print(f"[Kisauni] inject_globals DB error: {exc}")
        school_name = "KISAUNI PRIMARY SCHOOL"
        academic_year = "2026"
        logo_path = "images/logo.png"
    return dict(
        school_name=school_name,
        academic_year=academic_year,
        logo_path=logo_path,
        logo_url=resolve_logo_url,
        user_photo_url=resolve_user_photo_url,
        asset_version=asset_version,
        session_user=current_user(),
        ROLE_HEADMASTER=ROLE_HEADMASTER,
        ROLE_CLASS_TEACHER=ROLE_CLASS_TEACHER,
        STATUS_LABELS=STATUS_LABELS,
        IDLE_TIMEOUT_MINUTES=Config.IDLE_TIMEOUT_MINUTES,
    )


# ---------------------------------------------------------------------------
# AUTH ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

        # --- Brute-force lockout check --------------------------------
        # If this account is currently locked (too many recent wrong
        # passwords), refuse the attempt WITHOUT even checking the
        # password - that's what keeps someone from just retrying past
        # the lock. Once the lock has naturally expired, fall through
        # and let this attempt be judged normally (and clear the old
        # lock/counter so a correct password here logs them straight in).
        if user and user["locked_until"]:
            locked_until = datetime.strptime(user["locked_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
            if now() < locked_until:
                minutes_left = max(1, int((locked_until - now()).total_seconds() // 60) + 1)
                flash(f"Too many failed attempts. This account is locked - try again in about "
                      f"{minutes_left} minute(s).", "danger")
                conn.close()
                return render_template("login.html")
            conn.execute("UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE id=?", (user["id"],))
            conn.commit()

        if not user or not check_password_hash(user["password_hash"], password):
            # Same generic message either way (unknown username vs wrong
            # password) so a login attempt can't be used to find out which
            # usernames exist. Only an EXISTING account's counter advances.
            if user:
                attempts = (user["failed_login_attempts"] or 0) + 1
                if attempts >= Config.MAX_LOGIN_ATTEMPTS:
                    locked_until_str = (now() + timedelta(minutes=Config.LOGIN_LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE users SET failed_login_attempts=?, locked_until=? WHERE id=?",
                        (attempts, locked_until_str, user["id"])
                    )
                    conn.commit()
                    log_action(conn, user, "LOGIN_LOCKED",
                               f"{user['username']} account locked for {Config.LOGIN_LOCKOUT_MINUTES} "
                               f"minutes after {attempts} failed login attempts")
                    flash(f"Too many failed attempts. This account is now locked for "
                          f"{Config.LOGIN_LOCKOUT_MINUTES} minutes.", "danger")
                else:
                    conn.execute(
                        "UPDATE users SET failed_login_attempts=? WHERE id=?", (attempts, user["id"])
                    )
                    conn.commit()
                    flash("Incorrect username or password.", "danger")
            else:
                flash("Incorrect username or password.", "danger")
            conn.close()
            return render_template("login.html")
        if not user["active"]:
            flash("This account has been deactivated. Please contact the Headmaster.", "danger")
            conn.close()
            return render_template("login.html")

        # Correct password - clear any leftover failed-attempt count.
        if user["failed_login_attempts"] or user["locked_until"]:
            conn.execute("UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE id=?", (user["id"],))
            conn.commit()

        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["full_name"] = user["full_name"]
        session["class_id"] = user["class_id"]
        # session.permanent = True + PERMANENT_SESSION_LIFETIME (config.py)
        # makes the session cookie carry a real, server-checked expiry
        # (IDLE_TIMEOUT_MINUTES) instead of only relying on the browser
        # fully closing - phones and some desktop browsers restore an open
        # tab/session on reopen regardless, so that alone wasn't enough to
        # guarantee a fresh login after someone leaves the app. With this,
        # the cookie stops being valid on its own once the timeout passes,
        # so returning later (however you got back in) always requires
        # logging in again - while SESSION_REFRESH_EACH_REQUEST keeps
        # renewing it during genuine active use, so it never interrupts
        # someone mid-task.
        session.permanent = True
        log_action(conn, user, "LOGIN", f"{user['username']} logged in")
        conn.close()
        if user["must_change_password"]:
            flash("Welcome! For your security, please set a new password before continuing.", "info")
            return redirect(url_for("change_password"))
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    user = current_user()
    if user:
        conn = get_db()
        log_action(conn, user, "LOGOUT", f"{user['username']} logged out")
        conn.close()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    # SECURITY: this page used to generate a brand-new password and show it
    # right there on screen to WHOEVER typed in a username - with no proof
    # they were actually that person. That let anyone take over any
    # account (e.g. "headmaster") just by knowing/guessing the username.
    #
    # Now it never changes or reveals a password by itself. It only records
    # that a reset was asked for (so the Headmaster has a record of it),
    # and tells the person to see the Headmaster, who can set a new
    # temporary password for them from Users -> Edit (that flow already
    # forces the person to choose their own new password on next login,
    # exactly like before - nothing about that part changes).
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if username:
            conn = get_db()
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            # Same response whether or not the username exists/is active -
            # so this page can't be used to check which usernames are real.
            if user and user["active"]:
                log_action(conn, user, "PASSWORD_RESET_REQUESTED",
                           f"{user['username']} asked for a password reset at the login page")
            conn.close()
        flash("Request received. Please see the Headmaster in person (or call the school office) "
              "to have your password reset - for security, it can no longer be reset from this "
              "page automatically.", "info")
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html", temp_password=None)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        new_username = request.form.get("new_username", "").strip()

        if not check_password_hash(user["password_hash"], current):
            flash("Your current password is incorrect.", "danger")
        elif len(new) < 4:
            flash("New password must be at least 4 characters long.", "danger")
        elif new != confirm:
            flash("New password and confirmation do not match.", "danger")
        elif not new_username:
            flash("Username cannot be empty.", "danger")
        else:
            duplicate = conn.execute(
                "SELECT 1 FROM users WHERE username=? AND id != ?", (new_username, user["id"])
            ).fetchone()
            if duplicate:
                flash("That username is already taken by another account. Choose a different one.", "danger")
                conn.close()
                return render_template("change_password.html", user=user)

            old_username = user["username"]
            conn.execute(
                "UPDATE users SET password_hash=?, username=?, must_change_password=0 WHERE id=?",
                (generate_password_hash(new), new_username, user["id"]),
            )
            conn.commit()
            detail = "User changed own password"
            if new_username != old_username:
                detail += f" and username ({old_username} -> {new_username})"
            log_action(conn, user, "CHANGE_PASSWORD", detail)
            flash("Your details have been updated successfully. Please remember your new username and password.", "success")
            conn.close()
            return redirect(url_for("dashboard"))
        conn.close()
        return render_template("change_password.html", user=user)
    conn.close()
    return render_template("change_password.html", user=user)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

    if request.method == "POST":
        action = request.form.get("action", "upload_photo")

        if action == "remove_photo":
            old_photo = user["photo_path"]
            conn.execute("UPDATE users SET photo_path=NULL WHERE id=?", (user["id"],))
            conn.commit()
            if old_photo and old_photo.startswith("uploads/"):
                old_abs = os.path.join(Config.UPLOAD_DIR, old_photo[len("uploads/"):])
                if os.path.exists(old_abs):
                    try:
                        os.remove(old_abs)
                    except OSError:
                        pass
            log_action(conn, user, "UPDATE_PROFILE", f"{user['username']} removed their profile photo")
            conn.close()
            flash("Profile photo removed.", "info")
            return redirect(url_for("profile"))

        photo = request.files.get("photo")
        if not photo or not photo.filename:
            flash("Please choose an image file first.", "danger")
            conn.close()
            return redirect(url_for("profile"))

        allowed_ext = {".png", ".jpg", ".jpeg"}
        ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
        if ext not in allowed_ext:
            flash("Only PNG or JPG images are allowed for profile photos.", "danger")
            conn.close()
            return redirect(url_for("profile"))

        # 2MB limit, kept generous but small enough to stay friendly to the
        # free hosting plan's storage/CPU limits.
        photo.seek(0, os.SEEK_END)
        size_bytes = photo.tell()
        photo.seek(0)
        if size_bytes > 2 * 1024 * 1024:
            flash("That image is too large - please use a photo under 2MB.", "danger")
            conn.close()
            return redirect(url_for("profile"))

        os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
        filename = f"user_{user['id']}_{int(time.time())}{ext}"
        save_path = os.path.join(Config.UPLOAD_DIR, filename)
        photo.save(save_path)

        try:
            from PIL import Image as PILImage
            with PILImage.open(save_path) as im:
                im.verify()
        except Exception:
            os.remove(save_path)
            flash("That file could not be read as an image - please upload a proper PNG or JPG photo.", "danger")
            conn.close()
            return redirect(url_for("profile"))

        old_photo = user["photo_path"]
        conn.execute("UPDATE users SET photo_path=? WHERE id=?", (f"uploads/{filename}", user["id"]))
        conn.commit()
        # Clean up the previous photo file now that the new one is safely saved.
        if old_photo and old_photo.startswith("uploads/"):
            old_abs = os.path.join(Config.UPLOAD_DIR, old_photo[len("uploads/"):])
            if os.path.exists(old_abs):
                try:
                    os.remove(old_abs)
                except OSError:
                    pass

        log_action(conn, user, "UPDATE_PROFILE", f"{user['username']} updated their profile photo")
        conn.close()
        flash("Profile photo updated.", "success")
        return redirect(url_for("profile"))

    cls = None
    if user["class_id"]:
        cls = conn.execute("SELECT * FROM classes WHERE id=?", (user["class_id"],)).fetchone()
    conn.close()
    return render_template("profile.html", user=user, cls=cls)


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    stats = {}
    stats["students"] = conn.execute("SELECT COUNT(*) c FROM students WHERE active=1").fetchone()["c"]
    stats["teachers"] = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE role=? AND active=1", (ROLE_CLASS_TEACHER,)
    ).fetchone()["c"]
    stats["classes"] = conn.execute("SELECT COUNT(*) c FROM classes").fetchone()["c"]
    stats["subjects"] = conn.execute("SELECT COUNT(*) c FROM subjects").fetchone()["c"]
    # Examinations / Pending / Approved on the dashboard reflect the CURRENT
    # academic year only, so switching the year in Settings gives a fresh
    # overview instead of an all-time total mixed with old years. Full
    # historical data for every year still stays available in full under
    # "Exam Records / Search".
    stats["exams"] = conn.execute(
        "SELECT COUNT(*) c FROM examinations WHERE academic_year=?", (current_academic_year,)
    ).fetchone()["c"]
    stats["pending"] = conn.execute("""
        SELECT COUNT(*) c FROM exam_class_status ecs JOIN examinations e ON e.id = ecs.exam_id
        WHERE ecs.status IN ('submitted','under_review') AND e.academic_year=?
    """, (current_academic_year,)).fetchone()["c"]
    stats["approved"] = conn.execute("""
        SELECT COUNT(*) c FROM exam_class_status ecs JOIN examinations e ON e.id = ecs.exam_id
        WHERE ecs.status='approved' AND e.academic_year=?
    """, (current_academic_year,)).fetchone()["c"]

    # Real per-class active-student counts, ordered the same way as everywhere
    # else in the app (sort_order) - powers the "Students by Class" chart.
    class_breakdown = conn.execute("""
        SELECT c.name, COUNT(s.id) c
        FROM classes c LEFT JOIN students s ON s.class_id = c.id AND s.active=1
        GROUP BY c.id ORDER BY c.sort_order
    """).fetchall()

    # Real result-workflow status counts for the current academic year only -
    # powers the "Results Status Overview" chart (replaces any fabricated
    # month-by-month trend, since the system doesn't keep that history).
    status_rows = conn.execute("""
        SELECT ecs.status, COUNT(*) c FROM exam_class_status ecs
        JOIN examinations e ON e.id = ecs.exam_id
        WHERE e.academic_year=? GROUP BY ecs.status
    """, (current_academic_year,)).fetchall()
    status_breakdown = {"draft": 0, "submitted": 0, "under_review": 0, "approved": 0, "returned": 0}
    for row in status_rows:
        status_breakdown[row["status"]] = row["c"]

    my_class = None
    my_class_students = 0
    returned_results = []
    my_class_analytics = None
    my_class_latest_exam = None
    if session["role"] == ROLE_CLASS_TEACHER and session.get("class_id"):
        my_class = conn.execute("SELECT * FROM classes WHERE id=?", (session["class_id"],)).fetchone()
        my_class_students = conn.execute(
            "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=1", (session["class_id"],)
        ).fetchone()["c"]
        returned_results = conn.execute("""
            SELECT ecs.*, e.exam_type, e.academic_year, e.id as exam_id_val
            FROM exam_class_status ecs JOIN examinations e ON ecs.exam_id = e.id
            WHERE ecs.class_id=? AND ecs.status='returned'
            ORDER BY e.id DESC
        """, (session["class_id"],)).fetchall()

        # Auto-refreshed performance snapshot: whichever exam most recently
        # had marks recorded for this class, with no separate "generate"
        # step - it just reflects whatever is in `marks` right now.
        my_class_latest_exam = conn.execute("""
            SELECT e.* FROM examinations e
            WHERE EXISTS (
                SELECT 1 FROM marks m
                WHERE m.exam_id = e.id AND m.class_id = ? AND m.score IS NOT NULL
            )
            ORDER BY e.id DESC LIMIT 1
        """, (session["class_id"],)).fetchone()
        if my_class_latest_exam:
            my_class_analytics = compute_class_analytics(conn, session["class_id"], my_class_latest_exam["id"])

    # School-wide class ranking (leaderboard) for the same latest exam - lets
    # a Class Teacher see which class is leading overall, and where their
    # own class stands against every other class.
    class_ranking = []
    leading_class = None
    my_class_rank = None
    if my_class_latest_exam:
        class_ranking = compute_class_ranking(conn, my_class_latest_exam["id"])
        leading_class = class_ranking[0] if class_ranking else None
        my_class_rank = next(
            (e for e in class_ranking if e["cls"]["id"] == session.get("class_id")), None
        )

    recent_logs = humanize_logs(conn, conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 8"
    ).fetchall())
    offsite_days = days_since_last_offsite_download(conn) if session["role"] == ROLE_HEADMASTER else None
    conn.close()
    return render_template("dashboard.html", stats=stats, my_class=my_class,
                            my_class_students=my_class_students, recent_logs=recent_logs,
                            returned_results=returned_results, offsite_days=offsite_days,
                            class_breakdown=class_breakdown, status_breakdown=status_breakdown,
                            current_academic_year=current_academic_year,
                            my_class_analytics=my_class_analytics, my_class_latest_exam=my_class_latest_exam,
                            leading_class=leading_class, my_class_rank=my_class_rank,
                            class_ranking=class_ranking, class_ranking_total=len(class_ranking),
                            chart_colors=['#2563eb', '#16a34a', '#f59e0b', '#dc2626',
                                          '#6f42c1', '#0891b2', '#db2777', '#64748b'])


# ---------------------------------------------------------------------------
# STUDENTS
# ---------------------------------------------------------------------------
@app.route("/students")
@login_required
def students():
    conn = get_db()
    class_filter = request.args.get("class_id", "")
    search = request.args.get("q", "").strip()

    query = """
        SELECT s.*, c.name as class_name, c.sort_order
        FROM students s JOIN classes c ON s.class_id = c.id
        WHERE s.active = ?
    """
    show_removed = session["role"] == ROLE_HEADMASTER and request.args.get("status") == "removed"
    params = [0 if show_removed else 1]
    if session["role"] == ROLE_CLASS_TEACHER:
        query += " AND s.class_id = ?"
        params.append(session.get("class_id"))
    elif class_filter:
        query += " AND s.class_id = ?"
        params.append(class_filter)

    if search:
        query += " AND (s.full_name LIKE ? OR s.reg_no LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += (
        " ORDER BY c.sort_order ASC, "
        "CASE s.gender WHEN 'Male' THEN 0 WHEN 'Female' THEN 1 ELSE 2 END ASC, "
        "s.full_name COLLATE NOCASE ASC"
    )
    rows = conn.execute(query, params).fetchall()

    classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()

    # group students alphabetically per class (Standard order)
    grouped = {}
    for r in rows:
        grouped.setdefault(r["class_name"], []).append(r)

    conn.close()
    return render_template("students.html", grouped=grouped, classes=classes,
                            class_filter=class_filter, search=search, show_removed=show_removed)


@app.route("/api/detect-gender")
@login_required
def api_detect_gender():
    name = request.args.get("name", "")
    gender = detect_gender(name)
    return jsonify({"gender": gender})


@app.route("/api/keep-alive")
@login_required
def api_keep_alive():
    # Being a normal @login_required request is what matters here: with
    # SESSION_REFRESH_EACH_REQUEST=True (config.py) and session.permanent
    # set at login, Flask renews the server-checked session expiry on every
    # request - including this one. The front-end idle timer (script.js)
    # pings this quietly while someone is genuinely active (typing/
    # clicking) but not navigating to a new page, so a long form doesn't
    # get logged out from under them mid-entry, and separately shows its
    # own "you're about to be logged out" warning after real inactivity.
    return jsonify({"ok": True})


@app.route("/api/students-by-class/<int:class_id>")
@login_required
def api_students_by_class(class_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, reg_no, full_name FROM students WHERE class_id=? AND active=1 "
        "ORDER BY CASE gender WHEN 'Male' THEN 0 WHEN 'Female' THEN 1 ELSE 2 END ASC, full_name COLLATE NOCASE ASC",
        (class_id,),
    ).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "reg_no": r["reg_no"], "full_name": r["full_name"]} for r in rows])


@app.route("/api/students-with-marks/<int:class_id>/<int:exam_id>")
@login_required
def api_students_with_marks(class_id, exam_id):
    """Returns only students in this class who actually have at least one
    recorded mark for this specific examination - used by the Reports page
    so the Student dropdown never shows a student who wasn't part of that
    exam (e.g. they were in a different exam period, or no marks were
    entered for them at all)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT s.id, s.reg_no, s.full_name FROM students s
        JOIN marks m ON m.student_id = s.id
        WHERE s.class_id=? AND m.exam_id=? AND s.active=1 AND m.score IS NOT NULL
        ORDER BY CASE s.gender WHEN 'Male' THEN 0 WHEN 'Female' THEN 1 ELSE 2 END ASC,
                 s.full_name COLLATE NOCASE ASC
    """, (class_id, exam_id)).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "reg_no": r["reg_no"], "full_name": r["full_name"]} for r in rows])


@app.route("/students/add", methods=["GET", "POST"])
@login_required
def add_student():
    conn = get_db()
    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()

    if request.method == "POST":
        reg_no = request.form.get("reg_no", "").strip()
        full_name = request.form.get("full_name", "").strip()
        class_id = request.form.get("class_id")
        gender = request.form.get("gender", "").strip()
        gender_confirmed = 1 if request.form.get("gender_confirmed") == "on" else 0

        error = None
        if not reg_no or not full_name or not class_id:
            error = "Please fill in Reg No, Full Name and Class."
        elif conn.execute("SELECT 1 FROM students WHERE reg_no=?", (reg_no,)).fetchone():
            error = "This Reg No is already registered in the system."
        elif session["role"] == ROLE_CLASS_TEACHER and str(class_id) != str(session.get("class_id")):
            error = "You can only register students for your own assigned class."

        if error:
            flash(error, "danger")
            conn.close()
            return render_template("student_form.html", classes=classes, student=request.form, mode="add")

        if not gender:
            gender = detect_gender(full_name) or "Unknown"

        conn.execute(
            "INSERT INTO students (reg_no, full_name, gender, gender_confirmed, class_id, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (reg_no, full_name, gender, gender_confirmed, class_id,
             now_str()),
        )
        conn.commit()
        log_action(conn, current_user(), "ADD_STUDENT", f"{full_name} ({reg_no})")
        conn.close()
        flash(f"Student {full_name} registered successfully.", "success")
        return redirect(url_for("students"))

    conn.close()
    return render_template("student_form.html", classes=classes, student=None, mode="add")


@app.route("/students/add-bulk", methods=["GET", "POST"])
@login_required
def add_students_bulk():
    """Lets a teacher type in several students (Reg No, Name, Gender) for one
    class and save them all in a single click, instead of repeating the
    full add-student form one student at a time."""
    conn = get_db()
    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()

    if request.method == "POST":
        class_id = request.form.get("class_id")
        reg_nos = request.form.getlist("reg_no[]")
        full_names = request.form.getlist("full_name[]")
        genders = request.form.getlist("gender[]")

        if session["role"] == ROLE_CLASS_TEACHER and str(class_id) != str(session.get("class_id")):
            flash("You can only register students for your own assigned class.", "danger")
            conn.close()
            return render_template("student_form_bulk.html", classes=classes, rows=[])

        if not class_id:
            flash("Please select a Class before saving.", "danger")
            conn.close()
            return render_template("student_form_bulk.html", classes=classes, rows=[])

        rows_out = []       # what we redisplay if something goes wrong
        to_insert = []      # (reg_no, full_name, gender) that passed validation
        seen_reg_nos = set()
        errors = []

        for i, (reg_no, full_name, gender) in enumerate(zip(reg_nos, full_names, genders), start=1):
            reg_no = reg_no.strip()
            full_name = full_name.strip()
            gender = gender.strip()
            rows_out.append({"reg_no": reg_no, "full_name": full_name, "gender": gender})

            if not reg_no and not full_name:
                continue  # a blank row left over in the table - just skip it silently

            if not reg_no or not full_name or not gender:
                errors.append(f"Row {i}: Reg No, Full Name and Gender are all required.")
                continue
            if reg_no in seen_reg_nos:
                errors.append(f"Row {i}: Reg No '{reg_no}' is duplicated in this list.")
                continue
            if conn.execute("SELECT 1 FROM students WHERE reg_no=?", (reg_no,)).fetchone():
                errors.append(f"Row {i}: Reg No '{reg_no}' is already registered in the system.")
                continue

            seen_reg_nos.add(reg_no)
            to_insert.append((reg_no, full_name, gender))

        if not to_insert and not errors:
            flash("Please add at least one student before saving.", "warning")
            conn.close()
            return render_template("student_form_bulk.html", classes=classes, rows=rows_out,
                                    selected_class=class_id)

        for reg_no, full_name, gender in to_insert:
            conn.execute(
                "INSERT INTO students (reg_no, full_name, gender, gender_confirmed, class_id, active, created_at) "
                "VALUES (?, ?, ?, 1, ?, 1, ?)",
                (reg_no, full_name, gender, class_id, now_str()),
            )
        conn.commit()
        if to_insert:
            log_action(conn, current_user(), "ADD_STUDENTS_BULK",
                       f"{len(to_insert)} student(s) added to class_id={class_id}")

        if errors:
            for e in errors:
                flash(e, "danger")
            if to_insert:
                flash(f"{len(to_insert)} student(s) saved successfully. "
                      f"Please fix the row(s) above and save again for the rest.", "success")
            # only redisplay the rows that failed, so already-saved ones aren't duplicated
            saved_reg_nos = {r[0] for r in to_insert}
            remaining_rows = [r for r in rows_out if r["reg_no"] not in saved_reg_nos]
            conn.close()
            return render_template("student_form_bulk.html", classes=classes, rows=remaining_rows,
                                    selected_class=class_id)

        conn.close()
        flash(f"{len(to_insert)} student(s) registered successfully.", "success")
        return redirect(url_for("students"))

    conn.close()
    return render_template("student_form_bulk.html", classes=classes, rows=[])


@app.route("/students/edit/<int:student_id>", methods=["GET", "POST"])
@login_required
def edit_student(student_id):
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        abort(404)
    if session["role"] == ROLE_CLASS_TEACHER and student["class_id"] != session.get("class_id"):
        conn.close()
        flash("You can only edit students in your own assigned class.", "danger")
        return redirect(url_for("students"))

    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        class_id = request.form.get("class_id")
        gender = request.form.get("gender", "").strip()
        gender_confirmed = 1 if request.form.get("gender_confirmed") == "on" else 0
        reg_no = request.form.get("reg_no", "").strip()

        dup = conn.execute("SELECT 1 FROM students WHERE reg_no=? AND id != ?", (reg_no, student_id)).fetchone()
        if dup:
            flash("This Reg No is already used by another student.", "danger")
            conn.close()
            return render_template("student_form.html", classes=classes, student=student, mode="edit")

        conn.execute(
            "UPDATE students SET reg_no=?, full_name=?, gender=?, gender_confirmed=?, class_id=? WHERE id=?",
            (reg_no, full_name, gender, gender_confirmed, class_id, student_id),
        )
        conn.commit()
        log_action(conn, current_user(), "EDIT_STUDENT", f"{full_name} ({reg_no})")
        conn.close()
        flash("Student details updated successfully.", "success")
        return redirect(url_for("students"))

    conn.close()
    return render_template("student_form.html", classes=classes, student=student, mode="edit")


@app.route("/students/delete/<int:student_id>", methods=["POST"])
@login_required
def delete_student(student_id):
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        abort(404)
    if session["role"] == ROLE_CLASS_TEACHER and student["class_id"] != session.get("class_id"):
        conn.close()
        flash("You can only remove students in your own assigned class.", "danger")
        return redirect(url_for("students"))

    # soft-delete so historical marks/results remain intact
    conn.execute("UPDATE students SET active=0 WHERE id=?", (student_id,))
    conn.commit()
    log_action(conn, current_user(), "DELETE_STUDENT", f"{student['full_name']} ({student['reg_no']})")
    conn.close()
    flash(f"Student {student['full_name']} has been removed.", "info")
    return redirect(url_for("students"))


@app.route("/students/restore/<int:student_id>", methods=["POST"])
@headmaster_required
def restore_student(student_id):
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        abort(404)
    conn.execute("UPDATE students SET active=1 WHERE id=?", (student_id,))
    conn.commit()
    log_action(conn, current_user(), "RESTORE_STUDENT", f"{student['full_name']} ({student['reg_no']})")
    conn.close()
    flash(f"{student['full_name']} has been restored to the active list.", "success")
    return redirect(url_for("students", status="removed"))


@app.route("/students/purge/<int:student_id>", methods=["POST"])
@headmaster_required
def purge_student(student_id):
    """PERMANENTLY erases a student and every mark ever recorded for them -
    unlike the normal Remove action, this cannot be undone. Only usable on a
    student who has already been removed first (active=0) - a deliberate
    extra safety step so this can never be one click away from an active,
    real student's record. Intended for cleaning up test/junk data, not for
    real students leaving the school (use Remove for that instead, which
    preserves their history)."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        abort(404)
    if student["active"]:
        conn.close()
        flash("This student is still active - remove them first, then you can permanently delete them.",
              "danger")
        return redirect(url_for("students"))

    conn.execute("DELETE FROM marks WHERE student_id=?", (student_id,))
    conn.execute("DELETE FROM students WHERE id=?", (student_id,))
    conn.commit()
    log_action(conn, current_user(), "PURGE_STUDENT", f"{student['full_name']} ({student['reg_no']})")
    conn.close()
    flash(f"{student['full_name']} and all their marks have been permanently deleted.", "info")
    return redirect(url_for("students", status="removed"))


# ---------------------------------------------------------------------------
# CLASSES  (headmaster assigns class teachers)
# ---------------------------------------------------------------------------
@app.route("/classes", methods=["GET", "POST"])
@headmaster_required
def classes():
    conn = get_db()
    if request.method == "POST":
        class_id = request.form.get("class_id")
        teacher_id = request.form.get("teacher_id") or None
        conn.execute("UPDATE classes SET teacher_id=? WHERE id=?", (teacher_id, class_id))
        # keep users.class_id in sync with the assignment
        conn.execute("UPDATE users SET class_id=NULL WHERE class_id=?", (class_id,))
        if teacher_id:
            conn.execute("UPDATE users SET class_id=? WHERE id=?", (class_id, teacher_id))
        conn.commit()
        log_action(conn, current_user(), "ASSIGN_CLASS_TEACHER", f"class_id={class_id} teacher_id={teacher_id}")
        flash("Class Teacher assigned to class successfully.", "success")
        conn.close()
        return redirect(url_for("classes"))

    rows = conn.execute("""
        SELECT c.*, u.full_name as teacher_name,
        (SELECT COUNT(*) FROM students s WHERE s.class_id=c.id AND s.active=1) as student_count
        FROM classes c LEFT JOIN users u ON c.teacher_id = u.id
        ORDER BY c.sort_order
    """).fetchall()
    teachers = conn.execute(
        "SELECT * FROM users WHERE role='class_teacher' AND active=1 ORDER BY full_name"
    ).fetchall()
    conn.close()
    return render_template("classes.html", classes=rows, teachers=teachers,
                            ordinal_words=ORDINAL_WORDS, num_standards=NUM_STANDARDS)


@app.route("/classes/add", methods=["POST"])
@headmaster_required
def add_class():
    conn = get_db()
    try:
        standard = int(request.form.get("standard", ""))
    except (TypeError, ValueError):
        standard = None
    stream = request.form.get("stream", "").strip().upper()

    if not standard or standard < 1 or standard > NUM_STANDARDS or not stream:
        flash("Please choose a valid Standard and give the new Stream a letter/name (e.g. C).", "danger")
        conn.close()
        return redirect(url_for("classes"))

    word = ORDINAL_WORDS.get(standard)
    name = f"Standard {word} {stream}"
    if conn.execute("SELECT 1 FROM classes WHERE name=?", (name,)).fetchone():
        flash(f"'{name}' already exists.", "warning")
        conn.close()
        return redirect(url_for("classes"))

    max_sort = conn.execute("SELECT COALESCE(MAX(sort_order),0) m FROM classes").fetchone()["m"]
    cur = conn.execute(
        "INSERT INTO classes (name, standard, stream, sort_order) VALUES (?, ?, ?, ?)",
        (name, standard, stream, max_sort + 1),
    )
    class_id = cur.lastrowid
    for sname in subjects_for_standard(standard):
        srow = conn.execute("SELECT id FROM subjects WHERE name=?", (sname,)).fetchone()
        if srow:
            conn.execute(
                "INSERT OR IGNORE INTO class_subjects (class_id, subject_id) VALUES (?, ?)",
                (class_id, srow["id"]),
            )
    conn.commit()
    log_action(conn, current_user(), "ADD_CLASS", name)
    flash(f"Class '{name}' added successfully with its subjects already set up.", "success")
    conn.close()
    return redirect(url_for("classes"))


@app.route("/classes/delete/<int:class_id>", methods=["POST"])
@headmaster_required
def delete_class(class_id):
    conn = get_db()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not cls:
        conn.close()
        abort(404)

    active_students = conn.execute(
        "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=1", (class_id,)
    ).fetchone()["c"]
    if active_students > 0:
        flash(f"Cannot remove '{cls['name']}' - it still has {active_students} student(s) in it. "
              f"Move or remove them first.", "danger")
        conn.close()
        return redirect(url_for("classes"))

    has_marks = conn.execute("""
        SELECT 1 FROM marks m JOIN students s ON m.student_id = s.id
        WHERE s.class_id=? LIMIT 1
    """, (class_id,)).fetchone()
    if has_marks:
        flash(f"Cannot remove '{cls['name']}' - historical marks exist for students who were in "
              f"this class. It will stay in the system to protect that data.", "danger")
        conn.close()
        return redirect(url_for("classes"))

    # BUGFIX: a class can still have INACTIVE (soft-deleted/left-the-school)
    # students linked to it even once the active-student count above is 0 -
    # e.g. a student who transferred out. students.class_id is a required
    # (NOT NULL) foreign key to classes, so deleting the class while any
    # such student row still points to it used to crash with an unhandled
    # "FOREIGN KEY constraint failed" error (a 500 page) instead of telling
    # the Headmaster what was actually wrong. Check for that case explicitly
    # and explain it, the same way the active-student and has-marks checks
    # above already do.
    former_students = conn.execute(
        "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=0", (class_id,)
    ).fetchone()["c"]
    if former_students > 0:
        flash(f"Cannot remove '{cls['name']}' - {former_students} former student(s) who used to be "
              f"in this class are still on record here (to protect their history). It will stay in "
              f"the system.", "danger")
        conn.close()
        return redirect(url_for("classes"))

    conn.execute("UPDATE users SET class_id=NULL WHERE class_id=?", (class_id,))
    conn.execute("DELETE FROM class_subjects WHERE class_id=?", (class_id,))
    conn.execute("DELETE FROM classes WHERE id=?", (class_id,))
    conn.commit()
    log_action(conn, current_user(), "DELETE_CLASS", cls["name"])
    flash(f"Class '{cls['name']}' removed successfully.", "info")
    conn.close()
    return redirect(url_for("classes"))


# ---------------------------------------------------------------------------
# YEAR-END PROMOTION  (Headmaster only) - move every class's students up to
# the next Standard (same Stream where possible) for a new academic year,
# graduate Standard Seven out, optionally move each Class Teacher up with
# their students, and leave every source class empty and ready for new
# intake (e.g. new Standard One admissions).
# ---------------------------------------------------------------------------
def _class_can_promote(cls, current_academic_year):
    """Promotion is always allowed, any time, regardless of whether this
    class/year combination was already promoted before. The system
    deliberately does NOT remember or enforce a "one promotion per
    academic year" rule any more (Class Teachers and the Headmaster can
    both re-run promotion for the same class/year as many times as
    needed - e.g. after deleting and re-registering a batch of students).
    `last_promoted_year` is still recorded on each promotion for history/
    audit purposes, but nothing reads it to block a future promotion."""
    return True


def _guess_promotion_target(conn, cls, all_classes_by_name):
    """Best-guess next class for a given source class: same Stream, one
    Standard higher. Returns a class row, or None if Standard Seven (leaves
    school) or no sensible match exists (Headmaster picks manually)."""
    if not cls["standard"]:
        return None
    if cls["standard"] >= NUM_STANDARDS:
        return None  # Standard Seven -> graduates / leaves school
    next_word = ORDINAL_WORDS.get(cls["standard"] + 1)
    guess_name = f"Standard {next_word} {cls['stream']}"
    return all_classes_by_name.get(guess_name)


@app.route("/promote", methods=["GET", "POST"])
@login_required
def promote_students():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    all_classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    all_classes_by_id = {c["id"]: c for c in all_classes}
    all_classes_by_name = {c["name"]: c for c in all_classes}

    # --- Class Teacher: simplified "move my class's students to another
    # class" only. They cannot advance the school-wide academic year, seed
    # new examinations, or reassign which teacher runs which class - those
    # remain a Headmaster/Admin-only action (Users/Accounts and the full
    # promotion flow below).
    if session["role"] == ROLE_CLASS_TEACHER:
        my_class = None
        if session.get("class_id"):
            my_class = conn.execute("SELECT * FROM classes WHERE id=?", (session["class_id"],)).fetchone()
        if not my_class:
            conn.close()
            flash("You are not currently assigned to a class. Contact the Headmaster.", "warning")
            return redirect(url_for("dashboard"))

        student_count = conn.execute(
            "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=1", (my_class["id"],)
        ).fetchone()["c"]
        can_promote = _class_can_promote(my_class, current_academic_year)
        is_final_standard = bool(my_class["standard"]) and my_class["standard"] >= NUM_STANDARDS
        guess = None if is_final_standard else _guess_promotion_target(conn, my_class, all_classes_by_name)
        # Standard Seven is the last class in primary school - there is no
        # "Standard Eight" to move up to, so these students only ever
        # graduate/leave. Other classes are not offered as a destination at
        # all for this class.
        other_classes = [] if is_final_standard else [c for c in all_classes if c["id"] != my_class["id"]]

        def _render_my_class(**extra):
            return render_template("promote_my_class.html", my_class=my_class,
                                    student_count=student_count, other_classes=other_classes,
                                    guess=guess, current_academic_year=current_academic_year,
                                    can_promote=can_promote, is_final_standard=is_final_standard, **extra)

        if request.method == "POST":
            if not can_promote:
                flash("The Headmaster has not changed the academic year in Settings yet - "
                      "please wait until a new academic year is set before promoting your class.", "danger")
                conn.close()
                return _render_my_class()

            target_raw = request.form.get("target_class_id", "")

            if is_final_standard or target_raw == "graduate":
                if student_count == 0:
                    flash("Nothing to graduate - there are no students in this class.", "danger")
                    conn.close()
                    return _render_my_class()
                create_backup(reason=f"before_class_teacher_graduate_{my_class['id']}")
                conn.execute(
                    "UPDATE students SET active=0, leave_reason=? WHERE class_id=? AND active=1",
                    (f"Graduated / completed Standard Seven in {current_academic_year}", my_class["id"]),
                )
                conn.execute(
                    "UPDATE classes SET last_promoted_year=? WHERE id=?",
                    (current_academic_year, my_class["id"]),
                )
                conn.commit()
                log_action(conn, current_user(), "PROMOTE_MY_CLASS_GRADUATE",
                           f"class={my_class['id']} count={student_count} year={current_academic_year}")
                conn.close()
                flash(f"{student_count} student(s) marked as graduated (Class of {current_academic_year}).",
                      "success")
                return redirect(url_for("students"))

            try:
                target_id = int(target_raw)
            except (TypeError, ValueError):
                flash("Please select which class to move your students to.", "danger")
                conn.close()
                return _render_my_class()
            target = all_classes_by_id.get(target_id)
            if not target or target["id"] == my_class["id"] or student_count == 0:
                flash("Nothing to move - please check the class you selected.", "danger")
                conn.close()
                return _render_my_class()

            create_backup(reason=f"before_class_teacher_promote_{my_class['id']}_to_{target['id']}")
            conn.execute(
                "UPDATE students SET class_id=? WHERE class_id=? AND active=1",
                (target["id"], my_class["id"]),
            )
            conn.execute(
                "UPDATE classes SET last_promoted_year=? WHERE id=?",
                (current_academic_year, my_class["id"]),
            )
            conn.commit()
            log_action(conn, current_user(), "PROMOTE_MY_CLASS",
                       f"{my_class['id']} -> {target['id']} count={student_count} year={current_academic_year}")
            conn.close()
            flash(f"{student_count} student(s) moved from {my_class['name']} to {target['name']}.", "success")
            return redirect(url_for("students"))

        conn.close()
        return _render_my_class()

    # --- Headmaster/Admin: full whole-school academic-year advance below ---
    if session["role"] != ROLE_HEADMASTER:
        conn.close()
        abort(403)

    source_classes = []
    for cls in all_classes:
        count = conn.execute(
            "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=1", (cls["id"],)
        ).fetchone()["c"]
        if count > 0:
            guess = _guess_promotion_target(conn, cls, all_classes_by_name)
            source_classes.append({
                "cls": cls, "student_count": count, "guess": guess,
                "can_promote": _class_can_promote(cls, current_academic_year),
            })

    if request.method == "POST":
        new_academic_year_input = request.form.get("new_academic_year", "").strip()
        move_teachers = request.form.get("move_teachers") == "on"

        if new_academic_year_input and new_academic_year_input != current_academic_year:
            # Admin is advancing the year right here, in this same submission.
            new_academic_year = new_academic_year_input
            advancing_year_now = True
        else:
            # No new year typed - this only works if the Headmaster already
            # changed the academic year in Settings beforehand. Otherwise
            # there is nothing to promote to yet (locked, same as Class
            # Teachers) and we don't touch the settings table again.
            new_academic_year = current_academic_year
            advancing_year_now = False

        promotable = [item for item in source_classes if item["can_promote"]]
        if not advancing_year_now and not promotable:
            flash("Every class has already been promoted for the current academic year. "
                  "Enter a new academic year above, or change it first in Settings, before promoting again.",
                  "danger")
            conn.close()
            return render_template("promote.html", source_classes=source_classes,
                                    current_academic_year=current_academic_year, all_classes=all_classes)

        summary = []
        # Safety backup FIRST - promotion is a bulk, hard-to-reverse move of
        # every student in the school, so a restore point is taken before
        # anything is changed.
        create_backup(reason=f"before_promote_{current_academic_year}_to_{new_academic_year}")
        for item in source_classes:
            cls = item["cls"]
            if not item["can_promote"]:
                summary.append(f"{cls['name']}: skipped (already promoted for {current_academic_year})")
                continue
            target_raw = request.form.get(f"target_{cls['id']}", "")
            if target_raw == "graduate":
                conn.execute(
                    "UPDATE students SET active=0, leave_reason=? WHERE class_id=? AND active=1",
                    (f"Graduated / completed Standard Seven in {current_academic_year}", cls["id"]),
                )
                conn.execute("UPDATE classes SET last_promoted_year=? WHERE id=?",
                             (new_academic_year, cls["id"]))
                summary.append(f"{cls['name']}: {item['student_count']} student(s) graduated/left school")
                log_action(conn, current_user(), "PROMOTE_GRADUATE",
                           f"class={cls['id']} count={item['student_count']} year={current_academic_year}")
                continue

            try:
                target_id = int(target_raw)
            except (TypeError, ValueError):
                continue  # left blank/unset - this class is skipped, students stay put
            target = all_classes_by_id.get(target_id)
            if not target or target["id"] == cls["id"]:
                continue

            conn.execute(
                "UPDATE students SET class_id=? WHERE class_id=? AND active=1",
                (target["id"], cls["id"]),
            )
            conn.execute("UPDATE classes SET last_promoted_year=? WHERE id=?",
                         (new_academic_year, cls["id"]))
            if move_teachers and cls["teacher_id"] and not target["teacher_id"]:
                conn.execute("UPDATE classes SET teacher_id=? WHERE id=?", (cls["teacher_id"], target["id"]))
                conn.execute("UPDATE users SET class_id=? WHERE id=?", (target["id"], cls["teacher_id"]))
                conn.execute("UPDATE classes SET teacher_id=NULL WHERE id=?", (cls["id"],))
            summary.append(f"{cls['name']} -> {target['name']}: {item['student_count']} student(s) moved")
            log_action(conn, current_user(), "PROMOTE_CLASS",
                       f"{cls['id']} -> {target['id']} count={item['student_count']} year={current_academic_year}")

        if advancing_year_now:
            conn.execute("UPDATE settings SET value=? WHERE key='academic_year'", (new_academic_year,))
            log_action(conn, current_user(), "ADVANCE_ACADEMIC_YEAR",
                       f"{current_academic_year} -> {new_academic_year}")
        conn.commit()
        conn.close()
        year_note = f" Academic year is now {new_academic_year}." if advancing_year_now else ""
        flash("Promotion complete: " + "; ".join(summary) + "." + year_note, "success")
        return redirect(url_for("classes"))

    conn.close()
    return render_template("promote.html", source_classes=source_classes,
                            current_academic_year=current_academic_year, all_classes=all_classes)


# ---------------------------------------------------------------------------
# SUBJECTS
# ---------------------------------------------------------------------------
@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    conn = get_db()
    if request.method == "POST" and session["role"] == ROLE_HEADMASTER:
        action = request.form.get("action")
        if action == "add_subject":
            name = request.form.get("name", "").strip()
            class_ids = request.form.getlist("class_ids")
            if name:
                cur = conn.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
                sid = cur.lastrowid
                for cid in class_ids:
                    conn.execute("INSERT OR IGNORE INTO class_subjects (class_id, subject_id) VALUES (?, ?)", (cid, sid))
                conn.commit()
                log_action(conn, current_user(), "ADD_SUBJECT", name)
                flash(f"Subject '{name}' added successfully.", "success")
        elif action == "toggle":
            class_id = request.form.get("class_id")
            subject_id = request.form.get("subject_id")
            exists = conn.execute(
                "SELECT 1 FROM class_subjects WHERE class_id=? AND subject_id=?", (class_id, subject_id)
            ).fetchone()
            if exists:
                # Only allow REMOVING a subject from a class if no marks
                # have ever been recorded for that subject in that class -
                # protects marks a teacher has already entered/saved.
                has_marks = conn.execute("""
                    SELECT 1 FROM marks m JOIN students s ON m.student_id = s.id
                    WHERE s.class_id=? AND m.subject_id=? AND m.score IS NOT NULL LIMIT 1
                """, (class_id, subject_id)).fetchone()
                if has_marks:
                    subj = conn.execute("SELECT name FROM subjects WHERE id=?", (subject_id,)).fetchone()
                    cls = conn.execute("SELECT name FROM classes WHERE id=?", (class_id,)).fetchone()
                    flash(f"Cannot remove '{subj['name']}' from {cls['name']} - marks have already "
                          f"been recorded for it in that class. It will stay assigned to protect that data.", "danger")
                else:
                    conn.execute("DELETE FROM class_subjects WHERE class_id=? AND subject_id=?", (class_id, subject_id))
                    conn.commit()
            else:
                # Only allow ADDING a subject to a class if that class does
                # NOT currently have a locked/submitted examination - adding
                # a subject there would show it in an already-locked marks
                # sheet where it can never be filled in, which is confusing
                # and pointless.
                locked_exam = conn.execute("""
                    SELECT ecs.*, e.exam_type, e.academic_year FROM exam_class_status ecs
                    JOIN examinations e ON ecs.exam_id = e.id
                    WHERE ecs.class_id=? AND ecs.status IN ('submitted','under_review','approved')
                    LIMIT 1
                """, (class_id,)).fetchone()
                if locked_exam:
                    subj = conn.execute("SELECT name FROM subjects WHERE id=?", (subject_id,)).fetchone()
                    cls = conn.execute("SELECT name FROM classes WHERE id=?", (class_id,)).fetchone()
                    flash(f"Cannot add '{subj['name']}' to {cls['name']} right now - marks for "
                          f"{locked_exam['exam_type']} ({locked_exam['academic_year']}) are already "
                          f"submitted/locked for this class. Return those results first, or add the "
                          f"subject before marks are submitted next time.", "danger")
                else:
                    conn.execute("INSERT INTO class_subjects (class_id, subject_id) VALUES (?, ?)", (class_id, subject_id))
                    conn.commit()
        return redirect(url_for("subjects"))
    classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    all_subjects = conn.execute("SELECT * FROM subjects ORDER BY name").fetchall()
    mapping = conn.execute("SELECT * FROM class_subjects").fetchall()
    mapping_set = {(m["class_id"], m["subject_id"]) for m in mapping}
    conn.close()
    return render_template("subjects.html", classes=classes, all_subjects=all_subjects, mapping_set=mapping_set)


@app.route("/subjects/delete/<int:subject_id>", methods=["POST"])
@headmaster_required
def delete_subject(subject_id):
    conn = get_db()
    subject = conn.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()
    if not subject:
        conn.close()
        abort(404)
    has_marks = conn.execute("SELECT 1 FROM marks WHERE subject_id=? LIMIT 1", (subject_id,)).fetchone()
    if has_marks:
        flash(f"Cannot delete '{subject['name']}' — marks have already been recorded for it. "
              f"Remove it from all classes instead (toggle off) if it is no longer taught.", "danger")
        conn.close()
        return redirect(url_for("subjects"))
    conn.execute("DELETE FROM class_subjects WHERE subject_id=?", (subject_id,))
    conn.execute("DELETE FROM subjects WHERE id=?", (subject_id,))
    conn.commit()
    log_action(conn, current_user(), "DELETE_SUBJECT", subject["name"])
    conn.close()
    flash(f"Subject '{subject['name']}' deleted successfully.", "info")
    return redirect(url_for("subjects"))


# ---------------------------------------------------------------------------
# EXAMINATIONS
# ---------------------------------------------------------------------------
@app.route("/examinations", methods=["GET", "POST"])
@login_required
def examinations():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    if request.method == "POST" and session["role"] == ROLE_HEADMASTER:
        exam_type = request.form.get("exam_type")
        # Academic Year always comes from Settings (single source of truth) -
        # it is no longer typed manually here, so it can never drift out of
        # sync with the rest of the system.
        academic_year = current_academic_year
        if exam_type in EXAM_TYPES and academic_year:
            try:
                conn.execute(
                    "INSERT INTO examinations (exam_type, academic_year, created_at) VALUES (?, ?, ?)",
                    (exam_type, academic_year, now_str()),
                )
                conn.commit()
                log_action(conn, current_user(), "ADD_EXAM", f"{exam_type} {academic_year}")
                flash("Examination added successfully.", "success")
            except Exception:
                flash("This examination already exists for that academic year.", "warning")
        return redirect(url_for("examinations"))

    # Only show this academic year's examinations here - older years remain
    # safely in the database (with all their marks/results untouched), they
    # are simply hidden from this "current" list once the school moves on
    # to a new academic year in Settings.
    exams = conn.execute(
        "SELECT * FROM examinations WHERE academic_year=? ORDER BY id DESC",
        (current_academic_year,),
    ).fetchall()
    conn.close()
    return render_template("examinations.html", exams=exams, exam_types=EXAM_TYPES,
                            current_academic_year=current_academic_year)


@app.route("/examinations/delete/<int:exam_id>", methods=["POST"])
@headmaster_required
def delete_examination(exam_id):
    conn = get_db()
    exam = conn.execute("SELECT * FROM examinations WHERE id=?", (exam_id,)).fetchone()
    if not exam:
        conn.close()
        abort(404)
    # Only actual scores count as "real result data" worth protecting. A
    # leftover exam_class_status row (e.g. a class was marked "submitted"
    # during testing) with no marks behind it is just workflow trail from
    # testing - safe to clear out together with the exam, once the test
    # marks themselves are already gone (e.g. via Purge Student).
    has_marks = conn.execute("SELECT 1 FROM marks WHERE exam_id=? LIMIT 1", (exam_id,)).fetchone()
    if has_marks:
        flash(f"Cannot delete '{exam['exam_type']} ({exam['academic_year']})' - marks already "
              f"exist for this examination. Deleting it would destroy that data.", "danger")
        conn.close()
        return redirect(url_for("examinations"))
    conn.execute("DELETE FROM exam_class_status WHERE exam_id=?", (exam_id,))
    conn.execute("DELETE FROM examinations WHERE id=?", (exam_id,))
    conn.commit()
    log_action(conn, current_user(), "DELETE_EXAM", f"{exam['exam_type']} ({exam['academic_year']})")
    conn.close()
    flash("Examination deleted successfully.", "info")
    return redirect(url_for("examinations"))



# ---------------------------------------------------------------------------
# USERS / ACCOUNTS  (headmaster only)
# ---------------------------------------------------------------------------
@app.route("/users")
@headmaster_required
def users():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.*, c.name as class_name FROM users u
        LEFT JOIN classes c ON u.class_id = c.id
        ORDER BY u.role DESC, u.full_name
    """).fetchall()
    conn.close()
    return render_template("users.html", users=rows)


@app.route("/users/add", methods=["GET", "POST"])
@headmaster_required
def add_user():
    conn = get_db()
    classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role_choice = request.form.get("role")
        # "Headmaster" and "Administrator" are two DIFFERENT labels for the
        # exact same permission level - both map to ROLE_HEADMASTER. Only
        # `title` differs, so this never touches any @headmaster_required
        # check anywhere else in the app.
        if role_choice == "administrator":
            role, title = ROLE_HEADMASTER, "Administrator"
        elif role_choice == "headmaster":
            role, title = ROLE_HEADMASTER, "Headmaster"
        else:
            role, title = ROLE_CLASS_TEACHER, None
        class_id = request.form.get("class_id") or None
        if role != ROLE_CLASS_TEACHER:
            class_id = None

        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            flash("This username is already taken.", "danger")
            conn.close()
            return render_template("user_form.html", classes=classes, user=request.form, mode="add", role_choice=role_choice)

        conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role, title, class_id, active, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (username, generate_password_hash(password), full_name, role, title, class_id,
             now_str()),
        )
        new_user_id = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        if role == ROLE_CLASS_TEACHER and class_id:
            # Keep classes.teacher_id in sync with this new assignment, the
            # same way the Classes page does it - otherwise the Classes page
            # would still show this class as unassigned.
            conn.execute("UPDATE classes SET teacher_id=? WHERE id=?", (new_user_id, class_id))
        conn.commit()
        log_action(conn, current_user(), "ADD_USER", f"{username} ({title or role})")
        conn.close()
        flash(f"Account for {full_name} created successfully. Share the username and temporary "
              f"password with them — they will be asked to set a new password on first login.", "success")
        return redirect(url_for("users"))
    conn.close()
    return render_template("user_form.html", classes=classes, user=None, mode="add", role_choice=None)


@app.route("/users/edit/<int:user_id>", methods=["GET", "POST"])
@headmaster_required
def edit_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()

    def current_role_choice(u):
        if u["role"] == ROLE_HEADMASTER and u["title"] == "Administrator":
            return "administrator"
        elif u["role"] == ROLE_HEADMASTER:
            return "headmaster"
        return "class_teacher"

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        role_choice = request.form.get("role")
        if role_choice == "administrator":
            role, title = ROLE_HEADMASTER, "Administrator"
        elif role_choice == "headmaster":
            role, title = ROLE_HEADMASTER, "Headmaster"
        else:
            role, title = ROLE_CLASS_TEACHER, None
        class_id = request.form.get("class_id") or None
        if role != ROLE_CLASS_TEACHER:
            class_id = None
        new_password = request.form.get("password", "").strip()
        new_username = request.form.get("username", "").strip()

        if not new_username:
            flash("Username cannot be empty.", "danger")
            conn.close()
            return render_template("user_form.html", classes=classes, user=user, mode="edit", role_choice=role_choice)

        duplicate = conn.execute(
            "SELECT 1 FROM users WHERE username=? AND id != ?", (new_username, user_id)
        ).fetchone()
        if duplicate:
            flash("This username is already taken by another user.", "danger")
            conn.close()
            return render_template("user_form.html", classes=classes, user=user, mode="edit", role_choice=role_choice)

        old_username = user["username"]
        conn.execute("UPDATE users SET username=?, full_name=?, role=?, title=?, class_id=? WHERE id=?",
                     (new_username, full_name, role, title, class_id, user_id))
        # Keep classes.teacher_id in sync: clear this user from whatever
        # class they may have been marked as teaching, then re-set it if
        # they are still (or newly) a class teacher with a class assigned -
        # otherwise the Classes page would keep showing a stale assignment.
        conn.execute("UPDATE classes SET teacher_id=NULL WHERE teacher_id=?", (user_id,))
        if role == ROLE_CLASS_TEACHER and class_id:
            conn.execute("UPDATE classes SET teacher_id=? WHERE id=?", (user_id, class_id))
        if new_password:
            conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                         (generate_password_hash(new_password), user_id))
        conn.commit()
        log_action(conn, current_user(), "EDIT_USER",
                   f"{old_username}" + (f" -> renamed to {new_username}" if new_username != old_username else ""))
        conn.close()
        flash("User details updated successfully.", "success")
        return redirect(url_for("users"))
    conn.close()
    return render_template("user_form.html", classes=classes, user=user, mode="edit", role_choice=current_role_choice(user))


@app.route("/users/toggle/<int:user_id>", methods=["POST"])
@headmaster_required
def toggle_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    if user["role"] == ROLE_HEADMASTER:
        flash("You cannot deactivate the Headmaster account.", "warning")
        conn.close()
        return redirect(url_for("users"))
    new_status = 0 if user["active"] else 1
    conn.execute("UPDATE users SET active=? WHERE id=?", (new_status, user_id))
    conn.commit()
    log_action(conn, current_user(), "TOGGLE_USER", f"{user['username']} -> active={new_status}")
    conn.close()
    flash("Account status updated.", "info")
    return redirect(url_for("users"))


@app.route("/users/delete/<int:user_id>", methods=["POST"])
@headmaster_required
def delete_user(user_id):
    """PERMANENTLY deletes a user account (e.g. a leftover test/unused
    teacher account) - unlike deactivating, this cannot be undone. The
    Headmaster account can never be deleted this way, and you can never
    delete the account you are currently logged in as (avoids ever locking
    yourself out). If the account was still assigned to a class, that class
    is simply left unassigned - it is not deleted."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    if user["role"] == ROLE_HEADMASTER:
        flash("You cannot delete the Headmaster account.", "danger")
        conn.close()
        return redirect(url_for("users"))
    if user_id == session.get("user_id"):
        flash("You cannot delete the account you are currently logged in as.", "danger")
        conn.close()
        return redirect(url_for("users"))

    conn.execute("UPDATE classes SET teacher_id=NULL WHERE teacher_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    log_action(conn, current_user(), "DELETE_USER",
               f"{user['username']} ({user['full_name']}, role={user['role']})")
    conn.close()
    flash(f"Account '{user['username']}' has been permanently deleted.", "info")
    return redirect(url_for("users"))


# ---------------------------------------------------------------------------
# RESULT COMPUTATION HELPER
# ---------------------------------------------------------------------------
def compute_class_results(conn, class_id, exam_id, include_student_id=None, use_snapshot=True,
                           fallback_to_current=True):
    """Returns (subjects_list, results_list) for a class+exam.
    results_list items: {student, scores: {subject_id: score}, total, average, grade, colour, position}

    include_student_id: if given, that student is included even if they are
    no longer active (e.g. left/graduated) - used so a former student's own
    historical report can still be opened and ranked, without pulling every
    other inactive student into normal (current) class views.

    use_snapshot: when True (the default - used for every READ-ONLY view:
    Headmaster Overview, Review Results, Records, a student's own History)
    and marks already exist for this exam+class, the roster is whoever
    ACTUALLY had marks recorded here (marks.class_id) - not the students'
    CURRENT class_id - so an old exam's results correctly "stay" with the
    class that sat it even after those students are later promoted
    elsewhere. Pass False only for the still-open marks-ENTRY page itself
    (see marks() below) - there we want the class's live current roster
    every time, so a student transferred in mid-term still shows up to
    have marks entered for them, even before anything has been saved yet.

    fallback_to_current: when no marks exist for this exam+class at all,
    True (the default) shows the class's current roster (with everything
    blank) - right for the marks-entry page and for a single class+exam
    someone deliberately opened. Pass False for a whole-school scan across
    every class (Headmaster Overview) so a class with genuinely no part in
    a given exam shows a clean 0 rather than listing its current students
    against a mark sheet they were never on.
    """
    subjects = conn.execute("""
        SELECT sub.* FROM subjects sub
        JOIN class_subjects cs ON cs.subject_id = sub.id
        WHERE cs.class_id = ?
        ORDER BY sub.name
    """, (class_id,)).fetchall()
    subject_ids = [s["id"] for s in subjects]

    all_marks = conn.execute(
        "SELECT * FROM marks WHERE exam_id=? AND class_id=?", (exam_id, class_id)
    ).fetchall()

    if use_snapshot and all_marks:
        # Historical roster: exactly who had marks recorded for THIS class
        # in THIS exam, regardless of where they are enrolled today.
        student_ids = set(m["student_id"] for m in all_marks)
        if include_student_id:
            student_ids.add(include_student_id)
        student_ids = sorted(student_ids)
        placeholders = ",".join("?" * len(student_ids))
        students = conn.execute(
            f"SELECT * FROM students WHERE id IN ({placeholders}) "
            "ORDER BY CASE gender WHEN 'Male' THEN 0 WHEN 'Female' THEN 1 ELSE 2 END ASC, full_name COLLATE NOCASE ASC",
            student_ids
        ).fetchall()
    elif not fallback_to_current:
        # Nothing recorded for this class+exam, and the caller explicitly
        # doesn't want the current roster substituted in (whole-school
        # scan) - this class simply had no part in this exam.
        students = []
    else:
        # Live current roster - either nothing recorded yet for this
        # class+exam, or this is the still-open entry page itself.
        students = conn.execute(
            "SELECT * FROM students WHERE class_id=? AND (active=1 OR id=?) "
            "ORDER BY CASE gender WHEN 'Male' THEN 0 WHEN 'Female' THEN 1 ELSE 2 END ASC, full_name COLLATE NOCASE ASC",
            (class_id, include_student_id or -1)
        ).fetchall()
    marks_map = {}
    for m in all_marks:
        marks_map.setdefault(m["student_id"], {})[m["subject_id"]] = m["score"]

    results = []
    for st in students:
        scores = marks_map.get(st["id"], {})
        entered = [scores[sid] for sid in subject_ids if scores.get(sid) is not None]
        total = sum(entered) if entered else 0
        average = (total / len(entered)) if entered else None
        grade, colour = grade_for_score(average) if average is not None else ("-", "#6c757d")
        results.append({
            "student": st,
            "scores": scores,
            "total": total,
            "average": round(average, 1) if average is not None else None,
            "grade": grade,
            "colour": colour,
            "subjects_entered": len(entered),
        })

    # Rank by average (desc); students with no marks go last
    ranked = sorted(
        [r for r in results if r["average"] is not None],
        key=lambda r: r["average"], reverse=True
    )
    pos = 0
    prev_avg = None
    for i, r in enumerate(ranked, start=1):
        if r["average"] != prev_avg:
            pos = i
            prev_avg = r["average"]
        r["position"] = pos
        r["remark"] = remark_for(r["grade"], pos, len(ranked))
    for r in results:
        if r["average"] is None:
            r["position"] = "-"
            r["remark"] = "-"

    # keep display order alphabetical (already queried that way)
    order_map = {r["student"]["id"]: r for r in results}
    ordered_results = [order_map[st["id"]] for st in students]

    return subjects, ordered_results


# ---------------------------------------------------------------------------
# PERFORMANCE ANALYTICS  (class-teacher + headmaster subject/grade insights)
#
# All of this is computed live, straight from the `marks` table, every time
# a page is opened - there is no separate "generate report" step and no
# extra snapshot table. That is deliberate: as soon as a Class Teacher saves
# the last score for their class, the very next page load already reflects
# it (class average, top subjects, grade counts) with nothing left stale or
# waiting on a manual trigger.
#
# Marks are always filtered by marks.class_id (the class the mark was
# ACTUALLY entered under), matching compute_class_results()'s use_snapshot
# behaviour above - so an old exam's subject analytics correctly "stay"
# with the class that sat it, even after those students are later promoted.
# ---------------------------------------------------------------------------
GRADE_LETTERS = ["A", "B", "C", "D", "E"]


def compute_subject_stats(conn, exam_id, class_id=None):
    """Per-subject stats for an exam: average score (=percentage, since every
    subject is marked out of 100), letter grade for that average, and how
    many students landed in each grade band. class_id=None aggregates the
    subject across every class (whole-school view) - subjects are shared
    rows (one 'Mathematics' row used by every class that teaches it), so
    this is a single straightforward GROUP BY, not a per-class merge."""
    if class_id:
        rows = conn.execute("""
            SELECT sub.id AS subject_id, sub.name AS subject_name, m.score
            FROM marks m JOIN subjects sub ON sub.id = m.subject_id
            WHERE m.exam_id=? AND m.class_id=? AND m.score IS NOT NULL
        """, (exam_id, class_id)).fetchall()
    else:
        rows = conn.execute("""
            SELECT sub.id AS subject_id, sub.name AS subject_name, m.score
            FROM marks m JOIN subjects sub ON sub.id = m.subject_id
            WHERE m.exam_id=? AND m.score IS NOT NULL
        """, (exam_id,)).fetchall()

    by_subject = {}
    for row in rows:
        entry = by_subject.setdefault(
            row["subject_id"], {"name": row["subject_name"], "scores": []}
        )
        entry["scores"].append(row["score"])

    stats = []
    for sid, data in by_subject.items():
        scores = data["scores"]
        average = round(sum(scores) / len(scores), 1)
        grade, colour = grade_for_score(average)
        grade_counts = {g: 0 for g in GRADE_LETTERS}
        for sc in scores:
            g, _ = grade_for_score(sc)
            if g in grade_counts:
                grade_counts[g] += 1
        stats.append({
            "subject_id": sid,
            "name": data["name"],
            "average": average,          # also the % performance (out of 100)
            "grade": grade,
            "colour": colour,
            "grade_counts": grade_counts,
            "student_count": len(scores),
        })
    stats.sort(key=lambda s: s["name"])
    return stats


def rank_and_split_subjects(subject_stats):
    """Sort a class/school's subject_stats by average score and split into
    Top 3 (best-performing) and Bottom 3 (weakest, excluding whatever
    already made Top 3 so the two lists never overlap)."""
    ranked = sorted(
        [s for s in subject_stats if s["average"] is not None],
        key=lambda s: s["average"], reverse=True
    )
    top_subjects = ranked[:3]
    top_ids = {s["subject_id"] for s in top_subjects}
    remaining = [s for s in ranked if s["subject_id"] not in top_ids]
    bottom_subjects = sorted(remaining, key=lambda s: s["average"])[:3]
    return top_subjects, bottom_subjects


def compute_class_analytics(conn, class_id, exam_id):
    """One class's full analytics for one exam: class average/grade (from
    each student's own total-marks average, same figure Headmaster Overview
    already shows), the per-subject breakdown, and that class's Top 3 /
    Bottom 3 subjects for this exam."""
    subjects, results = compute_class_results(conn, class_id, exam_id, fallback_to_current=False)
    student_averages = [r["average"] for r in results if r["average"] is not None]
    class_average = round(sum(student_averages) / len(student_averages), 1) if student_averages else None
    class_grade, class_grade_colour = (
        grade_for_score(class_average) if class_average is not None else ("-", "#6c757d")
    )
    subject_stats = compute_subject_stats(conn, exam_id, class_id)
    top_subjects, bottom_subjects = rank_and_split_subjects(subject_stats)
    return {
        "class_average": class_average,
        "class_grade": class_grade,
        "class_grade_colour": class_grade_colour,
        "student_count": len(student_averages),
        "subject_stats": subject_stats,
        "top_subjects": top_subjects,
        "bottom_subjects": bottom_subjects,
    }


def compute_trend(conn, class_id, limit=6):
    """Chronological (oldest -> newest) list of average + grade across the
    exams that actually have marks recorded, capped to the most recent
    `limit` exams - powers the term-over-term trend line. class_id=None
    computes the whole-school average per exam instead of one class's."""
    exams = conn.execute(
        "SELECT * FROM examinations ORDER BY academic_year ASC, id ASC"
    ).fetchall()
    points = []
    for e in exams:
        if class_id:
            has_marks = conn.execute(
                "SELECT 1 FROM marks WHERE exam_id=? AND class_id=? AND score IS NOT NULL LIMIT 1",
                (e["id"], class_id)
            ).fetchone()
            if not has_marks:
                continue
            ca = compute_class_analytics(conn, class_id, e["id"])
            average, grade = ca["class_average"], ca["class_grade"]
        else:
            rows = conn.execute("""
                SELECT student_id, AVG(score) avg_score FROM marks
                WHERE exam_id=? AND score IS NOT NULL GROUP BY student_id
            """, (e["id"],)).fetchall()
            if not rows:
                continue
            average = round(sum(r["avg_score"] for r in rows) / len(rows), 1)
            grade, _ = grade_for_score(average)
        points.append({
            "label": f'{e["exam_type"]} {e["academic_year"]}',
            "average": average,
            "grade": grade,
        })
    return points[-limit:]


def compute_class_ranking(conn, exam_id):
    """Ranks every class that has marks recorded for this exam by class
    average (desc) - this is the "which class is leading" comparison.
    Ties share the same position (same rule as student ranking inside
    compute_class_results). Each entry is a class's full analytics dict
    (class_average, class_grade, top_subjects, subject_stats...) plus
    'cls' and 'position'."""
    classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    entries = []
    for cls in classes:
        ca = compute_class_analytics(conn, cls["id"], exam_id)
        if ca["student_count"]:
            entries.append({"cls": cls, **ca})

    ranked = sorted(entries, key=lambda e: e["class_average"], reverse=True)
    pos = 0
    prev_avg = None
    for i, e in enumerate(ranked, start=1):
        if e["class_average"] != prev_avg:
            pos = i
            prev_avg = e["class_average"]
        e["position"] = pos
    return ranked


def compute_school_analytics(conn, exam_id):
    """Whole-school view for the Headmaster: every class's own analytics
    (points 1-4) side by side and ranked against each other, PLUS a
    school-wide average/grade and a school-wide Top 3 subjects ranking
    combining every class."""
    per_class = compute_class_ranking(conn, exam_id)

    # True school-wide student average (each student's own average, not an
    # average-of-class-averages, so classes of different sizes are weighted
    # fairly).
    student_rows = conn.execute("""
        SELECT student_id, AVG(score) avg_score
        FROM marks WHERE exam_id=? AND score IS NOT NULL
        GROUP BY student_id
    """, (exam_id,)).fetchall()
    school_average = (
        round(sum(r["avg_score"] for r in student_rows) / len(student_rows), 1)
        if student_rows else None
    )
    school_grade, school_grade_colour = (
        grade_for_score(school_average) if school_average is not None else ("-", "#6c757d")
    )
    # Highest/Lowest average are by CLASS average (which class is leading /
    # trailing overall), not by individual student average - per_class is
    # already sorted highest-to-lowest by compute_class_ranking().
    highest_average = per_class[0]["class_average"] if per_class else None
    highest_average_class = per_class[0]["cls"]["name"] if per_class else None
    lowest_average = per_class[-1]["class_average"] if per_class else None
    lowest_average_class = per_class[-1]["cls"]["name"] if per_class else None

    subject_stats = compute_subject_stats(conn, exam_id, class_id=None)
    top_subjects, bottom_subjects = rank_and_split_subjects(subject_stats)

    return {
        "class_average": school_average,
        "class_grade": school_grade,
        "class_grade_colour": school_grade_colour,
        "student_count": len(student_rows),
        "highest_average": highest_average,
        "highest_average_class": highest_average_class,
        "lowest_average": lowest_average,
        "lowest_average_class": lowest_average_class,
        "subject_stats": subject_stats,
        "top_subjects": top_subjects,
        "bottom_subjects": bottom_subjects,
        "per_class": per_class,
    }


def get_exam_status(conn, exam_id, class_id):
    row = conn.execute(
        "SELECT * FROM exam_class_status WHERE exam_id=? AND class_id=?", (exam_id, class_id)
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# MARKS ENTRY  (Class Teacher enters marks for their own class)
# ---------------------------------------------------------------------------
@app.route("/marks", methods=["GET"])
@login_required
def marks_select():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    # Only show examinations for the CURRENT academic year (set in Settings) -
    # older years' examinations still exist safely in the database (with all
    # their marks/results intact), they are simply not offered here so
    # teachers can't accidentally enter marks against a past year's exam.
    exams = conn.execute(
        "SELECT * FROM examinations WHERE academic_year=? ORDER BY id DESC",
        (current_academic_year,),
    ).fetchall()
    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    conn.close()
    return render_template("marks_select.html", exams=exams, classes=classes,
                            current_academic_year=current_academic_year)


@app.route("/marks/<int:exam_id>/<int:class_id>", methods=["GET", "POST"])
@login_required
def marks(exam_id, class_id):
    conn = get_db()
    if session["role"] == ROLE_CLASS_TEACHER and class_id != session.get("class_id"):
        conn.close()
        flash("You can only enter marks for your own assigned class.", "danger")
        return redirect(url_for("marks_select"))

    exam = conn.execute("SELECT * FROM examinations WHERE id=?", (exam_id,)).fetchone()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    status_row = get_exam_status(conn, exam_id, class_id)
    status = status_row["status"] if status_row else "draft"
    locked = status in ("submitted", "under_review", "approved")

    if request.method == "POST":
        if locked:
            flash("Marks for this class have already been submitted/approved and cannot be edited.", "warning")
            conn.close()
            return redirect(url_for("marks", exam_id=exam_id, class_id=class_id))

        subjects = conn.execute("""
            SELECT sub.* FROM subjects sub JOIN class_subjects cs ON cs.subject_id = sub.id
            WHERE cs.class_id=?
        """, (class_id,)).fetchall()
        students = conn.execute(
            "SELECT * FROM students WHERE class_id=? AND active=1", (class_id,)
        ).fetchall()
        invalid_entries = []

        for st in students:
            for sub in subjects:
                field = f"score_{st['id']}_{sub['id']}"
                val = request.form.get(field, "").strip()
                score = None
                if val != "":
                    try:
                        parsed = float(val)
                    except ValueError:
                        parsed = None
                    if parsed is None:
                        invalid_entries.append(f"{st['full_name']} - {sub['name']}: \"{val}\" is not a number")
                    elif parsed < 0 or parsed > 100:
                        invalid_entries.append(f"{st['full_name']} - {sub['name']}: {val} (must be between 0 and 100)")
                    else:
                        # Kept as typed (e.g. 87.5) rather than rounded, so
                        # what the teacher entered is exactly what is saved.
                        score = parsed
                existing = conn.execute(
                    "SELECT 1 FROM marks WHERE student_id=? AND exam_id=? AND subject_id=?",
                    (st["id"], exam_id, sub["id"]),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE marks SET score=?, entered_by=?, updated_at=?, class_id=? "
                        "WHERE student_id=? AND exam_id=? AND subject_id=?",
                        (score, session["user_id"], now_str(), class_id,
                         st["id"], exam_id, sub["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO marks (student_id, exam_id, subject_id, score, entered_by, updated_at, class_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (st["id"], exam_id, sub["id"], score, session["user_id"],
                         now_str(), class_id),
                    )
        conn.execute(
            "INSERT INTO exam_class_status (exam_id, class_id, status) VALUES (?, ?, 'draft') "
            "ON CONFLICT(exam_id, class_id) DO UPDATE SET status='draft'",
            (exam_id, class_id),
        )
        conn.commit()
        log_action(conn, current_user(), "ENTER_MARKS", f"exam={exam_id} class={class_id}")
        if invalid_entries:
            # SECURITY/DATA INTEGRITY: an out-of-range or non-numeric mark is
            # never silently clamped (e.g. "150" silently becoming "100")
            # or silently guessed - that would quietly corrupt real data.
            # Every OTHER, valid mark on the form is still saved normally;
            # only the bad cell(s) are left blank so the teacher can see
            # exactly which ones need fixing.
            preview = "; ".join(invalid_entries[:5])
            if len(invalid_entries) > 5:
                preview += f"; and {len(invalid_entries) - 5} more"
            flash(f"Marks saved, but {len(invalid_entries)} entr{'y' if len(invalid_entries)==1 else 'ies'} "
                  f"could not be used (left blank) - please correct and re-save: {preview}", "warning")
        else:
            flash("Marks saved successfully.", "success")
        conn.close()
        return redirect(url_for("marks", exam_id=exam_id, class_id=class_id))

    subjects, results = compute_class_results(conn, class_id, exam_id, use_snapshot=locked)
    history = get_status_history(conn, exam_id, class_id)
    conn.close()
    return render_template("marks.html", exam=exam, cls=cls, subjects=subjects,
                            results=results, status=status, locked=locked, history=history)


@app.route("/marks/submit/<int:exam_id>/<int:class_id>", methods=["POST"])
@login_required
def submit_marks(exam_id, class_id):
    conn = get_db()
    if session["role"] == ROLE_CLASS_TEACHER and class_id != session.get("class_id"):
        conn.close()
        flash("You do not have permission to do that.", "danger")
        return redirect(url_for("marks_select"))

    # Don't let an incomplete class go to the Headmaster by accident - if
    # any student is missing a mark for any subject taught in this class,
    # block the submission here and say exactly how many are missing,
    # rather than silently forwarding a half-filled result sheet.
    subject_count = conn.execute(
        "SELECT COUNT(*) c FROM class_subjects WHERE class_id=?", (class_id,)
    ).fetchone()["c"]
    student_count = conn.execute(
        "SELECT COUNT(*) c FROM students WHERE class_id=? AND active=1", (class_id,)
    ).fetchone()["c"]
    entered_count = conn.execute(
        "SELECT COUNT(*) c FROM marks WHERE exam_id=? AND class_id=? AND score IS NOT NULL",
        (exam_id, class_id),
    ).fetchone()["c"]
    expected_count = subject_count * student_count
    if expected_count and entered_count < expected_count:
        missing = expected_count - entered_count
        conn.close()
        flash(f"Cannot submit yet - {missing} mark(s) are still missing for this class/exam. "
              f"Please fill in every subject for every student before submitting.", "warning")
        return redirect(url_for("marks", exam_id=exam_id, class_id=class_id))

    conn.execute(
        "INSERT INTO exam_class_status (exam_id, class_id, status, submitted_by, submitted_at) "
        "VALUES (?, ?, 'submitted', ?, ?) "
        "ON CONFLICT(exam_id, class_id) DO UPDATE SET status='submitted', submitted_by=?, submitted_at=?",
        (exam_id, class_id, session["user_id"], now_str(),
         session["user_id"], now_str()),
    )
    conn.commit()
    log_action(conn, current_user(), "SUBMIT_RESULTS", f"exam={exam_id} class={class_id}")
    conn.close()
    flash("Results submitted to the Headmaster for review.", "success")
    return redirect(url_for("marks", exam_id=exam_id, class_id=class_id))


# ---------------------------------------------------------------------------
# RESULTS REVIEW & APPROVAL  (Headmaster)
# ---------------------------------------------------------------------------
@app.route("/results")
@headmaster_required
def results():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    rows = conn.execute("""
        SELECT ecs.*, e.exam_type, e.academic_year, c.name as class_name, c.id as class_id_val
        FROM exam_class_status ecs
        JOIN examinations e ON ecs.exam_id = e.id
        JOIN classes c ON ecs.class_id = c.id
        WHERE ecs.status IN ('submitted','under_review','approved','returned')
          AND e.academic_year = ?
        ORDER BY (ecs.status IN ('submitted','under_review')) DESC, e.academic_year DESC, c.sort_order
    """, (current_academic_year,)).fetchall()
    conn.close()
    return render_template("results.html", rows=rows, current_academic_year=current_academic_year)


@app.route("/results/review/<int:exam_id>/<int:class_id>", methods=["GET", "POST"])
@headmaster_required
def review_results(exam_id, class_id):
    conn = get_db()
    exam = conn.execute("SELECT * FROM examinations WHERE id=?", (exam_id,)).fetchone()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    status_row = get_exam_status(conn, exam_id, class_id)

    # Headmaster opening the review page moves SUBMITTED -> UNDER REVIEW
    if request.method == "GET" and status_row and status_row["status"] == "submitted":
        conn.execute(
            "UPDATE exam_class_status SET status='under_review' WHERE exam_id=? AND class_id=?",
            (exam_id, class_id),
        )
        conn.commit()
        log_action(conn, current_user(), "START_REVIEW", f"exam={exam_id} class={class_id}")
        status_row = get_exam_status(conn, exam_id, class_id)

    if request.method == "POST":
        action = request.form.get("action")
        remarks = request.form.get("remarks", "").strip()
        if action == "approve":
            conn.execute(
                "UPDATE exam_class_status SET status='approved', approved_by=?, approved_at=?, remarks=? "
                "WHERE exam_id=? AND class_id=?",
                (session["user_id"], now_str(), remarks, exam_id, class_id),
            )
            conn.commit()
            log_action(conn, current_user(), "APPROVE_RESULTS", f"exam={exam_id} class={class_id}")
            flash("Results approved and locked successfully.", "success")
        elif action == "return":
            if not remarks:
                flash("Please provide a reason/feedback when returning results for correction.", "warning")
                conn.close()
                return redirect(url_for("review_results", exam_id=exam_id, class_id=class_id))
            conn.execute(
                "UPDATE exam_class_status SET status='returned', remarks=? WHERE exam_id=? AND class_id=?",
                (remarks, exam_id, class_id),
            )
            conn.commit()
            log_action(conn, current_user(), "RETURN_RESULTS", f"exam={exam_id} class={class_id}: {remarks}")
            flash("Results returned to the Class Teacher for correction.", "info")
        conn.close()
        return redirect(url_for("results"))

    subjects, res = compute_class_results(conn, class_id, exam_id)
    history = get_status_history(conn, exam_id, class_id)
    teacher = None
    if cls["teacher_id"]:
        teacher = conn.execute("SELECT * FROM users WHERE id=?", (cls["teacher_id"],)).fetchone()
    student_averages = [r["average"] for r in res if r["average"] is not None]
    class_average = round(sum(student_averages) / len(student_averages), 1) if student_averages else None
    class_grade, _ = grade_for_score(class_average) if class_average is not None else ("-", None)
    # This class's position among ALL classes for this same exam (reuses the
    # same tie-aware ranking already used on the Headmaster/Class Teacher
    # analytics pages), so the Class Result Sheet shows not just this class's
    # own average/grade but where it stands school-wide.
    class_ranking = compute_class_ranking(conn, exam_id)
    class_position = next(
        (e["position"] for e in class_ranking if e["cls"]["id"] == class_id), None
    )
    conn.close()
    return render_template("review_results.html", exam=exam, cls=cls, subjects=subjects,
                            results=res, status_row=status_row, history=history,
                            teacher=teacher, class_average=class_average, class_grade=class_grade,
                            class_position=class_position,
                            report_date=now().strftime("%d/%m/%Y"))


@app.route("/results/review/<int:exam_id>/<int:class_id>/download")
@headmaster_required
def review_results_download(exam_id, class_id):
    """Downloadable PDF version of the Class Result Sheet - same data as
    the on-screen/Print view, laid out landscape via build_class_result_pdf()
    so it works reliably even where a browser's Print-to-PDF is awkward."""
    conn = get_db()
    exam = conn.execute("SELECT * FROM examinations WHERE id=?", (exam_id,)).fetchone()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not exam or not cls:
        conn.close()
        abort(404)

    subjects, res = compute_class_results(conn, class_id, exam_id)
    teacher = None
    if cls["teacher_id"]:
        teacher = conn.execute("SELECT * FROM users WHERE id=?", (cls["teacher_id"],)).fetchone()
    student_averages = [r["average"] for r in res if r["average"] is not None]
    class_average = round(sum(student_averages) / len(student_averages), 1) if student_averages else None
    class_grade, _ = grade_for_score(class_average) if class_average is not None else ("-", None)
    class_ranking = compute_class_ranking(conn, exam_id)
    class_position = next(
        (e["position"] for e in class_ranking if e["cls"]["id"] == class_id), None
    )

    school_name = get_setting(conn, "school_name", "KISAUNI PRIMARY SCHOOL")
    logo_path = get_setting(conn, "logo_path", "images/logo.png")
    conn.close()

    pdf_data = {
        "school_name": school_name,
        "academic_year": exam["academic_year"],
        "exam_type": exam["exam_type"].title(),
        "class_name": cls["name"],
        "teacher_name": teacher["full_name"] if teacher else None,
        "class_average": class_average if class_average is not None else "-",
        "class_grade": class_grade or "-",
        "class_position": class_position if class_position is not None else "-",
        "total_students": len(res),
        "report_date": now().strftime("%d/%m/%Y"),
        "subjects": [s["name"] for s in subjects],
        "rows": [
            {
                "name": r["student"]["full_name"],
                "reg_no": r["student"]["reg_no"],
                "scores": [r["scores"].get(s["id"]) for s in subjects],
                "total": r["total"] if r["subjects_entered"] else None,
                "average": r["average"],
                "grade": r["grade"],
                "remark": r["remark"],
                "position": r["position"],
            }
            for r in res
        ],
    }
    logo_abs_path = resolve_logo_abs_path(logo_path)
    pdf_bytes = build_class_result_pdf(pdf_data, logo_abs_path=logo_abs_path)

    safe_class = "".join(c if c.isalnum() else "_" for c in cls["name"])
    safe_exam = "".join(c if c.isalnum() else "_" for c in exam["exam_type"])
    filename = f"{safe_class}_{safe_exam}_{exam['academic_year']}_ResultSheet.pdf"
    return send_file(
        BytesIO(pdf_bytes), mimetype="application/pdf",
        as_attachment=True, download_name=filename,
    )


# ---------------------------------------------------------------------------
# HEADMASTER OVERVIEW  (summary of all classes' performance)
# ---------------------------------------------------------------------------
@app.route("/headmaster/overview")
@headmaster_required
def headmaster_overview():
    conn = get_db()
    exam_id = request.args.get("exam_id")
    exams = conn.execute("SELECT * FROM examinations ORDER BY academic_year DESC, id DESC").fetchall()
    if not exam_id and exams:
        exam_id = exams[0]["id"]

    overview = []
    if exam_id:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
        for cls in classes:
            subjects, res = compute_class_results(conn, cls["id"], exam_id, fallback_to_current=False)
            averages = [r["average"] for r in res if r["average"] is not None]
            class_avg = round(sum(averages) / len(averages), 1) if averages else None
            status_row = get_exam_status(conn, exam_id, cls["id"])
            overview.append({
                "cls": cls,
                "student_count": len(res),
                "class_average": class_avg,
                "status": status_row["status"] if status_row else "draft",
                "top_student": res[0]["student"]["full_name"] if res and res[0]["average"] is not None else None,
            })
            # find true top student by position 1
            top = [r for r in res if r.get("position") == 1]
            overview[-1]["top_student"] = top[0]["student"]["full_name"] if top else None

    conn.close()
    return render_template("headmaster_overview.html", exams=exams, exam_id=int(exam_id) if exam_id else None,
                            overview=overview)


# ---------------------------------------------------------------------------
# PERFORMANCE ANALYTICS PAGE
#   Class Teacher: always sees their own assigned class - class average +
#     grade, top 3 subjects, and per-subject grade distribution.
#   Headmaster: can pick any single class (same view as above), or "All
#     Classes" for a school-wide rollup of the same four things.
# ---------------------------------------------------------------------------
@app.route("/analytics")
@login_required
def performance_analytics():
    conn = get_db()
    exams = conn.execute("SELECT * FROM examinations ORDER BY academic_year DESC, id DESC").fetchall()
    exam_id = request.args.get("exam_id", type=int)
    if not exam_id and exams:
        exam_id = exams[0]["id"]

    classes = None
    cls = None
    if session["role"] == ROLE_CLASS_TEACHER:
        view_scope = "class"
        class_id = session.get("class_id")
        if not class_id:
            flash("Your account is not assigned to a class yet. Contact the Headmaster.", "warning")
            conn.close()
            return redirect(url_for("dashboard"))
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
        class_param = request.args.get("class_id", "all")
        try:
            class_id = None if class_param in ("all", "", None) else int(class_param)
        except (TypeError, ValueError):
            class_id = None  # malformed input - fall back to the safe "all classes" view
        view_scope = "class" if class_id else "school"

    analytics = None
    trend = []
    if exam_id and class_id:
        cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
        analytics = compute_class_analytics(conn, class_id, exam_id)
        trend = compute_trend(conn, class_id)
    elif exam_id and view_scope == "school":
        analytics = compute_school_analytics(conn, exam_id)
        trend = compute_trend(conn, None)

    conn.close()
    return render_template(
        "analytics.html", exams=exams, exam_id=exam_id, classes=classes,
        class_id=class_id, cls=cls, analytics=analytics, view_scope=view_scope,
        trend=trend,
    )


# ---------------------------------------------------------------------------
# EXAM RECORDS ARCHIVE  (Headmaster: all classes / Class Teacher: own class)
# Lets staff browse every exam type that has ever been held, in any past
# academic year, and search students by Reg No or Name - including students
# who have since left/graduated - so a former student's records can still
# be found if a parent asks for them later.
# ---------------------------------------------------------------------------
@app.route("/records")
@login_required
def records():
    conn = get_db()
    years = [r["academic_year"] for r in conn.execute(
        "SELECT DISTINCT academic_year FROM examinations ORDER BY academic_year DESC"
    ).fetchall()]
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    year = request.args.get("year", "") or (current_academic_year if current_academic_year in years else (years[0] if years else ""))
    exam_type = request.args.get("exam_type", "")
    search = request.args.get("q", "").strip()

    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
        class_filter_id = str(session.get("class_id") or "")
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
        class_filter_id = request.args.get("class_id", "")

    # Exam Type dropdown only offers types that were actually added (by the
    # Headmaster, under Examinations) for the selected year - not the full
    # generic list - so a teacher never sees/picks a type that doesn't exist
    # for that year.
    if year:
        exam_types_for_year = [r["exam_type"] for r in conn.execute(
            "SELECT DISTINCT exam_type FROM examinations WHERE academic_year=? ORDER BY exam_type", (year,)
        ).fetchall()]
    else:
        exam_types_for_year = [r["exam_type"] for r in conn.execute(
            "SELECT DISTINCT exam_type FROM examinations ORDER BY exam_type"
        ).fetchall()]

    exams = []
    if year:
        q = "SELECT * FROM examinations WHERE academic_year=?"
        params = [year]
        if exam_type:
            q += " AND exam_type=?"
            params.append(exam_type)
        q += " ORDER BY id"
        exams = conn.execute(q, params).fetchall()

    rows = []
    for exam in exams:
        sq = """
            SELECT DISTINCT s.id, s.reg_no, s.full_name, s.active, s.leave_reason,
                   COALESCE(hc.name, cur.name) as class_name, COALESCE(hc.id, cur.id) as class_id
            FROM marks m
            JOIN students s ON m.student_id = s.id
            JOIN classes cur ON s.class_id = cur.id
            LEFT JOIN classes hc ON m.class_id = hc.id
            WHERE m.exam_id=? AND m.score IS NOT NULL
        """
        sp = [exam["id"]]
        if class_filter_id:
            sq += " AND s.class_id=?"
            sp.append(class_filter_id)
        if search:
            sq += " AND (s.full_name LIKE ? OR s.reg_no LIKE ?)"
            sp += [f"%{search}%", f"%{search}%"]
        sq += " ORDER BY cur.sort_order, s.full_name COLLATE NOCASE"
        students = conn.execute(sq, sp).fetchall()
        if students:
            rows.append({"exam": exam, "students": students})

    conn.close()
    return render_template(
        "records.html", years=years, year=year, exam_type=exam_type, exam_types=exam_types_for_year,
        classes=classes, class_filter_id=class_filter_id, search=search, rows=rows,
    )


# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------
@app.route("/reports")
@login_required
def reports():
    conn = get_db()
    current_academic_year = get_setting(conn, "academic_year", DEFAULT_ACADEMIC_YEAR)
    # Only show examinations for the CURRENT academic year here - this menu is
    # for day-to-day report generation, not history. Older years' exams still
    # have all their data intact and are always reachable via Exam Records.
    exams = conn.execute(
        "SELECT * FROM examinations WHERE academic_year=? ORDER BY id DESC",
        (current_academic_year,),
    ).fetchall()
    if session["role"] == ROLE_CLASS_TEACHER:
        classes = conn.execute("SELECT * FROM classes WHERE id=?", (session.get("class_id"),)).fetchall()
    else:
        classes = conn.execute("SELECT * FROM classes ORDER BY sort_order").fetchall()
    conn.close()
    return render_template("reports.html", exams=exams, classes=classes,
                            current_academic_year=current_academic_year)



def _load_student_report_context(student_id, exam_id):
    """Shared data-gathering + permission checks for both the HTML view and
    the PDF download, so the two never drift out of sync."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return None, None

    if session["role"] == ROLE_CLASS_TEACHER and student["class_id"] != session.get("class_id"):
        conn.close()
        return None, "forbidden"

    exam = conn.execute("SELECT * FROM examinations WHERE id=?", (exam_id,)).fetchone()
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (student["class_id"],)).fetchone()
    teacher = None
    if cls["teacher_id"]:
        teacher = conn.execute("SELECT * FROM users WHERE id=?", (cls["teacher_id"],)).fetchone()

    subjects, results = compute_class_results(conn, cls["id"], exam_id, include_student_id=student_id)
    my_result = next((r for r in results if r["student"]["id"] == student_id), None)
    total_students = len(results)
    status_row = get_exam_status(conn, exam_id, cls["id"])
    conn.close()
    return {
        "exam": exam, "cls": cls, "student": student, "subjects": subjects,
        "result": my_result, "total_students": total_students, "teacher": teacher,
        "status_row": status_row, "report_date": now().strftime("%d/%m/%Y"),
    }, None


@app.route("/students/<int:student_id>/history")
@login_required
def student_history(student_id):
    """Shows every examination this student has recorded marks for, across
    all academic years - a full academic history in one place."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        abort(404)
    if session["role"] == ROLE_CLASS_TEACHER and student["class_id"] != session.get("class_id"):
        conn.close()
        flash("You can only view history for students in your own assigned class.", "danger")
        return redirect(url_for("students"))

    cls = conn.execute("SELECT * FROM classes WHERE id=?", (student["class_id"],)).fetchone()
    exam_rows = conn.execute(
        "SELECT DISTINCT exam_id FROM marks WHERE student_id=? AND score IS NOT NULL",
        (student_id,),
    ).fetchall()

    history = []
    for row in exam_rows:
        exam = conn.execute("SELECT * FROM examinations WHERE id=?", (row["exam_id"],)).fetchone()
        subjects, results = compute_class_results(conn, student["class_id"], row["exam_id"], include_student_id=student_id)
        my_result = next((r for r in results if r["student"]["id"] == student_id), None)
        if my_result and my_result["subjects_entered"]:
            history.append({"exam": exam, "result": my_result, "total_students": len(results)})

    history.sort(key=lambda h: (h["exam"]["academic_year"], h["exam"]["id"]), reverse=True)
    conn.close()
    return render_template("student_history.html", student=student, cls=cls, history=history)


@app.route("/reports/student/<int:student_id>/<int:exam_id>")
@login_required
def student_report(student_id, exam_id):
    ctx, error = _load_student_report_context(student_id, exam_id)
    if error == "forbidden":
        flash("You do not have permission to do that.", "danger")
        return redirect(url_for("reports"))
    if ctx is None:
        abort(404)
    return render_template("student_report.html", **ctx)


@app.route("/reports/student/<int:student_id>/<int:exam_id>/download")
@login_required
def student_report_download(student_id, exam_id):
    ctx, error = _load_student_report_context(student_id, exam_id)
    if error == "forbidden":
        flash("You do not have permission to do that.", "danger")
        return redirect(url_for("reports"))
    if ctx is None:
        abort(404)

    conn = get_db()
    school_name = get_setting(conn, "school_name", "KISAUNI PRIMARY SCHOOL")
    logo_path = get_setting(conn, "logo_path", "images/logo.png")
    conn.close()

    subj_rows = []
    scores_map = ctx["result"]["scores"] if ctx["result"] else {}
    for sub in ctx["subjects"]:
        score = scores_map.get(sub["id"])
        if score is None:
            grade = "-"
        elif score >= 81:
            grade = "A"
        elif score >= 61:
            grade = "B"
        elif score >= 41:
            grade = "C"
        elif score >= 21:
            grade = "D"
        else:
            grade = "E"
        subj_rows.append({"name": sub["name"], "mark": score, "grade": grade})

    pdf_data = {
        "school_name": school_name,
        "academic_year": ctx["exam"]["academic_year"],
        "reg_no": ctx["student"]["reg_no"],
        "full_name": ctx["student"]["full_name"],
        "class_name": ctx["cls"]["name"],
        "gender": ctx["student"]["gender"],
        "exam_type": ctx["exam"]["exam_type"].title(),
        "subjects": subj_rows,
        "total": ctx["result"]["total"] if ctx["result"] and ctx["result"]["subjects_entered"] else "-",
        "average": ctx["result"]["average"] if ctx["result"] and ctx["result"]["average"] is not None else "-",
        "overall_grade": ctx["result"]["grade"] if ctx["result"] else "-",
        "remark": ctx["result"]["remark"] if ctx["result"] else "-",
        "position": ctx["result"]["position"] if ctx["result"] else "-",
        "total_students": ctx["total_students"],
        "teacher_name": ctx["teacher"]["full_name"] if ctx["teacher"] else None,
        "report_date": ctx["report_date"],
    }
    logo_abs_path = resolve_logo_abs_path(logo_path)
    pdf_bytes = build_student_report_pdf(pdf_data, logo_abs_path=logo_abs_path)

    safe_name = "".join(c if c.isalnum() else "_" for c in ctx["student"]["full_name"])
    safe_exam = "".join(c if c.isalnum() else "_" for c in ctx["exam"]["exam_type"])
    filename = f"{safe_name}_{safe_exam}_Report.pdf"

    return send_file(
        BytesIO(pdf_bytes), mimetype="application/pdf",
        as_attachment=True, download_name=filename,
    )


# ---------------------------------------------------------------------------
# SETTINGS  (headmaster only)
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@headmaster_required
def settings_page():
    conn = get_db()
    if request.method == "POST":
        school_name = request.form.get("school_name", "").strip()
        academic_year = request.form.get("academic_year", "").strip()
        conn.execute("UPDATE settings SET value=? WHERE key='school_name'", (school_name,))
        conn.execute("UPDATE settings SET value=? WHERE key='academic_year'", (academic_year,))

        logo = request.files.get("logo")
        if logo and logo.filename:
            filename = secure_filename(logo.filename)
            os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
            save_path = os.path.join(Config.UPLOAD_DIR, filename)
            logo.save(save_path)
            try:
                from PIL import Image as PILImage
                with PILImage.open(save_path) as im:
                    im.verify()
                conn.execute("UPDATE settings SET value=? WHERE key='logo_path'", (f"uploads/{filename}",))
            except Exception:
                os.remove(save_path)
                flash("That logo file could not be read as an image - please upload a PNG or JPG file.", "danger")
                conn.close()
                return redirect(url_for("settings_page"))

        conn.commit()
        log_action(conn, current_user(), "UPDATE_SETTINGS", "School settings updated")
        flash("School settings saved successfully.", "success")
        conn.close()
        return redirect(url_for("settings_page"))

    current = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings").fetchall()}
    conn.close()

    db_exists = os.path.exists(Config.DATABASE)
    db_size_kb = round(os.path.getsize(Config.DATABASE) / 1024, 1) if db_exists else 0
    db_modified = (
        local_from_timestamp(os.path.getmtime(Config.DATABASE)).strftime("%Y-%m-%d %H:%M:%S")
        if db_exists else None
    )
    backups = list_backups()
    conn2 = get_db()
    offsite_days = days_since_last_offsite_download(conn2)
    conn2.close()
    return render_template(
        "settings.html", current=current, backups=backups,
        db_path=Config.DATABASE, db_size_kb=db_size_kb, db_modified=db_modified,
        last_backup=backups[0] if backups else None, offsite_days=offsite_days,
        backup_retention_days=Config.BACKUP_RETENTION_DAYS,
        backup_min_keep=Config.BACKUP_MIN_KEEP,
    )


# ---------------------------------------------------------------------------
# DATABASE BACKUPS  (headmaster only) - part of Settings
# ---------------------------------------------------------------------------
@app.route("/settings/backup/create", methods=["POST"])
@headmaster_required
def create_backup_route():
    filename = create_backup(reason="manual")
    conn = get_db()
    if filename:
        log_action(conn, current_user(), "CREATE_BACKUP", filename)
        flash(f"Backup created successfully: {filename}", "success")
    else:
        flash("Could not create a backup - the database file was not found.", "danger")
    conn.close()
    return redirect(url_for("settings_page"))


@app.route("/settings/backup/download/<path:filename>")
@headmaster_required
def download_backup(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(Config.BACKUP_DIR, safe_name)
    if not safe_name.startswith("kisauni_backup_") or not os.path.exists(path):
        abort(404)
    conn = get_db()
    log_action(conn, current_user(), "DOWNLOAD_BACKUP", safe_name)
    conn.close()
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.route("/settings/backup/restore/<path:filename>", methods=["POST"])
@headmaster_required
def restore_backup_route(filename):
    ok = restore_backup(filename)
    conn = get_db()
    if ok:
        log_action(conn, current_user(), "RESTORE_BACKUP", filename)
        flash("Database restored from that backup successfully. A safety copy of the data "
              "from just before the restore was also saved, in case you need to undo this.", "success")
    else:
        flash("Could not restore that backup file - it may have been removed.", "danger")
    conn.close()
    return redirect(url_for("settings_page"))


@app.route("/settings/backup/upload-restore", methods=["POST"])
@headmaster_required
def upload_restore_backup_route():
    """Disaster-recovery path: restores from a .db file the Headmaster
    uploads from their own computer/phone, for when this server itself was
    lost/rebuilt and only a locally-downloaded backup still exists."""
    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Please choose a backup (.db) file to upload first.", "danger")
        return redirect(url_for("settings_page"))
    if not file.filename.lower().endswith(".db"):
        flash("That doesn't look like a database backup file (expected a .db file).", "danger")
        return redirect(url_for("settings_page"))

    ok, reason = restore_backup_from_upload(file)
    conn = get_db()
    if ok:
        log_action(conn, current_user(), "RESTORE_BACKUP_UPLOAD", file.filename)
        flash("Database restored successfully from the uploaded backup. A safety copy of the "
              "data from just before the restore was also saved, in case you need to undo this.",
              "success")
    else:
        flash(f"Could not restore from that file: {reason}", "danger")
    conn.close()
    return redirect(url_for("settings_page"))


@app.route("/settings/backup/delete/<path:filename>", methods=["POST"])
@headmaster_required
def delete_backup_route(filename):
    ok = delete_backup(filename)
    conn = get_db()
    if ok:
        log_action(conn, current_user(), "DELETE_BACKUP", os.path.basename(filename))
        flash("Backup file deleted.", "info")
    else:
        flash("That backup file could not be found.", "warning")
    conn.close()
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# AUDIT LOG  (headmaster only)
# ---------------------------------------------------------------------------
@app.route("/audit-logs")
@headmaster_required
def audit_logs():
    conn = get_db()
    logs = humanize_logs(conn, conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall())
    conn.close()
    return render_template("audit_logs.html", logs=logs, retention_days=AUDIT_LOG_RETENTION_DAYS)


if __name__ == "__main__":
    # SECURITY: debug=True used to leave Werkzeug's interactive debugger
    # reachable to anyone on the same WiFi/network as this computer if the
    # app ever hit an unhandled error - that debugger lets whoever sees it
    # run arbitrary code on this machine, not just view the error. debug is
    # now off; host stays "0.0.0.0" on purpose so other computers on the
    # school's own network can still open the system normally.
    app.run(debug=False, host="0.0.0.0", port=5000)
