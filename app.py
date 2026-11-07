"""ML Lab Exam - Question Paper Generator (Flask + SQLite).

Run:   python app.py
Admin: http://<host>:5000/admin   (password from ADMIN_PASSWORD env var)
"""
import csv
import io
import os
import random
import secrets
import sqlite3
import time
import uuid
import zipfile
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
# Use DATA_DIR env var for persistent disk (e.g. Render disk mount at /data)
DATA_DIR = os.environ.get("DATA_DIR", BASE)
DB_PATH = os.path.join(DATA_DIR, "exam.db")
UPLOADS = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
SUBMISSIONS = os.path.join(UPLOADS, "submissions")
os.makedirs(SUBMISSIONS, exist_ok=True)

# student output snapshots: extension -> required file signature
SNAP_TYPES = {"png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff",
              "gif": b"GIF8", "webp": b"RIFF", "pdf": b"%PDF"}
SNAP_IMAGES = {"png", "jpg", "jpeg", "gif", "webp"}
SNAP_MAX_BYTES = 10 * 1024 * 1024   # per file
SNAP_MAX_PER_SLOT = 15              # files per student per slot

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB per request


def _load_secret():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(DATA_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _load_secret()
rng = random.SystemRandom()

# ----------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT UNIQUE NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (reg_no TEXT PRIMARY KEY, name TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS fixed (
    reg_no TEXT NOT NULL,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    PRIMARY KEY (reg_no, slot_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    reg_no TEXT NOT NULL,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    PRIMARY KEY (reg_no, slot_id)
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    orig_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_no TEXT NOT NULL,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    orig_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    if not db.execute("SELECT 1 FROM settings WHERE key='pin'").fetchone():
        pin = str(rng.randint(100000, 999999))
        db.executemany("INSERT INTO settings VALUES(?,?)", [
            ("pin", pin), ("title", "ML Internal Lab Exam"), ("open", "1"),
            ("instructions", "Download the dataset(s) from this page if your question needs one.")])
        for i, label in enumerate(["1A", "1B", "2A", "2B"]):
            db.execute("INSERT INTO slots(label, position) VALUES(?,?)", (label, i))
        print(f"\n*** First run: exam PIN is {pin} (change it in the admin page) ***")
    db.commit()
    db.close()


with app.app_context():
    init_db()
    if ADMIN_PASSWORD == "admin123":
        print("*** WARNING: using default admin password. Set ADMIN_PASSWORD env var. ***")

# ---------------------------------------------------------------- security


@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "Bad or missing form token. Reload the page and try again.")


@app.context_processor
def inject_helpers():
    return {"csrf_field": lambda: Markup(
        f'<input type="hidden" name="csrf" value="{session.get("csrf", "")}">')}


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


_fails = {}  # ip -> [timestamps]


def too_many_failures(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 300]
    return len(_fails[ip]) >= 15


def record_failure(ip):
    _fails.setdefault(ip, []).append(time.time())


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


def norm_reg(s):
    return "".join((s or "").split()).upper()


# --------------------------------------------------------------- core logic


def get_slots(db):
    return db.execute("SELECT * FROM slots ORDER BY position, id").fetchall()


def question_labels(db):
    """qid -> 'slotLabel-N' (N = serial number within the slot)."""
    labels = {}
    for s in get_slots(db):
        qs = db.execute("SELECT id FROM questions WHERE slot_id=? ORDER BY id", (s["id"],)).fetchall()
        for n, q in enumerate(qs, 1):
            labels[q["id"]] = f"{s['label']}-{n}"
    return labels


def ensure_assignments(db, reg):
    """Return [(slot, question_row_or_None)] for this student, creating and
    locking any missing assignment (fixed question first, else random).
    Respects assignment_mode: 'one_set' (default) gives 1 set only, 'all_slots' gives all."""
    mode = get_setting("assignment_mode", "one_set")
    slots = get_slots(db)
    if not slots:
        return []

    if mode == "one_set":
        slot_map = {s["id"]: s for s in slots}

        # Check existing assignments for this student
        existing = db.execute(
            "SELECT a.slot_id, q.* FROM assignments a "
            "JOIN questions q ON q.id=a.question_id "
            "WHERE a.reg_no=? ORDER BY a.slot_id", (reg,)
        ).fetchall()

        if existing:
            # If already assigned, return only their assigned set
            first = existing[0]
            if first["slot_id"] in slot_map:
                return [(slot_map[first["slot_id"]], dict(first))]
            return []

        # Check fixed questions for this student
        fx = db.execute("SELECT slot_id, question_id FROM fixed WHERE reg_no=?", (reg,)).fetchone()
        if fx and fx["slot_id"] in slot_map:
            sid = fx["slot_id"]
            qid = fx["question_id"]
        else:
            # Balanced distribution: pick slot with fewest assignments
            counts = {s["id"]: 0 for s in slots}
            for r in db.execute("SELECT slot_id, COUNT(*) as c FROM assignments GROUP BY slot_id").fetchall():
                if r["slot_id"] in counts:
                    counts[r["slot_id"]] = r["c"]

            # Prioritize slots that actually have questions added
            with_q = [s for s in slots if db.execute("SELECT 1 FROM questions WHERE slot_id=?", (s["id"],)).fetchone()]
            candidates = with_q if with_q else slots

            min_c = min(counts[s["id"]] for s in candidates)
            best_slots = [s for s in candidates if counts[s["id"]] == min_c]
            chosen_slot = rng.choice(best_slots)
            sid = chosen_slot["id"]

            pool = [r["id"] for r in db.execute("SELECT id FROM questions WHERE slot_id=?", (sid,))]
            qid = rng.choice(pool) if pool else None

        if qid is not None:
            db.execute("INSERT OR IGNORE INTO assignments(reg_no,slot_id,question_id) VALUES(?,?,?)",
                       (reg, sid, qid))
            db.commit()

        chosen_s = slot_map.get(sid)
        q = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone() if qid else None
        return [(chosen_s, q)] if chosen_s else []

    # 'all_slots' mode: student gets one question from every slot
    out = []
    for s in slots:
        q = db.execute(
            "SELECT q.* FROM assignments a JOIN questions q ON q.id=a.question_id "
            "WHERE a.reg_no=? AND a.slot_id=?", (reg, s["id"])).fetchone()
        if q is None:
            fx = db.execute("SELECT question_id FROM fixed WHERE reg_no=? AND slot_id=?",
                            (reg, s["id"])).fetchone()
            if fx:
                qid = fx["question_id"]
            else:
                pool = [r["id"] for r in db.execute(
                    "SELECT id FROM questions WHERE slot_id=?", (s["id"],))]
                qid = rng.choice(pool) if pool else None
            if qid is not None:
                db.execute("INSERT OR IGNORE INTO assignments(reg_no,slot_id,question_id) VALUES(?,?,?)",
                           (reg, s["id"], qid))
                db.commit()
                q = db.execute(
                    "SELECT q.* FROM assignments a JOIN questions q ON q.id=a.question_id "
                    "WHERE a.reg_no=? AND a.slot_id=?", (reg, s["id"])).fetchone()
        out.append((s, q))
    return out


# ------------------------------------------------------------ student pages


@app.route("/")
def home():
    db = get_db()
    datasets = db.execute("SELECT * FROM datasets ORDER BY id DESC").fetchall()
    return render_template("home.html", title=get_setting("title"),
                           instructions=get_setting("instructions"),
                           is_open=get_setting("open") == "1", datasets=datasets,
                           logged_in=bool(session.get("reg_no")))


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or "?"
    if too_many_failures(ip):
        flash("Too many wrong attempts. Wait a few minutes and try again.", "error")
        return redirect(url_for("home"))
    db = get_db()
    if get_setting("open") != "1":
        flash("The exam is not open yet.", "error")
        return redirect(url_for("home"))
    reg = norm_reg(request.form.get("reg_no"))
    pin = request.form.get("pin", "").strip()
    if not reg or not secrets.compare_digest(pin.encode(), get_setting("pin").encode()):
        record_failure(ip)
        flash("Invalid registration number or PIN.", "error")
        return redirect(url_for("home"))
    if db.execute("SELECT COUNT(*) FROM roster").fetchone()[0] > 0:
        if not db.execute("SELECT 1 FROM roster WHERE reg_no=?", (reg,)).fetchone():
            record_failure(ip)
            flash("This registration number is not in the exam list. Contact your invigilator.", "error")
            return redirect(url_for("home"))
    session["reg_no"] = reg
    return redirect(url_for("paper"))


@app.route("/paper")
def paper():
    reg = session.get("reg_no")
    if not reg:
        return redirect(url_for("home"))
    db = get_db()
    if get_setting("open") != "1":
        session.pop("reg_no", None)
        flash("The exam is closed.", "error")
        return redirect(url_for("home"))
    items = ensure_assignments(db, reg)
    name = db.execute("SELECT name FROM roster WHERE reg_no=?", (reg,)).fetchone()
    datasets = db.execute("SELECT * FROM datasets ORDER BY id DESC").fetchall()
    subs = {}
    for r in db.execute("SELECT *, datetime(uploaded_at,'+330 minutes') AS ts FROM submissions "
                        "WHERE reg_no=? ORDER BY id", (reg,)):
        subs.setdefault(r["slot_id"], []).append(r)
    return render_template("paper.html", title=get_setting("title"), reg=reg,
                           name=name["name"] if name else "", items=items, datasets=datasets,
                           subs=subs, submit_open=get_setting("submit_open", "1") == "1",
                           is_image=lambda n: n.rsplit(".", 1)[-1].lower() in SNAP_IMAGES)


def serve_submission(row):
    ext = row["orig_name"].rsplit(".", 1)[-1].lower()
    resp = send_from_directory(SUBMISSIONS, row["stored_name"],
                               as_attachment=ext not in SNAP_IMAGES,
                               download_name=row["orig_name"])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return resp


@app.route("/submit", methods=["POST"])
def submit():
    reg = session.get("reg_no")
    if not reg:
        return redirect(url_for("home"))
    db = get_db()
    if get_setting("open") != "1" or get_setting("submit_open", "1") != "1":
        flash("Uploads are closed.", "error")
        return redirect(url_for("paper"))
    sid = request.form.get("slot_id", type=int)
    if not db.execute("SELECT 1 FROM slots WHERE id=?", (sid,)).fetchone():
        flash("Unknown question part.", "error")
        return redirect(url_for("paper"))
    count = db.execute("SELECT COUNT(*) FROM submissions WHERE reg_no=? AND slot_id=?",
                       (reg, sid)).fetchone()[0]
    saved, errors = 0, []
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        orig = secure_filename(f.filename) or "snapshot"
        ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""
        head = f.stream.read(12)
        f.stream.seek(0)
        ok = ext in SNAP_TYPES and head.startswith(SNAP_TYPES[ext])
        if ext == "webp":
            ok = ok and head[8:12] == b"WEBP"
        if not ok:
            errors.append(f"{orig}: only PNG, JPG, GIF, WEBP or PDF files are allowed")
            continue
        if count >= SNAP_MAX_PER_SLOT:
            errors.append(f"Limit of {SNAP_MAX_PER_SLOT} files per question part reached")
            break
        stored = f"{uuid.uuid4().hex}_{orig}"
        path = os.path.join(SUBMISSIONS, stored)
        f.save(path)
        size = os.path.getsize(path)
        if size > SNAP_MAX_BYTES:
            os.remove(path)
            errors.append(f"{orig}: larger than {SNAP_MAX_BYTES // (1024 * 1024)} MB")
            continue
        db.execute("INSERT INTO submissions(reg_no,slot_id,orig_name,stored_name,size) VALUES(?,?,?,?,?)",
                   (reg, sid, orig, stored, size))
        count += 1
        saved += 1
    db.commit()
    if saved:
        flash(f"Uploaded {saved} file(s).", "ok")
    for e in errors[:5]:
        flash(e, "error")
    if not saved and not errors:
        flash("Choose at least one file.", "error")
    return redirect(url_for("paper") + "#uploads")


@app.route("/submissions/<int:subid>/file")
def submission_file(subid):
    reg = session.get("reg_no")
    row = get_db().execute("SELECT * FROM submissions WHERE id=?", (subid,)).fetchone()
    if not reg or not row or row["reg_no"] != reg:
        abort(404)
    return serve_submission(row)


@app.route("/submissions/<int:subid>/delete", methods=["POST"])
def submission_delete(subid):
    reg = session.get("reg_no")
    db = get_db()
    row = db.execute("SELECT * FROM submissions WHERE id=?", (subid,)).fetchone()
    if not reg or not row or row["reg_no"] != reg:
        abort(404)
    if get_setting("open") != "1" or get_setting("submit_open", "1") != "1":
        flash("Uploads are closed, so files can no longer be removed.", "error")
        return redirect(url_for("paper") + "#uploads")
    try:
        os.remove(os.path.join(SUBMISSIONS, row["stored_name"]))
    except OSError:
        pass
    db.execute("DELETE FROM submissions WHERE id=?", (subid,))
    db.commit()
    flash("File removed.", "ok")
    return redirect(url_for("paper") + "#uploads")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("reg_no", None)
    return redirect(url_for("home"))


@app.route("/datasets/<int:did>/download")
def dataset_download(did):
    row = get_db().execute("SELECT * FROM datasets WHERE id=?", (did,)).fetchone()
    if not row:
        abort(404)
    return send_from_directory(UPLOADS, row["stored_name"], as_attachment=True,
                               download_name=row["orig_name"])


# -------------------------------------------------------------- admin pages


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many_failures(ip):
            flash("Too many attempts. Wait a few minutes.", "error")
        elif secrets.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            return redirect(url_for("admin"))
        else:
            record_failure(ip)
            flash("Wrong password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    slots = get_slots(db)
    labels = question_labels(db)
    questions = {s["id"]: db.execute("SELECT * FROM questions WHERE slot_id=? ORDER BY id",
                                     (s["id"],)).fetchall() for s in slots}
    fixed = db.execute("SELECT * FROM fixed ORDER BY reg_no, slot_id").fetchall()
    slot_label = {s["id"]: s["label"] for s in slots}
    assign = {}
    for a in db.execute("SELECT * FROM assignments ORDER BY reg_no"):
        assign.setdefault(a["reg_no"], {})[a["slot_id"]] = a["question_id"]
    fixed_set = {(f["reg_no"], f["slot_id"]) for f in fixed}
    roster_count = db.execute("SELECT COUNT(*) FROM roster").fetchone()[0]
    roster_preview = db.execute("SELECT * FROM roster ORDER BY reg_no LIMIT 15").fetchall()
    datasets = db.execute("SELECT * FROM datasets ORDER BY id DESC").fetchall()
    subs = {}
    for r in db.execute("SELECT *, datetime(uploaded_at,'+330 minutes') AS ts FROM submissions "
                        "ORDER BY reg_no, slot_id, id"):
        subs.setdefault(r["reg_no"], []).append(r)
    return render_template(
        "admin.html", subs=subs, submit_open=get_setting("submit_open", "1") == "1",
        is_image=lambda n: n.rsplit(".", 1)[-1].lower() in SNAP_IMAGES,
        slots=slots, questions=questions, labels=labels, fixed=fixed,
        slot_label=slot_label, assign=assign, fixed_set=fixed_set,
        roster_count=roster_count, roster_preview=roster_preview, datasets=datasets,
        s_title=get_setting("title"), s_pin=get_setting("pin"),
        s_open=get_setting("open") == "1", s_instr=get_setting("instructions"),
        s_mode=get_setting("assignment_mode", "one_set"))


def back():
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    pin = request.form.get("pin", "").strip()
    if not pin:
        flash("PIN cannot be empty.", "error")
        return back()
    set_setting("title", request.form.get("title", "").strip() or "ML Internal Lab Exam")
    set_setting("pin", pin)
    set_setting("instructions", request.form.get("instructions", "").strip())
    set_setting("open", "1" if request.form.get("open") else "0")
    set_setting("submit_open", "1" if request.form.get("submit_open") else "0")
    mode = request.form.get("assignment_mode", "one_set")
    if mode in ("one_set", "all_slots"):
        set_setting("assignment_mode", mode)
    flash("Settings saved.", "ok")
    return back()


@app.route("/admin/slots/add", methods=["POST"])
@admin_required
def slot_add():
    raw = request.form.get("label", "").strip()
    label = " ".join(raw.split())
    if not label:
        flash("Enter a slot/set label like 'Set 1' or '3A'.", "error")
        return back()
    db = get_db()
    pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM slots").fetchone()[0]
    try:
        db.execute("INSERT INTO slots(label, position) VALUES(?,?)", (label, pos))
        db.commit()
        flash(f"Slot {label} added.", "ok")
    except sqlite3.IntegrityError:
        flash(f"Slot {label} already exists.", "error")
    return back()


@app.route("/admin/slots/generate", methods=["POST"])
@admin_required
def slot_generate():
    gen_type = request.form.get("gen_type", "parts")
    db = get_db()
    if gen_type == "sets":
        try:
            n_sets = max(1, min(int(request.form.get("n_sets", 4)), 20))
        except ValueError:
            flash("Enter a valid number for sets.", "error")
            return back()
        added = 0
        for i in range(1, n_sets + 1):
            label = f"Set {i}"
            pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM slots").fetchone()[0]
            try:
                db.execute("INSERT INTO slots(label, position) VALUES(?,?)", (label, pos))
                added += 1
            except sqlite3.IntegrityError:
                pass
        db.commit()
        flash(f"Added {added} new set(s).", "ok")
        return back()

    try:
        n = max(1, min(int(request.form.get("n_questions", 2)), 20))
        p = max(1, min(int(request.form.get("n_parts", 2)), 8))
    except ValueError:
        flash("Enter whole numbers.", "error")
        return back()
    added = 0
    for i in range(1, n + 1):
        for j in range(p):
            label = f"{i}{chr(65 + j)}"
            pos = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM slots").fetchone()[0]
            try:
                db.execute("INSERT INTO slots(label, position) VALUES(?,?)", (label, pos))
                added += 1
            except sqlite3.IntegrityError:
                pass
    db.commit()
    flash(f"Added {added} new slot(s).", "ok")
    return back()


@app.route("/admin/slots/<int:sid>/delete", methods=["POST"])
@admin_required
def slot_delete(sid):
    db = get_db()
    db.execute("DELETE FROM slots WHERE id=?", (sid,))
    db.commit()
    flash("Slot and its questions deleted.", "ok")
    return back()


@app.route("/admin/questions/add", methods=["POST"])
@admin_required
def question_add():
    db = get_db()
    sid = request.form.get("slot_id", type=int)
    if not db.execute("SELECT 1 FROM slots WHERE id=?", (sid,)).fetchone():
        flash("Pick a slot.", "error")
        return back()
    # several questions at once: separate with a line containing only ---
    parts = [p.strip() for p in request.form.get("text", "").replace("\r", "").split("\n---\n")]
    parts = [p for p in parts if p]
    for p in parts:
        db.execute("INSERT INTO questions(slot_id, text) VALUES(?,?)", (sid, p))
    db.commit()
    flash(f"Added {len(parts)} question(s).", "ok" if parts else "error")
    return back()


@app.route("/admin/questions/<int:qid>/update", methods=["POST"])
@admin_required
def question_update(qid):
    text = request.form.get("text", "").strip()
    if text:
        db = get_db()
        db.execute("UPDATE questions SET text=? WHERE id=?", (text, qid))
        db.commit()
        flash("Question updated.", "ok")
    return back()


@app.route("/admin/questions/<int:qid>/delete", methods=["POST"])
@admin_required
def question_delete(qid):
    db = get_db()
    db.execute("DELETE FROM questions WHERE id=?", (qid,))
    db.commit()
    flash("Question deleted. Students who had it will get a fresh random one on next load.", "ok")
    return back()


@app.route("/admin/roster", methods=["POST"])
@admin_required
def roster_update():
    db = get_db()
    action = request.form.get("action")
    if action == "clear":
        db.execute("DELETE FROM roster")
        db.commit()
        flash("Roster cleared. Any registration number with the PIN can now log in.", "ok")
        return back()
    text = request.form.get("data", "")
    f = request.files.get("file")
    if f and f.filename:
        text += "\n" + f.read().decode("utf-8-sig", errors="ignore")
    rows = []
    for line in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in line]
        if not cells or not cells[0]:
            continue
        reg = norm_reg(cells[0])
        if reg.lower().startswith(("reg", "roll")) and rows == []:
            continue  # header row
        rows.append((reg, cells[1] if len(cells) > 1 else ""))
    for reg, name in rows:
        db.execute("INSERT INTO roster(reg_no,name) VALUES(?,?) "
                   "ON CONFLICT(reg_no) DO UPDATE SET name=excluded.name", (reg, name))
    db.commit()
    flash(f"Roster updated: {len(rows)} row(s) processed.", "ok" if rows else "error")
    return back()


@app.route("/admin/fixed/add", methods=["POST"])
@admin_required
def fixed_add():
    db = get_db()
    qid = request.form.get("question_id", type=int)
    q = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    regs = [norm_reg(r) for r in request.form.get("regs", "").replace(",", " ").split()]
    if not q or not regs:
        flash("Choose a question and enter at least one registration number.", "error")
        return back()
    for reg in regs:
        db.execute("INSERT INTO fixed(reg_no,slot_id,question_id) VALUES(?,?,?) "
                   "ON CONFLICT(reg_no,slot_id) DO UPDATE SET question_id=excluded.question_id",
                   (reg, q["slot_id"], q["id"]))
        # a fixed rule overrides anything already locked for that student/slot
        db.execute("INSERT INTO assignments(reg_no,slot_id,question_id) VALUES(?,?,?) "
                   "ON CONFLICT(reg_no,slot_id) DO UPDATE SET question_id=excluded.question_id",
                   (reg, q["slot_id"], q["id"]))
    db.commit()
    flash(f"Fixed question set for {len(regs)} student(s).", "ok")
    return back()


@app.route("/admin/fixed/delete", methods=["POST"])
@admin_required
def fixed_delete():
    db = get_db()
    reg = request.form.get("reg_no", "")
    sid = request.form.get("slot_id", type=int)
    db.execute("DELETE FROM fixed WHERE reg_no=? AND slot_id=?", (reg, sid))
    db.execute("DELETE FROM assignments WHERE reg_no=? AND slot_id=?", (reg, sid))
    db.commit()
    flash("Fixed question removed; that student will get a random one on next load.", "ok")
    return back()


@app.route("/admin/assignments/reset", methods=["POST"])
@admin_required
def assignments_reset():
    db = get_db()
    reg = norm_reg(request.form.get("reg_no", ""))
    if reg:
        db.execute("DELETE FROM assignments WHERE reg_no=?", (reg,))
        flash(f"Assignments for {reg} reset (fixed questions are re-applied).", "ok")
    else:
        db.execute("DELETE FROM assignments")
        flash("All random assignments cleared (fixed questions are re-applied).", "ok")
    db.commit()
    return back()


@app.route("/admin/assignments.csv")
@admin_required
def assignments_csv():
    db = get_db()
    slots = get_slots(db)
    labels = question_labels(db)
    texts = {q["id"]: q["text"] for q in db.execute("SELECT id,text FROM questions")}
    names = {r["reg_no"]: r["name"] for r in db.execute("SELECT * FROM roster")}
    assign = {}
    for a in db.execute("SELECT * FROM assignments ORDER BY reg_no"):
        assign.setdefault(a["reg_no"], {})[a["slot_id"]] = a["question_id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reg_no", "name"] + [f"{s['label']} id" for s in slots] + [f"{s['label']} text" for s in slots])
    for reg, m in assign.items():
        w.writerow([reg, names.get(reg, "")]
                   + [labels.get(m.get(s["id"]), "") for s in slots]
                   + [texts.get(m.get(s["id"]), "") for s in slots])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=assignments.csv"})


@app.route("/admin/datasets/upload", methods=["POST"])
@admin_required
def dataset_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return back()
    orig = secure_filename(f.filename) or "dataset"
    stored = f"{uuid.uuid4().hex}_{orig}"
    path = os.path.join(UPLOADS, stored)
    f.save(path)
    db = get_db()
    db.execute("INSERT INTO datasets(title,description,orig_name,stored_name,size) VALUES(?,?,?,?,?)",
               (request.form.get("title", "").strip() or orig,
                request.form.get("description", "").strip(), orig, stored, os.path.getsize(path)))
    db.commit()
    flash("Dataset uploaded. It is now visible on the home page.", "ok")
    return back()


@app.route("/admin/datasets/<int:did>/delete", methods=["POST"])
@admin_required
def dataset_delete(did):
    db = get_db()
    row = db.execute("SELECT * FROM datasets WHERE id=?", (did,)).fetchone()
    if row:
        try:
            os.remove(os.path.join(UPLOADS, row["stored_name"]))
        except OSError:
            pass
        db.execute("DELETE FROM datasets WHERE id=?", (did,))
        db.commit()
        flash("Dataset removed.", "ok")
    return back()


@app.route("/admin/submissions/<int:subid>/file")
@admin_required
def admin_submission_file(subid):
    row = get_db().execute("SELECT * FROM submissions WHERE id=?", (subid,)).fetchone()
    if not row:
        abort(404)
    return serve_submission(row)


@app.route("/admin/submissions/<int:subid>/delete", methods=["POST"])
@admin_required
def admin_submission_delete(subid):
    db = get_db()
    row = db.execute("SELECT * FROM submissions WHERE id=?", (subid,)).fetchone()
    if row:
        try:
            os.remove(os.path.join(SUBMISSIONS, row["stored_name"]))
        except OSError:
            pass
        db.execute("DELETE FROM submissions WHERE id=?", (subid,))
        db.commit()
        flash("File removed.", "ok")
    return back()


@app.route("/admin/submissions.zip")
@admin_required
def submissions_zip():
    """All snapshots as <reg_no>/<slot>_<id>_<filename>."""
    db = get_db()
    only = norm_reg(request.args.get("reg", ""))
    sql = ("SELECT s.*, sl.label FROM submissions s JOIN slots sl ON sl.id=s.slot_id "
           + ("WHERE s.reg_no=? " if only else "") + "ORDER BY s.reg_no, sl.position, s.id")
    rows = db.execute(sql, (only,) if only else ()).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for r in rows:
            path = os.path.join(SUBMISSIONS, r["stored_name"])
            if os.path.exists(path):
                folder = secure_filename(r["reg_no"]) or "unknown"
                z.write(path, f"{folder}/{secure_filename(r['label'])}_{r['id']}_{r['orig_name']}")
    name = f"snapshots_{only}.zip" if only else "snapshots_all.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.template_filter("filesize")
def filesize(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
