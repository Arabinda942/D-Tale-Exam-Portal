import os, re, hmac, secrets, time
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, session, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text as sql
from sqlalchemy.orm import deferred
from werkzeug.utils import secure_filename

app = Flask(__name__)
db_url = os.environ.get("DATABASE_URL", "sqlite:///dtale.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
TEACHER_PIN = os.environ.get("TEACHER_PIN", "dtale123")
GRACE_MIN = int(os.environ.get("UPLOAD_GRACE_MIN", "30"))
db = SQLAlchemy(app)


class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200))
    title = db.Column(db.String(200))
    duration = db.Column(db.Integer)
    marks = db.Column(db.String(50))
    instructions = db.Column(db.Text)
    text = db.Column(db.Text)
    pdf_name = db.Column(db.String(300))
    pdf_data = deferred(db.Column(db.LargeBinary))
    active = db.Column(db.Boolean, default=True)
    created = db.Column(db.DateTime, default=datetime.utcnow)


class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, index=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"))
    name = db.Column(db.String(200))
    roll = db.Column(db.String(100))
    cls = db.Column(db.String(200))
    contact = db.Column(db.String(200))
    started = db.Column(db.DateTime)
    finished = db.Column(db.DateTime)
    switches = db.Column(db.Integer, default=0)
    submitted = db.Column(db.Boolean, default=False)
    exam = db.relationship("Exam")


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200))
    exam_title = db.Column(db.String(200))
    name = db.Column(db.String(200))
    roll = db.Column(db.String(100))
    cls = db.Column(db.String(200))
    contact = db.Column(db.String(200))
    started = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    switches = db.Column(db.Integer, default=0)
    file_name = db.Column(db.String(300))
    file_data = db.Column(db.LargeBinary)


with app.app_context():
    db.create_all()
    # add PDF columns to an exam table created by an older version
    cols = {c["name"] for c in inspect(db.engine).get_columns("exam")}
    blob = "BYTEA" if db.engine.dialect.name == "postgresql" else "BLOB"
    with db.engine.begin() as cn:
        if "pdf_name" not in cols:
            cn.execute(sql("ALTER TABLE exam ADD COLUMN pdf_name VARCHAR(300)"))
        if "pdf_data" not in cols:
            cn.execute(sql("ALTER TABLE exam ADD COLUMN pdf_data " + blob))


def err(msg, code=400):
    return jsonify(error=msg), code


def iso(d):
    return d.isoformat() + "Z" if d else None


def active_exam():
    return Exam.query.filter_by(active=True).order_by(Exam.id.desc()).first()


def q_count(text):
    return len(re.findall(r"^\s*(?:Q(?:uestion)?\.?\s*)?\d+\s*[.):]", text or "", re.I | re.M))


def end_of(a):
    return a.started + timedelta(minutes=a.exam.duration)


def phase(a):
    if a.submitted:
        return "done"
    if a.finished or datetime.utcnow() >= end_of(a):
        return "upload"
    return "exam"


def get_attempt(token):
    return Attempt.query.filter_by(token=token or "").first()


# ---------- teacher auth ----------
fails = {}


def teacher_only(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("teacher"):
            return err("Not authorised", 401)
        return f(*a, **k)
    return w


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store" if request.path.startswith("/api") else r.headers.get("Cache-Control", "")
    return r


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


# ---------- student API ----------
@app.get("/api/exam")
def api_exam():
    e = active_exam()
    if not e:
        return jsonify(exam=None)
    return jsonify(exam=dict(subject=e.subject, title=e.title, duration=e.duration, marks=e.marks,
                             instructions=e.instructions, questions=q_count(e.text), has_pdf=bool(e.pdf_name)))


@app.post("/api/start")
def api_start():
    d = request.get_json(silent=True) or {}
    name, roll = (d.get("name") or "").strip()[:200], (d.get("roll") or "").strip()[:100]
    if not name or not roll:
        return err("Name and Roll/ID are required.")
    e = active_exam()
    if not e:
        return err("No exam is available right now.")
    a = Attempt(token=secrets.token_urlsafe(24), exam_id=e.id, name=name, roll=roll,
                cls=(d.get("cls") or "").strip()[:200], contact=(d.get("contact") or "").strip()[:200],
                started=datetime.utcnow())
    db.session.add(a)
    db.session.commit()
    return jsonify(token=a.token)


@app.get("/api/attempt/<token>")
def api_attempt(token):
    a = get_attempt(token)
    if not a:
        return err("Attempt not found", 404)
    p = phase(a)
    out = dict(phase=p, name=a.name, roll=a.roll, cls=a.cls, subject=a.exam.subject, title=a.exam.title,
               has_pdf=bool(a.exam.pdf_name))
    if p == "exam":
        out["remaining"] = max(0, (end_of(a) - datetime.utcnow()).total_seconds())
        out["text"] = a.exam.text
    return jsonify(out)


@app.get("/api/attempt/<token>/paper.pdf")
def api_paper(token):
    a = get_attempt(token)
    if not a or phase(a) != "exam" or not a.exam.pdf_name:
        return err("Not available", 403)
    return send_file(BytesIO(a.exam.pdf_data), mimetype="application/pdf", download_name="paper.pdf")


@app.post("/api/switch")
def api_switch():
    a = get_attempt((request.get_json(silent=True) or {}).get("token"))
    if a and phase(a) == "exam":
        a.switches = (a.switches or 0) + 1
        db.session.commit()
    return jsonify(ok=True)


@app.post("/api/finish")
def api_finish():
    a = get_attempt((request.get_json(silent=True) or {}).get("token"))
    if not a:
        return err("Attempt not found", 404)
    if phase(a) == "exam":
        a.finished = datetime.utcnow()
        db.session.commit()
    return jsonify(ok=True)


@app.post("/api/submit")
def api_submit():
    a = get_attempt(request.form.get("token"))
    if not a:
        return err("Attempt not found", 404)
    p = phase(a)
    if p == "done":
        return err("Already submitted.")
    if p == "exam":
        return err("The exam is still running.")
    closed = (a.finished or end_of(a)) + timedelta(minutes=GRACE_MIN)
    if datetime.utcnow() > closed:
        return err("The upload window has closed. Please contact your teacher.")
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return err("Only PDF files are accepted.")
    data = f.read()
    if not data.startswith(b"%PDF-"):
        return err("This does not look like a valid PDF file.")
    db.session.add(Submission(subject=a.exam.subject, exam_title=a.exam.title, name=a.name, roll=a.roll,
                              cls=a.cls, contact=a.contact, started=a.started, switches=a.switches or 0,
                              file_name=secure_filename(f.filename) or "answer.pdf", file_data=data))
    a.submitted = True
    db.session.commit()
    return jsonify(ok=True)


# ---------- teacher API ----------
@app.post("/api/teacher/login")
def t_login():
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr)
    n, t = fails.get(ip, (0, 0))
    if n >= 5 and time.time() - t < 300:
        return err("Too many attempts. Try again in a few minutes.", 429)
    pin = (request.get_json(silent=True) or {}).get("pin", "")
    if hmac.compare_digest(str(pin).encode(), TEACHER_PIN.encode()):
        fails.pop(ip, None)
        session["teacher"] = True
        return jsonify(ok=True)
    fails[ip] = (n + 1, time.time())
    return err("Wrong PIN", 401)


@app.get("/api/teacher/me")
@teacher_only
def t_me():
    return jsonify(ok=True)


@app.route("/api/teacher/exam", methods=["GET", "POST", "DELETE"])
@teacher_only
def t_exam():
    if request.method == "GET":
        e = active_exam()
        return jsonify(exam=e and dict(subject=e.subject, title=e.title, duration=e.duration,
                                       marks=e.marks, instructions=e.instructions, text=e.text,
                                       has_pdf=bool(e.pdf_name), pdf_name=e.pdf_name))
    if request.method == "DELETE":
        Exam.query.update({"active": False})
        db.session.commit()
        return jsonify(ok=True)
    d = request.form if request.form else (request.get_json(silent=True) or {})
    try:
        dur = int(d.get("duration"))
    except (TypeError, ValueError):
        dur = 0
    subject, title, text = (d.get("subject") or "").strip(), (d.get("title") or "").strip(), (d.get("text") or "").strip()
    pdf_name = pdf_data = None
    f = request.files.get("paper")
    if f and f.filename:
        if not f.filename.lower().endswith(".pdf"):
            return err("The exam paper file must be a PDF.")
        pdf_data = f.read()
        if not pdf_data.startswith(b"%PDF-"):
            return err("This does not look like a valid PDF file.")
        pdf_name = secure_filename(f.filename) or "paper.pdf"
    elif d.get("keep_pdf") == "1":
        prev = active_exam()
        if prev and prev.pdf_name:
            pdf_name, pdf_data = prev.pdf_name, prev.pdf_data
    if not subject or not title or dur < 1 or not (text or pdf_data):
        return err("Subject, title, duration and an exam paper (text or PDF) are required.")
    Exam.query.update({"active": False})
    db.session.add(Exam(subject=subject[:200], title=title[:200], duration=dur, marks=(d.get("marks") or "")[:50],
                        instructions=d.get("instructions") or "", text=text, pdf_name=pdf_name, pdf_data=pdf_data))
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/teacher/exam/paper.pdf")
@teacher_only
def t_paper():
    e = active_exam()
    if not e or not e.pdf_name:
        return err("No PDF paper", 404)
    return send_file(BytesIO(e.pdf_data), mimetype="application/pdf", download_name=e.pdf_name)


@app.get("/api/teacher/subs")
@teacher_only
def t_subs():
    rows = Submission.query.order_by(Submission.id.desc()).all()
    return jsonify(subs=[dict(id=s.id, subject=s.subject, examTitle=s.exam_title, name=s.name, roll=s.roll,
                              cls=s.cls, contact=s.contact, at=iso(s.submitted_at), switches=s.switches,
                              fileName=s.file_name) for s in rows])


@app.get("/api/teacher/subs/<int:sid>/file")
@teacher_only
def t_file(sid):
    s = db.session.get(Submission, sid)
    if not s:
        return err("Not found", 404)
    name = secure_filename(f"{s.name}_{s.roll}_{s.file_name}") or "answer.pdf"
    return send_file(BytesIO(s.file_data), mimetype="application/pdf", as_attachment=True, download_name=name)


@app.delete("/api/teacher/subs")
@teacher_only
def t_clear():
    Submission.query.delete()
    db.session.commit()
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)
