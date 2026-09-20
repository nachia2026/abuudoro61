"""
KISAUNI PRIMARY SCHOOL - RESULT MANAGEMENT SYSTEM
Configuration & constant data used across the app.
"""

import os
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# TIME: everything in this system (audit log, marks/exam timestamps, backup
# times, report dates) uses East Africa Time (Tanzania, UTC+3, no daylight
# saving) - NOT the server's own system clock. This matters because the
# server this app runs on (especially if hosted online, e.g. Railway) may
# be set to UTC or another timezone; without this, every timestamp shown
# to staff would be wrong by several hours.
# ---------------------------------------------------------------------------
EAT = timezone(timedelta(hours=3))


def now():
    """Current date & time in East Africa Time, regardless of server TZ."""
    return datetime.now(EAT)


def now_str():
    """Current East Africa Time as 'YYYY-MM-DD HH:MM:SS', for storing in the DB."""
    return now().strftime("%Y-%m-%d %H:%M:%S")


def local_from_timestamp(epoch_seconds):
    """Convert a UNIX timestamp (e.g. from os.path.getmtime) to East Africa
    Time, regardless of the server's own system timezone."""
    return datetime.fromtimestamp(epoch_seconds, tz=EAT)


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "kisauni-secret-key-change-me")
    # Auto-logout after this many minutes of no activity (mouse/keyboard/click)
    # - important because this system runs on a shared school-office computer
    # where several staff use the same browser at different times. If someone
    # logs in and walks away without clicking Logout, this makes sure their
    # session doesn't just stay open for the next person to use.
    #
    # 10 minutes balances that safety against real usage: a teacher entering
    # marks for a full class (140 students) needs room to pause and think
    # between rows without being logged out mid-entry, even with the
    # keep-alive ping (script.js) covering genuinely active typing/clicking.
    IDLE_TIMEOUT_MINUTES = 10
    # This is now ALSO enforced server-side (session.permanent = True in the
    # login route, below), not just by the client-side JS timer. Reason:
    # relying only on the browser fully closing to clear the session
    # doesn't hold up in practice - phones (and some desktop browsers)
    # deliberately restore your open tab/session when you reopen the app,
    # even after what looks like "closing" it, so a person could still land
    # back in without logging in again. A real, server-checked expiry means
    # the cookie itself stops being valid once this many minutes have
    # passed since the last request, regardless of whether the browser ever
    # "closed" in a way we can detect - so leaving the app (by any means)
    # for longer than this always requires logging in again next time.
    # SESSION_REFRESH_EACH_REQUEST (below) re-arms that same countdown on
    # every request, so someone actively using the app never gets logged
    # out mid-task - only a real gap in activity triggers it.
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=IDLE_TIMEOUT_MINUTES)
    SESSION_REFRESH_EACH_REQUEST = True
    # Login brute-force protection: after this many WRONG password attempts
    # in a row for the same username, that account is temporarily locked -
    # nobody (not even with the correct password) can log into it again
    # until the lockout period below has passed. A successful login resets
    # the failed-attempt counter back to zero.
    MAX_LOGIN_ATTEMPTS = 5
    LOGIN_LOCKOUT_MINUTES = 5
    # DATA_DIR can be pointed at a persistent volume in production (e.g. on
    # Railway: set DATA_DIR=/data, mounted as a persistent Volume) so the
    # database survives redeploys/restarts. Locally it just defaults to
    # this project folder, so nothing changes for local development.
    DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
    DATABASE = os.path.join(DATA_DIR, "kisauni.db")
    REPORTS_DIR = os.path.join(DATA_DIR, "reports", "generated")
    # Bundled default logo etc. that ships with the app code itself (not
    # school-specific data, so it's fine for this to live with the code).
    STATIC_IMAGES_DIR = os.path.join(BASE_DIR, "static", "images")
    # School-specific uploads (e.g. a custom logo the Headmaster uploads from
    # Settings) are real school data, so - like the database - they live on
    # DATA_DIR, NOT inside the app's own code folder. On hosting where
    # DATA_DIR points at a persistent Volume, an uploaded logo survives
    # redeploys exactly like the database does; a logo saved next to the
    # code would otherwise be wiped the next time the app is redeployed.
    UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
    # Automatic self-backups of the database live here (same persistent
    # volume as DATABASE in production, so they survive redeploys too).
    BACKUP_DIR = os.path.join(DATA_DIR, "backups")
    # Backup retention: a backup file is only eligible for automatic
    # deletion once it is older than BACKUP_RETENTION_DAYS - AND even then,
    # the BACKUP_MIN_KEEP most recent backups are always protected and
    # never deleted, no matter how old they get. This guarantees there are
    # always at least a few restore points on disk, even if something
    # keeps a headmaster from making a fresh backup for a while.
    #
    # Example with the defaults below (keep 3, 3 days): as soon as there
    # are more than 3 backups on disk, any of the older ones past their
    # 3rd day gets pruned automatically - but the 3 newest are always kept
    # regardless of age.
    BACKUP_RETENTION_DAYS = 3
    BACKUP_MIN_KEEP = 3
    # Minimum hours between automatic ("daily"/"startup") backups - prevents
    # spamming the backups folder if the app is restarted often.
    BACKUP_MIN_INTERVAL_HOURS = 20

# ---------------------------------------------------------------------------
# SCHOOL / GRADING SETTINGS (defaults - editable later from Settings page,
# stored in the `settings` table; these are just fallback values)
# ---------------------------------------------------------------------------
DEFAULT_SCHOOL_NAME = "KISAUNI PRIMARY SCHOOL"
DEFAULT_ACADEMIC_YEAR = "2026"

# Grading system (as provided by the school), using inclusive lower
# thresholds so every possible decimal average maps to exactly one grade
# with no gaps:
#   A = 81-100   B = 61-80.9   C = 41-60.9   D = 21-40.9   E = 0-20.9
GRADE_BANDS = [
    ("A", 81, 100, "#198754"),   # green
    ("B", 61, 80,  "#0d6efd"),   # blue
    ("C", 41, 60,  "#ffc107"),   # yellow/amber
    ("D", 21, 40,  "#fd7e14"),   # orange
    ("E", 0,  20,  "#dc3545"),   # red
]

# Ordered highest-to-lowest (letter, minimum-score-inclusive, colour).
# Using ">= minimum" avoids the gap bug that occurs when comparing against
# both a low AND a high bound with whole numbers (e.g. 40.5 previously
# matched neither the D band [21-40] nor the C band [41-60] and fell
# through to an incorrect default).
_GRADE_THRESHOLDS = [(letter, low, colour) for letter, low, _high, colour in GRADE_BANDS]

def grade_for_score(score):
    """Return (grade_letter, colour_hex) for a given numeric score/average.
    Every score from 0 up to any decimal (e.g. 20.1, 40.9, 60.5, 80.99)
    maps to exactly one grade with no gaps."""
    if score is None:
        return ("-", "#6c757d")
    try:
        score = float(score)
    except (TypeError, ValueError):
        return ("-", "#6c757d")
    for letter, low, colour in _GRADE_THRESHOLDS:
        if score >= low:
            return (letter, colour)
    return ("E", "#dc3545")

# ---------------------------------------------------------------------------
# PERFORMANCE REMARK: a plain-language comment (Excellent / Very Good / Good
# / Satisfactory / Needs Improvement / Poor) shown on the Class Result Sheet
# and Student Report next to the letter grade. It combines a student's own
# GRADE (from their average marks) with how they RANK within their own
# class for this exam - so two students on the same grade can still get
# different remarks (the one who is 1st in class gets a nudge better than
# one on the same grade near the bottom of the class), as requested.
# ---------------------------------------------------------------------------
REMARK_LEVELS = ["Poor", "Needs Improvement", "Satisfactory", "Good", "Very Good", "Excellent"]

# Each grade letter's starting point on that same 0-5 scale, before the
# position adjustment below is applied.
_GRADE_REMARK_POINTS = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}


def remark_for(grade, position, total_students):
    """Plain-language remark for one student's result in their class.

    Rule: start from the grade's point on the 0-5 REMARK_LEVELS scale, then
    nudge it by where the student ranks among their classmates this exam:
      - Top third of the class (by position)    -> +1 point
      - Middle third                            -> no change
      - Bottom third                            -> -1 point
    The result is clamped back onto the 0-5 scale, so an A student who is
    also top of the class still tops out at "Excellent" (nothing higher
    exists), and an E student at the bottom still floors out at "Poor"
    (nothing lower exists) - the adjustment only matters in between.

    Returns "-" if there is no grade/position yet (e.g. marks not entered).
    """
    if grade not in _GRADE_REMARK_POINTS or not isinstance(position, int) or not total_students:
        return "-"
    points = _GRADE_REMARK_POINTS[grade]
    fraction_from_top = (position - 1) / max(total_students - 1, 1)
    if fraction_from_top <= 1 / 3:
        points += 1
    elif fraction_from_top > 2 / 3:
        points -= 1
    points = max(0, min(len(REMARK_LEVELS) - 1, points))
    return REMARK_LEVELS[points]

# ---------------------------------------------------------------------------
# CLASSES: Standard One - Standard Seven (KG1/KG2 not included)
#
# Each Standard is split into one or more Streams (e.g. "Standard One A",
# "Standard One B"...). Streams are fully dynamic - the Headmaster can add
# or remove them at any time from the Classes page. ORDINAL_WORDS / the
# defaults below are only used to (a) build a class's display name and
# (b) seed a sensible starting set the very first time the system runs.
# ---------------------------------------------------------------------------
ORDINAL_WORDS = {
    1: "One", 2: "Two", 3: "Three", 4: "Four",
    5: "Five", 6: "Six", 7: "Seven",
}
NUM_STANDARDS = 7

# Starting streams seeded once on first run (Headmaster can add/remove more
# afterwards from the Classes page - this list is never re-applied once the
# system has been initialised, so it never overrides Headmaster changes).
DEFAULT_STREAMS = {
    1: ["A", "B"], 2: ["A", "B"], 3: ["A", "B"], 4: ["A", "B"],
    5: ["A", "B", "C"], 6: ["A", "B", "C"], 7: ["A", "B", "C"],
}

CLASS_NAMES = [f"Standard {ORDINAL_WORDS[n]}" for n in range(1, NUM_STANDARDS + 1)]

# ---------------------------------------------------------------------------
# SUBJECTS per class band
#   Standard 1-3 (lower)  : SUMI, Kiswahili, English, Mazingira, Dini, Mathematics
#   Standard 4-7 (upper)  : Kiswahili, English, Mathematics, Dini, Arabic,
#                            S.JAMII (Social Studies), Science and Technology, SUMI
# ---------------------------------------------------------------------------
LOWER_CLASS_SUBJECTS = ["SUMI", "Kiswahili", "English", "Mazingira", "Dini", "Mathematics"]
UPPER_CLASS_SUBJECTS = [
    "Kiswahili", "English", "Mathematics", "Dini", "Arabic",
    "S.JAMII", "Science and Technology", "SUMI",
]
# Standard NUMBERS (not names) that use the lower-class subject list - this
# is what makes it safe for streams/names to change freely.
LOWER_CLASSES = {1, 2, 3}


def subjects_for_standard(standard):
    """Return the correct subject-name list for a given Standard number."""
    try:
        standard = int(standard)
    except (TypeError, ValueError):
        return UPPER_CLASS_SUBJECTS
    return LOWER_CLASS_SUBJECTS if standard in LOWER_CLASSES else UPPER_CLASS_SUBJECTS

# ---------------------------------------------------------------------------
# EXAMINATION TYPES
# ---------------------------------------------------------------------------
EXAM_TYPES = ["MID TERM", "FIRST TERM", "SECOND MID TERM", "SECOND TERM"]

# ---------------------------------------------------------------------------
# ROLES
# ---------------------------------------------------------------------------
ROLE_HEADMASTER = "headmaster"
ROLE_CLASS_TEACHER = "class_teacher"

# ---------------------------------------------------------------------------
# Simple Tanzanian/Swahili/Islamic first-name lookup used to auto-detect
# gender. This is a best-effort heuristic only - the UI always shows a
# "Confirm Gender" control so staff can correct it.
# ---------------------------------------------------------------------------
MALE_NAMES = {
    "mohamed", "muhammad", "mohammed", "ally", "ali", "hassan", "hussein",
    "husein", "omar", "omary", "juma", "jumanne", "issa", "iddi", "idd",
    "rashid", "rashidi", "salum", "salim", "khamis", "khalifa", "abdallah",
    "abdala", "abdulla", "abdullah", "yusuph", "yusuf", "ibrahim", "ismail",
    "ismaili", "said", "seif", "suleiman", "sulemani", "hamisi", "hamis",
    "athuman", "athumani", "shabani", "shaban", "ramadhani", "ramadhan",
    "bakari", "bakar", "hamad", "hamadi", "kassim", "kasim", "amiri", "amir",
    "musa", "moses", "daudi", "david", "yohana", "john", "johnson", "peter",
    "petro", "paulo", "paul", "joseph", "yosefu", "emmanuel", "emanueli",
    "frank", "francis", "fransisko", "michael", "mikael", "jackson", "james",
    "jacob", "yakobo", "erick", "eric", "godfrey", "godwin", "edward",
    "eduard", "richard", "richard", "anthony", "antony", "baraka", "boniface",
    "bonifasi", "charles", "chalo", "clement", "dennis", "denis", "dickson",
    "elias", "eliya", "evans", "fred", "fredrick", "gabriel", "gasper",
    "george", "goodluck", "hamza", "hussen", "innocent", "isaya", "jaffar",
    "jafari", "kelvin", "kevin", "khalfan", "leonard", "makame", "maulid",
    "mbaraka", "mussa", "nasoro", "nassoro", "nuru" ,"rajabu", "rajab",
    "salehe", "saleh", "selemani", "sharif", "sharrif", "vincent", "wilbert",
    "zubery", "zuberi", "abel", "adam", "amani", "andrew", "andrea", "aziz",
}

FEMALE_NAMES = {
    "fatuma", "fatma", "aisha", "aysha", "amina", "mwanaisha", "mwajuma",
    "zainab", "zainabu", "mariam", "maria", "mary", "halima", "hadija",
    "hadijah", "khadija", "rehema", "rukia", "rukiya", "salma", "saumu",
    "asha", "asya", "bahati", "bibi", "farida", "hawa", "hawa", "husna",
    "jamila", "jamela", "juma", "kulthum", "latifa", "mwanahawa",
    "mwanahamisi", "mwanaidi", "nasra", "neema", "pili", "rahma", "raya",
    "sabra", "salama", "shakira", "sofia", "subira", "tatu", "tunu",
    "upendo", "zaituni", "zulfa", "agnes", "agness", "alice", "anna",
    "anastazia", "beatrice", "beatrice", "catherine", "cecilia", "consolata",
    "dorcas", "dorothy", "edna", "elizabeth", "esther", "eunice", "faraja",
    "flora", "florence", "gladness", "glory", "grace", "happiness", "irene",
    "jane", "janeth", "jesca", "joyce", "judith", "juliana", "lightness",
    "lilian", "lucy", "magdalena", "margareth", "martha", "mary", "monica",
    "naomi", "paulina", "prisca", "rehema", "rose", "ruth", "sarah", "sara",
    "scholastica", "stella", "susan", "teresia", "veronica", "victoria",
    "winfrida", "yustina", "zawadi", "zena", "zuhura",
}

def detect_gender(full_name):
    """Best-effort auto detection of gender from the first name.
    Returns 'Male', 'Female' or None (unknown -> needs manual confirmation).
    """
    if not full_name:
        return None
    first = full_name.strip().split()[0].lower()
    first = first.replace(".", "")
    if first in MALE_NAMES:
        return "Male"
    if first in FEMALE_NAMES:
        return "Female"
    return None
