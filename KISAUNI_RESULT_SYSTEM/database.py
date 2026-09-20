"""
Database layer for Kisauni Result System (SQLite).
"""
import glob
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

from config import (
    Config, CLASS_NAMES, LOWER_CLASS_SUBJECTS, UPPER_CLASS_SUBJECTS,
    LOWER_CLASSES, EXAM_TYPES, ROLE_HEADMASTER, ROLE_CLASS_TEACHER,
    DEFAULT_SCHOOL_NAME, DEFAULT_ACADEMIC_YEAR,
    ORDINAL_WORDS, NUM_STANDARDS, DEFAULT_STREAMS, subjects_for_standard,
    now, now_str, local_from_timestamp, EAT,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('headmaster','class_teacher')),
    class_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    photo_path TEXT,
    title TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(class_id) REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    standard INTEGER,
    stream TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    teacher_id INTEGER,
    FOREIGN KEY(teacher_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS class_subjects (
    class_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL,
    PRIMARY KEY (class_id, subject_id),
    FOREIGN KEY(class_id) REFERENCES classes(id),
    FOREIGN KEY(subject_id) REFERENCES subjects(id)
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_no TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    gender TEXT NOT NULL DEFAULT 'Unknown',
    gender_confirmed INTEGER NOT NULL DEFAULT 0,
    class_id INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    leave_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(class_id) REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS examinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_type TEXT NOT NULL,
    academic_year TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(exam_type, academic_year)
);

CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    exam_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL,
    score REAL,
    entered_by INTEGER,
    updated_at TEXT,
    UNIQUE(student_id, exam_id, subject_id),
    FOREIGN KEY(student_id) REFERENCES students(id),
    FOREIGN KEY(exam_id) REFERENCES examinations(id),
    FOREIGN KEY(subject_id) REFERENCES subjects(id)
);

-- Tracks the workflow status of a (class, exam) combination
CREATE TABLE IF NOT EXISTS exam_class_status (
    exam_id INTEGER NOT NULL,
    class_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','submitted','under_review','returned','approved')),
    remarks TEXT,
    submitted_by INTEGER,
    submitted_at TEXT,
    approved_by INTEGER,
    approved_at TEXT,
    PRIMARY KEY (exam_id, class_id),
    FOREIGN KEY(exam_id) REFERENCES examinations(id),
    FOREIGN KEY(class_id) REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL
);
"""


def get_db():
    conn = sqlite3.connect(Config.DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL (Write-Ahead Logging) mode lets one connection write while others
    # keep reading at the same time, instead of the whole database locking
    # up - this is what actually matters for "several teachers entering
    # marks at once", not the choice of SQLite vs a separate database server.
    conn.execute("PRAGMA journal_mode = WAL")
    # If two writes do land at the exact same instant, wait up to 30s and
    # retry automatically instead of failing with "database is locked".
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# The Activity/Audit Log (who did what, and when) is kept separate from the
# school's real records - it is only a short-term security/traceability
# trail, NOT part of the permanent academic record. Students, teachers,
# marks, exams, classes etc. are NEVER auto-deleted by this or anything
# else in the system - only this specific log purges old rows automatically.
AUDIT_LOG_RETENTION_DAYS = 3


def purge_old_audit_logs(conn):
    """Deletes Activity/Audit Log entries older than AUDIT_LOG_RETENTION_DAYS.
    This ONLY affects the audit_log table - it never touches students,
    marks, exams, users, or any other school data."""
    cutoff = (now() - timedelta(days=AUDIT_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
    conn.commit()


def log_action(conn, user, action, details=""):
    conn.execute(
        "INSERT INTO audit_log (user_id, username, action, details, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            user["id"] if user else None,
            user["username"] if user else "system",
            action,
            details,
            now_str(),
        ),
    )
    conn.commit()
    purge_old_audit_logs(conn)


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()

    # --- lightweight migration for DBs created before must_change_password ---
    try:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # --- lightweight migration for DBs created before class streams (A/B/C) ---
    for ddl in (
        "ALTER TABLE classes ADD COLUMN standard INTEGER",
        "ALTER TABLE classes ADD COLUMN stream TEXT",
        "ALTER TABLE students ADD COLUMN leave_reason TEXT",
        "ALTER TABLE classes ADD COLUMN last_promoted_year TEXT",
        "ALTER TABLE marks ADD COLUMN class_id INTEGER",
        "ALTER TABLE users ADD COLUMN photo_path TEXT",
        "ALTER TABLE users ADD COLUMN title TEXT",
        # Login brute-force lockout: counts consecutive wrong-password
        # attempts per account, and the timestamp (if any) until which that
        # account is locked out of logging in.
        "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN locked_until TEXT",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # Marks entered before this update never recorded which class the
    # student was in AT THE TIME (only their current, ever-changing class
    # was known via a join). Backfill those old rows with the student's
    # class as it stood at migration time - this is the best information
    # available for history recorded before this fix; every mark saved
    # FROM NOW ON stamps the real class it was entered under, so Exam
    # Records will correctly keep showing "Standard One B" for an old exam
    # even after that student is later promoted to "Standard Two B".
    conn.execute("""
        UPDATE marks SET class_id = (SELECT class_id FROM students WHERE students.id = marks.student_id)
        WHERE class_id IS NULL
    """)
    conn.commit()

    # A class with last_promoted_year still equal to the CURRENT academic
    # year is not allowed to promote again (Class Teachers and the
    # Headmaster's bulk tool are both blocked until the Headmaster changes
    # the academic year in Settings). Freshly-migrated classes have never
    # been stamped, so seed them to the current year - this means promotion
    # stays locked for everyone until the year is next changed, which is the
    # safe default (no accidental double-promotion right after this update).
    current_year_row = conn.execute("SELECT value FROM settings WHERE key='academic_year'").fetchone()
    if current_year_row:
        conn.execute(
            "UPDATE classes SET last_promoted_year=? WHERE last_promoted_year IS NULL",
            (current_year_row["value"],),
        )
        conn.commit()

    # --- normalise the SUMI subject name (older DBs may have the long form) ---
    conn.execute("UPDATE subjects SET name='SUMI' WHERE name LIKE 'SUMI (%'")
    conn.commit()

    # --- rename subjects to their current English names (older DBs may still
    #     have the earlier Swahili/short names) ---
    rename_map = {
        "Hisabati": "Mathematics",
        "Kiarabu": "Arabic",
        "Jamii": "S.JAMII",
        "Sayansi na Teknolojia": "Science and Technology",
    }
    for old_name, new_name in rename_map.items():
        conn.execute("UPDATE subjects SET name=? WHERE name=?", (new_name, old_name))
    conn.commit()

    # --- Activity/Audit Log entries older than AUDIT_LOG_RETENTION_DAYS are
    #     cleaned up on every startup too (in case the app was off for a
    #     while). This ONLY affects that log - students, marks, exams and
    #     every other school record are kept forever, never auto-deleted. ---
    purge_old_audit_logs(conn)

    # --- seed classes -----------------------------------------------------
    cur = conn.execute("SELECT COUNT(*) c FROM classes")
    if cur.fetchone()["c"] == 0:
        for i, name in enumerate(CLASS_NAMES, start=1):
            conn.execute(
                "INSERT INTO classes (name, sort_order) VALUES (?, ?)", (name, i)
            )
        conn.commit()

    # --- seed subjects + class_subjects mapping ---------------------------
    cur = conn.execute("SELECT COUNT(*) c FROM subjects")
    if cur.fetchone()["c"] == 0:
        all_subject_names = list(dict.fromkeys(LOWER_CLASS_SUBJECTS + UPPER_CLASS_SUBJECTS))
        name_to_id = {}
        for sname in all_subject_names:
            cur2 = conn.execute("INSERT INTO subjects (name) VALUES (?)", (sname,))
            name_to_id[sname] = cur2.lastrowid
        conn.commit()

        name_to_standard_num = {name: i for i, name in enumerate(CLASS_NAMES, start=1)}
        classes = conn.execute("SELECT id, name FROM classes").fetchall()
        for cls in classes:
            standard_num = name_to_standard_num.get(cls["name"])
            subj_list = subjects_for_standard(standard_num) if standard_num else UPPER_CLASS_SUBJECTS
            for sname in subj_list:
                conn.execute(
                    "INSERT OR IGNORE INTO class_subjects (class_id, subject_id) VALUES (?, ?)",
                    (cls["id"], name_to_id[sname]),
                )
        conn.commit()

    # --- ONE-TIME migration: split plain "Standard X" classes into Streams
    #     ("Standard X A", "Standard X B"...) and backfill standard/stream on
    #     every class row. Guarded by a settings flag so it runs exactly once
    #     and never re-adds a stream the Headmaster has since removed. -------
    already_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key='classes_streams_migrated'"
    ).fetchone()
    if not already_migrated:
        name_to_num = {word: num for num, word in ORDINAL_WORDS.items()}

        # 1) Backfill standard/stream for existing rows and rename bare
        #    "Standard X" rows (no stream letter yet) to "Standard X A",
        #    keeping the same id so every existing foreign key stays valid.
        rows = conn.execute("SELECT * FROM classes WHERE standard IS NULL").fetchall()
        for row in rows:
            parts = row["name"].split()
            num = name_to_num.get(parts[1]) if len(parts) > 1 else None
            if not num:
                continue
            stream = parts[2].upper() if len(parts) > 2 and parts[2] else "A"
            new_name = f"Standard {parts[1]} {stream}"
            conn.execute(
                "UPDATE classes SET standard=?, stream=?, name=? WHERE id=?",
                (num, stream, new_name, row["id"]),
            )
        conn.commit()

        # 2) Add the remaining default streams (e.g. "B", "C") that don't
        #    exist yet, each wired up with the right subjects for its
        #    Standard so marks entry works immediately.
        max_sort = conn.execute("SELECT COALESCE(MAX(sort_order),0) m FROM classes").fetchone()["m"]
        for num in range(1, NUM_STANDARDS + 1):
            word = ORDINAL_WORDS[num]
            for stream in DEFAULT_STREAMS.get(num, ["A"]):
                name = f"Standard {word} {stream}"
                exists = conn.execute("SELECT id FROM classes WHERE name=?", (name,)).fetchone()
                if exists:
                    continue
                max_sort += 1
                cur2 = conn.execute(
                    "INSERT INTO classes (name, standard, stream, sort_order) VALUES (?, ?, ?, ?)",
                    (name, num, stream, max_sort),
                )
                class_id = cur2.lastrowid
                for sname in subjects_for_standard(num):
                    srow = conn.execute("SELECT id FROM subjects WHERE name=?", (sname,)).fetchone()
                    if srow:
                        conn.execute(
                            "INSERT OR IGNORE INTO class_subjects (class_id, subject_id) VALUES (?, ?)",
                            (class_id, srow["id"]),
                        )
        conn.commit()
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('classes_streams_migrated', '1')"
        )
        conn.commit()

    # --- seed examination types (current academic year) -------------------
    cur = conn.execute("SELECT COUNT(*) c FROM examinations")
    if cur.fetchone()["c"] == 0:
        for etype in EXAM_TYPES:
            conn.execute(
                "INSERT OR IGNORE INTO examinations (exam_type, academic_year, created_at) "
                "VALUES (?, ?, ?)",
                (etype, DEFAULT_ACADEMIC_YEAR, now_str()),
            )
        conn.commit()

    # --- seed default headmaster account -----------------------------------
    cur = conn.execute("SELECT COUNT(*) c FROM users WHERE role='headmaster'")
    if cur.fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role, active, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, 1, 1, ?)",
            (
                "headmaster",
                generate_password_hash("admin123"),
                "Head Master",
                ROLE_HEADMASTER,
                now_str(),
            ),
        )
        conn.commit()

    # --- seed default settings ---------------------------------------------
    defaults = {
        "school_name": DEFAULT_SCHOOL_NAME,
        "academic_year": DEFAULT_ACADEMIC_YEAR,
        "logo_path": "images/logo.png",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# AUTOMATIC SELF-BACKUP
#
# This is a school's permanent record system, so it must not depend on any
# one person remembering to back it up. SQLite stores the whole database in
# a single file and every request opens/closes its own short-lived
# connection (see get_db above) - so a plain timestamped file copy is a
# safe, reliable point-in-time backup with no extra moving parts.
#
# Backups are taken automatically: once when the app starts, once a day
# while it keeps running, and right before any bulk/irreversible action
# (e.g. year-end student promotion). The Headmaster can also make one
# manually and download/restore/delete backups from Settings.
# ---------------------------------------------------------------------------
BACKUP_FILENAME_PREFIX = "kisauni_backup_"


def _backup_glob():
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    return glob.glob(os.path.join(Config.BACKUP_DIR, f"{BACKUP_FILENAME_PREFIX}*.db"))


def create_backup(reason="manual"):
    """Copies the live database to the backups folder with a timestamped
    name and prunes old backups beyond the configured keep-count. Returns
    the new backup's filename, or None if there is no database yet, or if
    the copy failed an integrity check (very rare - e.g. a disk error
    mid-copy) and had to be discarded rather than kept as a false safety
    net."""
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    if not os.path.exists(Config.DATABASE):
        return None
    # In WAL mode, the most recent commits can sit in a separate -wal file
    # rather than the main .db file - checkpoint first so a plain file copy
    # of the main .db always captures everything up to this exact moment.
    try:
        ckpt_conn = sqlite3.connect(Config.DATABASE, timeout=30)
        ckpt_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ckpt_conn.close()
    except sqlite3.Error:
        pass
    ts = now().strftime("%Y%m%d_%H%M%S")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:40] or "manual"
    filename = f"{BACKUP_FILENAME_PREFIX}{ts}_{safe_reason}.db"
    dest = os.path.join(Config.BACKUP_DIR, filename)
    shutil.copy2(Config.DATABASE, dest)

    # Verify the copy is actually a healthy, readable database before we
    # trust it as a safety net - a backup nobody can restore from isn't one.
    if not _is_healthy_sqlite_file(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
        return None

    prune_old_backups()
    return filename


def _is_healthy_sqlite_file(path):
    try:
        conn = sqlite3.connect(path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return bool(result) and result[0] == "ok"
    except sqlite3.Error:
        return False


def prune_old_backups(keep=None, retention_days=None):
    """Deletes backup files that are older than `retention_days`, but always
    protects the `keep` most recent backups regardless of their age - so
    there is never a moment with fewer than that many safety copies on
    disk, even if nobody has made a fresh backup in a while."""
    keep = keep if keep is not None else Config.BACKUP_MIN_KEEP
    retention_days = (
        retention_days if retention_days is not None else Config.BACKUP_RETENTION_DAYS
    )
    files = _backup_glob()
    files.sort(key=os.path.getmtime, reverse=True)  # newest first

    protected = files[:keep]          # the most recent ones - never touched
    candidates = files[keep:]         # only these are eligible for deletion

    cutoff = now().timestamp() - (retention_days * 86400)
    for f in candidates:
        if os.path.getmtime(f) < cutoff:
            try:
                os.remove(f)
            except OSError:
                pass


def list_backups():
    files = _backup_glob()
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for f in files:
        st = os.stat(f)
        out.append({
            "filename": os.path.basename(f),
            "size_kb": round(st.st_size / 1024, 1),
            "created_at": local_from_timestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


def should_run_periodic_backup():
    """True if no automatic backup has been made within BACKUP_MIN_INTERVAL_HOURS."""
    files = _backup_glob()
    if not files:
        return True
    newest_mtime = max(os.path.getmtime(f) for f in files)
    age_hours = (now().timestamp() - newest_mtime) / 3600
    return age_hours >= Config.BACKUP_MIN_INTERVAL_HOURS


def days_since_last_offsite_download(conn):
    """Days since a backup file was last downloaded from Settings (the one
    action that actually gets a copy off this server). Returns None if a
    backup has never been downloaded."""
    row = conn.execute(
        "SELECT created_at FROM audit_log WHERE action='DOWNLOAD_BACKUP' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        last = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
    except (ValueError, TypeError):
        return None
    return (now() - last).days


def restore_backup(filename):
    """Restores the live database from a chosen backup file. A safety copy
    of the CURRENT (pre-restore) database is taken first, so a restore can
    itself always be undone. Returns True on success."""
    safe_name = os.path.basename(filename)
    if not safe_name.startswith(BACKUP_FILENAME_PREFIX):
        return False
    src = os.path.join(Config.BACKUP_DIR, safe_name)
    if not os.path.exists(src):
        return False
    create_backup(reason="before_restore")
    shutil.copy2(src, Config.DATABASE)
    return True


def restore_backup_from_upload(file_storage):
    """Restores the live database from a .db file the Headmaster UPLOADS
    from their own computer/phone - this is the path used for real disaster
    recovery: the server was lost/rebuilt and only a locally-downloaded
    backup still exists. Returns (True, None) on success, or (False, reason)
    on failure - the uploaded file is validated as a healthy SQLite database
    before it's trusted, so a corrupted or unrelated file can never wipe out
    the live data.
    """
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    tmp_path = os.path.join(Config.BACKUP_DIR, "_uploaded_tmp.db")
    file_storage.save(tmp_path)

    if not _is_healthy_sqlite_file(tmp_path):
        os.remove(tmp_path)
        return False, "That file is not a valid/readable backup database."

    try:
        chk = sqlite3.connect(tmp_path)
        has_students_table = chk.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='students'"
        ).fetchone()
        chk.close()
    except sqlite3.Error:
        has_students_table = None
    if not has_students_table:
        os.remove(tmp_path)
        return False, "That file doesn't look like a Kisauni Result System backup (missing expected tables)."

    # Safety copy of what's currently live, THEN also file the uploaded
    # backup itself into the normal backups folder (timestamped) so it
    # joins the regular rotation instead of just being a throwaway temp file.
    if os.path.exists(Config.DATABASE):
        create_backup(reason="before_restore_upload")
    ts = now().strftime("%Y%m%d_%H%M%S")
    kept_name = f"{BACKUP_FILENAME_PREFIX}{ts}_uploaded.db"
    kept_path = os.path.join(Config.BACKUP_DIR, kept_name)
    shutil.move(tmp_path, kept_path)
    shutil.copy2(kept_path, Config.DATABASE)
    prune_old_backups()
    return True, None


def delete_backup(filename):
    safe_name = os.path.basename(filename)
    if not safe_name.startswith(BACKUP_FILENAME_PREFIX):
        return False
    path = os.path.join(Config.BACKUP_DIR, safe_name)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


if __name__ == "__main__":
    init_db()
    print("Database initialised ->", Config.DATABASE)
