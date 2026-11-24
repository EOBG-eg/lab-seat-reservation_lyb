import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

app = Flask(__name__)
# (GitHub 공개용 수정) 비밀값을 코드에 적지 않고 환경변수에서 읽는다. 없으면 실행할 때마다 무작위로 만든다
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "lab_system.db")

# 현재 켜져 있는 cloudflared 터널 주소. 터널을 다시 켜면 주소가 바뀌므로 바뀐 주소만 여기에 넣어주세요.
# (서버 동작에는 영향이 없고, 관리자 화면의 '휴대폰 접속 주소' 안내/QR에 표시됩니다.)
# (GitHub 공개용 수정) 예전 임시 주소는 이미 만료되어 비워 둔다. 비어 있으면 지금 접속한 주소를 보여준다
PUBLIC_TUNNEL_URL = os.environ.get("PUBLIC_TUNNEL_URL", "")

# 회원가입 입력 규칙 (학번은 숫자 7자리, 비밀번호는 4자 이상)
STUDENT_ID_PATTERN = re.compile(r"[0-9]{7}")
MIN_PASSWORD_LEN = 4

# 주의사항 입력 제한 (도배 방지)
MAX_NOTES_PER_SEAT = 30
MAX_MSG_LEN, MAX_PATH_LEN = 300, 200

# 관리자 화면의 '시연용 시계'가 설정하는 값. 서버를 다시 켜면 실제 시각으로 돌아옵니다.
_clock_offset = timedelta(0)

PHASE_LABEL = {"future": "예정", "active": "진행중", "past": "종료"}


# ───────────────────────── 시계 / 시간대 ─────────────────────────
def now_dt():
    """앱이 사용하는 현재 시각. 시연용 시계를 켜면 실제 시각에 오프셋이 더해집니다."""
    return datetime.now() + _clock_offset


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(value):
    """'YYYY-MM-DD HH:MM:SS' 또는 (예전 데이터) 'HH:MM:SS' 문자열을 datetime으로."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.combine(now_dt().date(), datetime.strptime(value, "%H:%M:%S").time())
    except ValueError:
        return None


def parse_slot(time_range, base):
    """'10:00 ~ 12:00' → base 날짜의 (시작, 종료) datetime. 형식이 틀리면 ValueError."""
    start_s, end_s = [t.strip() for t in time_range.split("~")]
    day = base.replace(second=0, microsecond=0)
    sh, sm = (int(x) for x in start_s.split(":"))
    eh, em = (int(x) for x in end_s.split(":"))
    return day.replace(hour=sh, minute=sm), day.replace(hour=eh, minute=em)


def slot_phase(slot, now=None):
    """시간대의 현재 상태: future(시작 전) / active(진행 중) / past(종료). 형식을 읽을 수 없으면 항상 active."""
    now = now or now_dt()
    time_range = slot if isinstance(slot, str) else slot["time_range"]
    try:
        start, end = parse_slot(time_range, now)
    except ValueError:
        return "active"
    if now < start:
        return "future"
    return "past" if now >= end else "active"


def clock_info():
    return {"demo": _clock_offset != timedelta(0), "now": now_dt().strftime("%H:%M")}


def default_slot(slots):
    """처음 보여줄 시간대: 진행 중인 시간대 → 없으면 가장 가까운 예정 시간대 → 없으면 마지막."""
    for wanted in ("active", "future"):
        for s in slots:
            if slot_phase(s) == wanted:
                return s
    return slots[-1]


app.jinja_env.globals.update(phase_of=slot_phase, PHASE_LABEL=PHASE_LABEL)


def like_pattern(text):
    """LIKE 검색용: 검색어에 들어 있는 % _ 를 와일드카드가 아닌 글자 그대로 취급 (ESCAPE '\\' 와 함께 사용)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def safe_next(url):
    """로그인 후 돌아갈 주소는 같은 사이트 내부 경로만 허용 (open redirect 방지)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


# ───────────────────────── DB ─────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_log(conn, slot_time, seat_num, user_info, action_type, memo):
    conn.execute(
        "INSERT INTO audit_logs (timestamp, slot_time, seat_num, user_info, action_type, memo) VALUES (?, ?, ?, ?, ?, ?)",
        (now_str(), slot_time, seat_num, user_info, action_type, memo))


def send_notification(conn, target_user_id, message, event_key):
    if target_user_id and conn.execute("SELECT id FROM notifications WHERE event_key = ?", (event_key,)).fetchone() is None:
        conn.execute("INSERT INTO notifications (target_user_id, message, event_key, created_at) VALUES (?, ?, ?, ?)",
                     (target_user_id, message, event_key, now_dt().strftime("%H:%M:%S")))


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, password TEXT NOT NULL,
            user_name TEXT NOT NULL, role TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS time_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time_range TEXT NOT NULL,
            slot_type TEXT NOT NULL, title TEXT NOT NULL
        )
    """)
    # caution_* 컬럼은 예전 구조(좌석당 주의사항 1개)의 흔적입니다. 지금은 seat_cautions 테이블을 사용합니다.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER NOT NULL,
            seat_num TEXT NOT NULL, status TEXT DEFAULT 'EMPTY', user_id TEXT, user_name TEXT,
            last_user_info TEXT DEFAULT '이력 없음', caution_owner_id TEXT DEFAULT '',
            caution_owner_name TEXT DEFAULT '', caution_path TEXT DEFAULT '', caution_msg TEXT DEFAULT '',
            reserved_at TEXT,
            FOREIGN KEY (slot_id) REFERENCES time_slots (id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seat_cautions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seat_num TEXT NOT NULL,
            user_id TEXT NOT NULL, user_name TEXT NOT NULL,
            path TEXT DEFAULT '', message TEXT NOT NULL, created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS waiting_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, user_name TEXT NOT NULL, created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, slot_time TEXT NOT NULL,
            seat_num TEXT NOT NULL, user_info TEXT NOT NULL, action_type TEXT NOT NULL, memo TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target_user_id TEXT NOT NULL,
            message TEXT NOT NULL, event_key TEXT UNIQUE, is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        default_users = [
            ("2221042", "1234", "이유빈", "STUDENT"), ("2221088", "1234", "김민수", "STUDENT"),
            ("2221099", "1234", "박지훈", "STUDENT"), ("2221105", "1234", "최수아", "STUDENT"),
            ("2221112", "1234", "정우성", "STUDENT"), ("2221120", "1234", "한지민", "STUDENT"),
            ("admin", "admin1234", "담당교수/조교", "ADMIN")
        ]
        cursor.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", default_users)

    cursor.execute("SELECT COUNT(*) FROM time_slots")
    if cursor.fetchone()[0] == 0:
        slots = [
            ("10:00 ~ 12:00", "CLASS", "인터넷프로그래밍 (정규수업)"),
            ("12:00 ~ 14:00", "FREE", "공강 자율 실습"),
            ("14:00 ~ 16:00", "FREE", "공강 자율 실습"),
            ("16:00 ~ 18:00", "CLASS", "자바프로그래밍응용 (정규수업)")
        ]
        cursor.executemany("INSERT INTO time_slots (time_range, slot_type, title) VALUES (?, ?, ?)", slots)

        for slot_id in range(1, 5):
            for i in range(1, 13):
                seat_num = f"PC-{i:02d}"
                if i == 5:
                    cursor.execute("""
                        INSERT INTO seats (slot_id, seat_num, status, last_user_info, caution_owner_id, caution_owner_name, caution_path, caution_msg)
                        VALUES (?, ?, 'EMPTY', ?, ?, ?, ?, ?)
                    """, (slot_id, seat_num, "2221042 이유빈", "2221042", "이유빈", "C:\\workspace\\2221042_인터넷프로그래밍", "교수님 대면 검사 예정인 프로젝트가 있습니다. 절대 삭제/변경하지 말아주세요!"))
                elif i == 2:
                    cursor.execute("""
                        INSERT INTO seats (slot_id, seat_num, status, last_user_info, caution_owner_id, caution_owner_name, caution_path, caution_msg)
                        VALUES (?, ?, 'EMPTY', ?, ?, ?, ?, ?)
                    """, (slot_id, seat_num, "2221105 최수아", "2221105", "최수아", "D:\\java_lab\\socket_server", "팀프로젝트 서버 코드가 바탕화면에 있습니다. 폴더 이동 금지 부탁드립니다."))
                else:
                    cursor.execute("INSERT INTO seats (slot_id, seat_num, status, last_user_info) VALUES (?, ?, 'EMPTY', '이력 없음')", (slot_id, seat_num))

        add_log(conn, "10:00 ~ 12:00", "PC-05", "2221042 이유빈", "주의사항 등록", "보호경로 등록 완료")

    # ── 구조 변경 마이그레이션 (여러 번 실행해도 안전) ──
    # 1) 점유 시각 컬럼: 예약/사용을 시작한 시각. 시간대가 끝난 뒤 남은 상태를 정리하는 기준입니다.
    if "reserved_at" not in [r[1] for r in cursor.execute("PRAGMA table_info(seats)").fetchall()]:
        cursor.execute("ALTER TABLE seats ADD COLUMN reserved_at TEXT")
    cursor.execute("UPDATE seats SET reserved_at = ? WHERE status != 'EMPTY' AND reserved_at IS NULL", (now_str(),))

    # 2) 좌석당 1개였던 주의사항을 seat_cautions(여러 개) 테이블로 이전. 이전 후에는 옛 컬럼을 비워 되살아나지 않게 함.
    if cursor.execute("SELECT COUNT(*) FROM seat_cautions").fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO seat_cautions (seat_num, user_id, user_name, path, message, created_at)
            SELECT seat_num, COALESCE(caution_owner_id, ''), COALESCE(caution_owner_name, ''), COALESCE(caution_path, ''), caution_msg, ?
            FROM seats WHERE caution_msg IS NOT NULL AND caution_msg != '' GROUP BY seat_num
        """, (now_str(),))
        cursor.execute("UPDATE seats SET caution_owner_id = '', caution_owner_name = '', caution_path = '', caution_msg = ''")

    # 3) 로그 용어 정리 (인증 기능 제거에 따라)
    cursor.execute("UPDATE audit_logs SET action_type = '반납' WHERE action_type IN ('퇴실', '퇴실/반납', '퇴실/주의사항 등록')")
    cursor.execute("UPDATE audit_logs SET action_type = '사용 시작' WHERE action_type = '본인 인증'")

    conn.commit()
    conn.close()


# ───────────────────────── 시간대 종료 후 자동 정리 ─────────────────────────
def sweep_ended_slots():
    """시간대가 끝났는데 남아 있는 사용/예약/대기 상태를 정리. 모든 요청 앞에서 실행됩니다.
    예약·사용을 시작한 날짜 기준으로 판단하므로, 어제 남은 상태도 오늘 첫 요청에서 정리됩니다."""
    conn = get_db()
    try:
        now, changed = now_dt(), False
        rows = conn.execute("SELECT s.*, t.time_range FROM seats s JOIN time_slots t ON s.slot_id = t.id WHERE s.status != 'EMPTY'").fetchall()
        for r in rows:
            claimed = parse_ts(r["reserved_at"]) or now
            try:
                _, end = parse_slot(r["time_range"], claimed)
            except ValueError:
                continue
            if now < end:
                continue
            cur = conn.execute("UPDATE seats SET status = 'EMPTY', user_id = NULL, user_name = NULL, reserved_at = NULL WHERE id = ? AND status = ? AND user_id = ?",
                               (r["id"], r["status"], r["user_id"]))
            if cur.rowcount == 0:
                continue
            changed = True
            user_full = f"{r['user_id']} {r['user_name']}"
            if r["status"] == "OCCUPIED":
                add_log(conn, r["time_range"], r["seat_num"], user_full, "시간 종료 자동 반납", "시간대 종료로 자동 반납 처리")
            else:
                add_log(conn, r["time_range"], r["seat_num"], user_full, "예약 만료", "사용하지 않고 시간대가 종료되어 예약 만료")
                send_notification(conn, r["user_id"], f"[예약 만료] {r['time_range']} {r['seat_num']} 예약이 사용 없이 시간대가 종료되어 만료되었습니다.",
                                  f"EXPIRE_{r['id']}_{r['reserved_at']}_{r['user_id']}")

        for q in conn.execute("SELECT w.*, t.time_range FROM waiting_queue w JOIN time_slots t ON w.slot_id = t.id").fetchall():
            created = parse_ts(q["created_at"]) or now
            try:
                _, end = parse_slot(q["time_range"], created)
            except ValueError:
                continue
            if now >= end:
                conn.execute("DELETE FROM waiting_queue WHERE id = ?", (q["id"],))
                changed = True
        if changed:
            conn.commit()
    finally:
        conn.close()


@app.before_request
def _sweep_before_request():
    if request.endpoint != "static":
        sweep_ended_slots()


# ───────────────────────── 접근 제어 ─────────────────────────
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.method == "GET":
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session or session.get("role") != "ADMIN":
            flash("관리자 전용 페이지입니다.")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        next_url = safe_next(request.form.get("next", ""))
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE user_id = ? AND password = ?",
                            (request.form.get("user_id", "").strip(), request.form.get("password", ""))).fetchone()
        conn.close()

        if user:
            session["user_id"], session["user_name"], session["role"] = user["user_id"], user["user_name"], user["role"]
            if next_url:
                return redirect(next_url)
            return redirect(url_for("admin_dashboard")) if user["role"] == "ADMIN" else redirect(url_for("index"))
        flash("학번(아이디) 또는 비밀번호가 일치하지 않습니다.")
        return render_template("login.html", next_url=next_url or "")
    return render_template("login.html", next_url=safe_next(request.args.get("next", "")) or "")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """학생 회원가입: 학번 + 이름 + 비밀번호. 가입 계정은 항상 학생 권한이며, 가입 후 바로 로그인됩니다."""
    if request.method == "POST":
        user_id, name = request.form.get("user_id", "").strip(), request.form.get("user_name", "").strip()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        error = None
        if not STUDENT_ID_PATTERN.fullmatch(user_id):
            error = "학번은 숫자 7자리로 입력해 주세요. (예: 2221042)"
        elif not (2 <= len(name) <= 20):
            error = "이름은 2~20자로 입력해 주세요."
        elif len(pw) < MIN_PASSWORD_LEN:
            error = f"비밀번호는 {MIN_PASSWORD_LEN}자 이상으로 입력해 주세요."
        elif pw != pw2:
            error = "비밀번호 확인이 일치하지 않습니다."
        if not error:
            conn = get_db()
            if conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
                error = "이미 가입된 학번입니다. 로그인하거나 교수/조교에게 문의하세요."
            else:
                try:
                    conn.execute("INSERT INTO users (user_id, password, user_name, role) VALUES (?, ?, ?, 'STUDENT')", (user_id, pw, name))
                    add_log(conn, "-", "-", f"{user_id} {name}", "회원가입", "학생 회원가입")
                    conn.commit()
                except sqlite3.IntegrityError:   # 동시에 같은 학번으로 가입한 경우
                    error = "이미 가입된 학번입니다. 로그인하거나 교수/조교에게 문의하세요."
            conn.close()
        if error:
            flash(error)
            return render_template("signup.html", form={"user_id": user_id, "user_name": name})
        session["user_id"], session["user_name"], session["role"] = user_id, name, "STUDENT"
        flash(f"{name}님, 가입이 완료되었습니다. 환영합니다!")
        return redirect(url_for("index"))
    return render_template("signup.html", form={"user_id": "", "user_name": ""})


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        cur_pw, new_pw, new_pw2 = request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("new_password2", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (session["user_id"],)).fetchone()
        if not user or user["password"] != cur_pw:
            flash("현재 비밀번호가 올바르지 않습니다.")
        elif len(new_pw) < MIN_PASSWORD_LEN:
            flash(f"새 비밀번호는 {MIN_PASSWORD_LEN}자 이상으로 입력해 주세요.")
        elif new_pw != new_pw2:
            flash("새 비밀번호 확인이 일치하지 않습니다.")
        elif new_pw == cur_pw:
            flash("현재 비밀번호와 다른 비밀번호를 입력해 주세요.")
        else:
            conn.execute("UPDATE users SET password = ? WHERE user_id = ?", (new_pw, session["user_id"]))
            add_log(conn, "-", "-", f"{session['user_id']} {session['user_name']}", "비밀번호 변경", "비밀번호 변경")
            conn.commit()
            conn.close()
            flash("비밀번호가 변경되었습니다.")
            return redirect(url_for("admin_dashboard") if session["role"] == "ADMIN" else url_for("index"))
        conn.close()
    return render_template("change_password.html", is_admin=session["role"] == "ADMIN")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ───────────────────────── 주의사항 (좌석마다 여러 명이 남길 수 있음) ─────────────────────────
def load_notes(conn):
    """{seat_num: [주의사항, ...]} — 오래된 것부터."""
    notes = {}
    for r in conn.execute("SELECT * FROM seat_cautions ORDER BY id ASC").fetchall():
        notes.setdefault(r["seat_num"], []).append({
            "id": r["id"], "user_id": r["user_id"], "user_name": r["user_name"],
            "path": r["path"] or "", "message": r["message"], "created_at": r["created_at"][:16],
        })
    return notes


def add_note(conn, seat_num, user_id, user_name, path, message, slot_time):
    path, message = (path or "").strip(), (message or "").strip()
    if not message:
        return False, "주의사항 내용을 입력해 주세요."
    if len(message) > MAX_MSG_LEN or len(path) > MAX_PATH_LEN:
        return False, f"내용은 {MAX_MSG_LEN}자, 경로는 {MAX_PATH_LEN}자 이내로 작성해 주세요."
    if conn.execute("SELECT COUNT(*) FROM seat_cautions WHERE seat_num = ?", (seat_num,)).fetchone()[0] >= MAX_NOTES_PER_SEAT:
        return False, "이 PC에는 더 이상 주의사항을 등록할 수 없습니다. 오래된 주의사항을 정리해 주세요."
    conn.execute("INSERT INTO seat_cautions (seat_num, user_id, user_name, path, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (seat_num, user_id, user_name, path, message, now_str()))
    add_log(conn, slot_time, seat_num, f"{user_id} {user_name}", "주의사항 등록", f"[{path or '경로 미등록'}] {message}")
    return True, f"[{seat_num}] 주의사항이 등록되었습니다."


def notify_note_authors(conn, seat_num, slot, user_id, user_full, verb):
    """이 PC에 주의사항을 남긴 다른 사람들에게, 누가 동의하고 예약/사용했는지 알려줌."""
    for author in {n["user_id"] for n in load_notes(conn).get(seat_num, []) if n["user_id"] and n["user_id"] != user_id}:
        send_notification(conn, author, f"[내 PC 알림] {user_full} 학생이 주의사항에 동의하고 {slot['time_range']} {seat_num}을(를) {verb}했습니다.",
                          f"NOTE_{verb}_{slot['id']}_{seat_num}_{user_id}_{author}_{now_dt():%H%M%S}")


@app.route("/note/delete", methods=["POST"])
@login_required
def note_delete():
    """작성자 본인(또는 관리자)만 주의사항 삭제 가능."""
    conn = get_db()
    note_id, slot_id = request.form.get("note_id", 0, type=int), request.form.get("slot_id", type=int)
    note = conn.execute("SELECT * FROM seat_cautions WHERE id = ?", (note_id,)).fetchone()
    if not note:
        flash("이미 삭제되었거나 존재하지 않는 주의사항입니다.")
    elif note["user_id"] != session["user_id"] and session["role"] != "ADMIN":
        flash("본인이 남긴 주의사항만 삭제할 수 있습니다.")
    else:
        conn.execute("DELETE FROM seat_cautions WHERE id = ?", (note_id,))
        by_admin = note["user_id"] != session["user_id"]
        add_log(conn, "-", note["seat_num"], f"{session['user_id']} {session['user_name']}", "주의사항 삭제",
                f"{'[관리자 삭제] ' if by_admin else ''}{note['user_name']}님의 주의사항 삭제: {note['message'][:60]}")
        conn.commit()
        flash("주의사항을 삭제했습니다.")
    conn.close()
    if request.form.get("from_page") == "admin":
        return redirect(url_for("admin_dashboard", slot_id=slot_id))
    return redirect(url_for("index", slot_id=slot_id))


# ───────────────────────── 학생 화면 ─────────────────────────
@app.route("/api/slot_snapshot")
@login_required
def api_slot_snapshot():
    """화면 자동 새로고침 판단용: 좌석 상태 + 주의사항 + 대기열 + 시간대 진행 상태가 바뀌면 값이 달라짐."""
    conn = get_db()
    slot_id = request.args.get("slot_id", 3, type=int)
    rows = conn.execute("SELECT id, status, user_id FROM seats WHERE slot_id = ? ORDER BY id ASC", (slot_id,)).fetchall()
    notes = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM seat_cautions").fetchone()
    queue = conn.execute("SELECT COUNT(*) FROM waiting_queue WHERE slot_id = ?", (slot_id,)).fetchone()[0]
    phases = "".join(slot_phase(s)[0] for s in conn.execute("SELECT * FROM time_slots ORDER BY id").fetchall())
    conn.close()
    seat_sig = "|".join(f"{r['id']}:{r['status']}:{r['user_id']}" for r in rows)
    return jsonify({"signature": f"{seat_sig}|N{notes[0]}:{notes[1]}|Q{queue}|P{phases}"})


@app.route("/")
@login_required
def index():
    conn = get_db()
    user_id, seat_filter = session["user_id"], request.args.get("seat_filter", "ALL")

    slots = conn.execute("SELECT * FROM time_slots ORDER BY id").fetchall()
    current_slot = next((s for s in slots if s["id"] == request.args.get("slot_id", type=int)), None) or default_slot(slots)
    phase = slot_phase(current_slot)

    my_active_seat = conn.execute("SELECT * FROM seats WHERE slot_id = ? AND user_id = ? AND status != 'EMPTY'", (current_slot["id"], user_id)).fetchone()
    notes_by_seat = load_notes(conn)

    seats = [dict(s) for s in conn.execute("SELECT * FROM seats WHERE slot_id = ? ORDER BY id ASC", (current_slot["id"],)).fetchall()]
    for s in seats:
        notes = notes_by_seat.get(s["seat_num"], [])
        s["notes_total"] = len(notes)
        s["notes_others"] = sum(1 for n in notes if n["user_id"] != user_id)
    if seat_filter == "EMPTY":
        seats = [s for s in seats if s["status"] == "EMPTY"]
    elif seat_filter == "HAS_CAUTION":
        seats = [s for s in seats if s["notes_total"]]
    elif seat_filter == "MY_RECENT":
        seats = [s for s in seats if user_id in (s["last_user_info"] or "") or any(n["user_id"] == user_id for n in notes_by_seat.get(s["seat_num"], []))]

    queue_list = conn.execute("SELECT * FROM waiting_queue WHERE slot_id = ? ORDER BY id ASC", (current_slot["id"],)).fetchall()
    notifications = conn.execute("SELECT * FROM notifications WHERE target_user_id = ? ORDER BY id DESC LIMIT 6", (user_id,)).fetchall()
    conn.close()

    return render_template("index.html", slots=slots, current_slot=current_slot, phase=phase, seats=seats, my_active_seat=my_active_seat,
                           notes_by_seat=notes_by_seat, queue_list=queue_list, notifications=notifications, current_user_id=user_id,
                           current_user_name=session["user_name"], current_role=session["role"], seat_filter=seat_filter, clock=clock_info())


def _go_back(slot_id):
    if request.form.get("from_page") == "admin":
        return redirect(url_for("admin_dashboard", slot_id=slot_id))
    return redirect(url_for("index", slot_id=slot_id))


def _promote_next_waiting(conn, slot, seat):
    """비워진 좌석을 대기열 1순위에게 예약 배정. 이미 끝난 시간대로는 배정하지 않음."""
    if slot_phase(slot) == "past":
        return False
    for w in conn.execute("SELECT * FROM waiting_queue WHERE slot_id = ? ORDER BY id ASC", (slot["id"],)).fetchall():
        conn.execute("DELETE FROM waiting_queue WHERE id = ?", (w["id"],))
        if conn.execute("SELECT 1 FROM seats WHERE slot_id = ? AND user_id = ? AND status != 'EMPTY'", (slot["id"], w["user_id"])).fetchone():
            continue   # 대기 중에 다른 좌석을 잡은 사람은 건너뜀 (1인 1좌석)
        conn.execute("UPDATE seats SET status = 'RESERVED', user_id = ?, user_name = ?, reserved_at = ? WHERE id = ?", (w["user_id"], w["user_name"], now_str(), seat["id"]))

        msg = f"[자동 승격] {slot['time_range']} {seat['seat_num']} 좌석이 예약 배정되었습니다."
        others = [n for n in load_notes(conn).get(seat["seat_num"], []) if n["user_id"] != w["user_id"]]
        memo = "FIFO 배정"
        if slot_phase(slot) == "active":
            msg += " [사용] 버튼으로 이용을 시작하세요."
        if others:
            msg += f" ⚠️ 이 PC에는 주의사항 {len(others)}건이 등록되어 있습니다. 이용 전에 꼭 확인하세요."
            memo += f" | 주의사항 {len(others)}건 알림 발송"
        send_notification(conn, w["user_id"], msg, f"PROMOTE_{slot['id']}_{seat['seat_num']}_{w['id']}")
        add_log(conn, slot["time_range"], seat["seat_num"], f"{w['user_id']} {w['user_name']}", "대기열 자동 승격", memo)
        return True
    return False


@app.route("/seat_action", methods=["POST"])
@login_required
def seat_action():
    """진행 중인 시간대: 사용 / 반납.  시작 전 시간대: 예약 / 예약 취소.  (주의사항 등록은 언제든 좌석 보유자가 가능)"""
    conn = get_db()
    slot_id, seat_id, action = request.form.get("slot_id", 0, type=int), request.form.get("seat_id", 0, type=int), request.form.get("action", "")
    user_id, user_name, user_role = session["user_id"], session["user_name"], session["role"]
    user_full = f"{user_id} {user_name}"

    slot = conn.execute("SELECT * FROM time_slots WHERE id = ?", (slot_id,)).fetchone()
    seat = conn.execute("SELECT * FROM seats WHERE id = ? AND slot_id = ?", (seat_id, slot_id)).fetchone()
    if not slot or not seat:
        conn.close()
        flash("존재하지 않는 좌석입니다.")
        return _go_back(slot_id)

    phase = slot_phase(slot)
    seat_num = seat["seat_num"]
    is_mine = seat["user_id"] == user_id and seat["status"] != "EMPTY"
    others_notes = [n for n in load_notes(conn).get(seat_num, []) if n["user_id"] != user_id]

    if action in ("RESERVE", "USE"):
        has_my_seat = conn.execute("SELECT 1 FROM seats WHERE slot_id = ? AND user_id = ? AND status != 'EMPTY'", (slot_id, user_id)).fetchone()
        own_reserved = is_mine and seat["status"] == "RESERVED"
        if phase == "past":
            flash("이미 종료된 시간대입니다.")
        elif action == "RESERVE" and phase == "active":
            flash("진행 중인 시간대는 예약 없이 [사용]으로 바로 이용할 수 있습니다.")
        elif action == "USE" and phase == "future":
            flash("아직 시작 전인 시간대입니다. [예약]을 이용해 주세요.")
        elif has_my_seat and not (action == "USE" and own_reserved):
            flash("1인 1좌석 제한: 이 시간대에 이미 좌석을 예약하거나 사용 중입니다.")
        elif seat["status"] != "EMPTY" and not (action == "USE" and own_reserved):
            flash("이미 다른 사람이 예약했거나 사용 중인 좌석입니다.")
        elif others_notes and request.form.get("caution_agreed") != "yes":
            flash("⚠️ 이 PC에 남겨진 주의사항을 확인하고 동의해야 합니다.")
        else:
            agree_memo = f" | 주의사항 {len(others_notes)}건 동의" if others_notes else ""
            if action == "RESERVE":
                cur = conn.execute("UPDATE seats SET status = 'RESERVED', user_id = ?, user_name = ?, reserved_at = ? WHERE id = ? AND status = 'EMPTY'",
                                   (user_id, user_name, now_str(), seat_id))
                if cur.rowcount == 0:
                    flash("방금 다른 사용자가 먼저 예약했습니다.")
                else:
                    add_log(conn, slot["time_range"], seat_num, user_full, "좌석 예약", f"예약 완료{agree_memo}")
                    if others_notes:
                        notify_note_authors(conn, seat_num, slot, user_id, user_full, "예약")
                    flash(f"[{seat_num}] 예약 완료! {slot['time_range']} 시간이 시작되면 [사용] 버튼으로 이용을 시작하세요.")
            else:
                if own_reserved:
                    cur = conn.execute("UPDATE seats SET status = 'OCCUPIED', last_user_info = ? WHERE id = ? AND status = 'RESERVED' AND user_id = ?", (user_full, seat_id, user_id))
                    how = "예약 후 사용"
                else:
                    cur = conn.execute("UPDATE seats SET status = 'OCCUPIED', user_id = ?, user_name = ?, last_user_info = ?, reserved_at = ? WHERE id = ? AND status = 'EMPTY'",
                                       (user_id, user_name, user_full, now_str(), seat_id))
                    how = "바로 사용"
                if cur.rowcount == 0:
                    flash("방금 다른 사용자가 먼저 사용을 시작했습니다.")
                else:
                    add_log(conn, slot["time_range"], seat_num, user_full, "사용 시작", f"{how}{agree_memo}")
                    if others_notes:
                        notify_note_authors(conn, seat_num, slot, user_id, user_full, "사용 시작")
                    flash(f"[{seat_num}] 사용을 시작합니다. 사용이 끝나면 [반납]을 눌러 주세요.")

    elif action == "CANCEL":
        if not (is_mine and seat["status"] == "RESERVED"):
            flash("본인이 예약한 좌석만 예약을 취소할 수 있습니다.")
        else:
            add_log(conn, slot["time_range"], seat_num, user_full, "예약 취소", "예약 취소")
            conn.execute("UPDATE seats SET status = 'EMPTY', user_id = NULL, user_name = NULL, reserved_at = NULL WHERE id = ?", (seat_id,))
            _promote_next_waiting(conn, slot, conn.execute("SELECT * FROM seats WHERE id = ?", (seat_id,)).fetchone())
            flash(f"[{seat_num}] 예약을 취소했습니다.")

    elif action == "RETURN":
        if not (is_mine and seat["status"] == "OCCUPIED"):
            flash("본인이 사용 중인 좌석만 반납할 수 있습니다.")
        else:
            memo = "반납"
            if request.form.get("caution_msg", "").strip():
                ok, msg = add_note(conn, seat_num, user_id, user_name, request.form.get("caution_path", ""), request.form.get("caution_msg", ""), slot["time_range"])
                if not ok:
                    conn.close()
                    flash(msg)
                    return _go_back(slot_id)
                memo = "반납 + 주의사항 등록"
            add_log(conn, slot["time_range"], seat_num, user_full, "반납", memo)
            conn.execute("UPDATE seats SET status = 'EMPTY', user_id = NULL, user_name = NULL, reserved_at = NULL WHERE id = ?", (seat_id,))
            _promote_next_waiting(conn, slot, conn.execute("SELECT * FROM seats WHERE id = ?", (seat_id,)).fetchone())
            flash(f"[{seat_num}] 반납이 완료되었습니다.")

    elif action == "ADD_NOTE":
        if not is_mine:
            flash("이 좌석을 예약했거나 사용 중인 사람만 주의사항을 남길 수 있습니다.")
        else:
            ok, msg = add_note(conn, seat_num, user_id, user_name, request.form.get("caution_path", ""), request.form.get("caution_msg", ""), slot["time_range"])
            flash(msg)

    elif action == "FORCE_RELEASE":
        if user_role != "ADMIN":
            flash("관리자만 사용할 수 있는 기능입니다.")
        elif seat["status"] == "EMPTY":
            flash("이미 비어 있는 좌석입니다.")
        else:
            prev_id, prev_full = seat["user_id"], f"{seat['user_id']} {seat['user_name']}"
            add_log(conn, slot["time_range"], seat_num, prev_full, "강제 회수",
                    f"[관리자 {session['user_name']}] 회수 전 상태: {'사용중' if seat['status'] == 'OCCUPIED' else '예약중'}")
            send_notification(conn, prev_id, f"[관리자 회수] {slot['time_range']} {seat_num} 좌석이 관리자에 의해 회수되었습니다.",
                              f"ADMIN_RELEASE_{slot_id}_{seat_num}_{now_dt():%H%M%S}")
            conn.execute("UPDATE seats SET status = 'EMPTY', user_id = NULL, user_name = NULL, reserved_at = NULL WHERE id = ?", (seat_id,))
            _promote_next_waiting(conn, slot, conn.execute("SELECT * FROM seats WHERE id = ?", (seat_id,)).fetchone())
            flash(f"[{seat_num}] 좌석을 회수했습니다.")

    conn.commit()
    conn.close()
    return _go_back(slot_id)


@app.route("/queue_register", methods=["POST"])
@login_required
def queue_register():
    conn = get_db()
    slot_id, user_id, user_name = request.form.get("slot_id", 0, type=int), session["user_id"], session["user_name"]
    slot = conn.execute("SELECT * FROM time_slots WHERE id = ?", (slot_id,)).fetchone()
    if not slot or slot_phase(slot) == "past":
        flash("이미 종료된 시간대입니다.")
    elif conn.execute("SELECT 1 FROM seats WHERE slot_id = ? AND user_id = ? AND status != 'EMPTY'", (slot_id, user_id)).fetchone():
        flash("이미 이 시간대에 좌석을 예약하거나 사용 중이라 대기 등록이 필요 없습니다.")
    elif conn.execute("SELECT id FROM waiting_queue WHERE slot_id = ? AND user_id = ?", (slot_id, user_id)).fetchone() is None:
        conn.execute("INSERT INTO waiting_queue (slot_id, user_id, user_name, created_at) VALUES (?, ?, ?, ?)", (slot_id, user_id, user_name, now_str()))
        conn.commit()
        flash("대기열에 등록되었습니다. 자리가 나면 자동으로 예약 배정됩니다.")
    else:
        flash("이미 대기열에 등록되어 있습니다.")
    conn.close()
    return redirect(url_for("index", slot_id=slot_id))


# ───────────────────────── 관리자: 이용 상태 확인/제어 + 로그 열람 ─────────────────────────
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    search_keyword, action_filter = request.args.get("keyword", "").strip(), request.args.get("action_filter", "ALL")
    start_date, end_date = request.args.get("start_date", "").strip(), request.args.get("end_date", "").strip()

    slots = conn.execute("SELECT * FROM time_slots ORDER BY id").fetchall()
    current_slot = next((s for s in slots if s["id"] == request.args.get("slot_id", type=int)), None) or default_slot(slots)

    notes_by_seat = load_notes(conn)
    seats = [dict(s) for s in conn.execute("SELECT * FROM seats WHERE slot_id = ? ORDER BY id ASC", (current_slot["id"],)).fetchall()]
    for s in seats:
        s["notes_total"] = len(notes_by_seat.get(s["seat_num"], []))
    counts = {
        "occupied": sum(1 for s in seats if s["status"] == "OCCUPIED"),
        "reserved": sum(1 for s in seats if s["status"] == "RESERVED"),
        "empty": sum(1 for s in seats if s["status"] == "EMPTY"),
        "caution": sum(1 for s in seats if s["notes_total"]),
    }
    queue_count = conn.execute("SELECT COUNT(*) FROM waiting_queue WHERE slot_id = ?", (current_slot["id"],)).fetchone()[0]
    all_notes = conn.execute("SELECT * FROM seat_cautions ORDER BY seat_num ASC, id ASC").fetchall()

    log_query = "SELECT * FROM audit_logs WHERE (user_info LIKE ? ESCAPE '\\' OR seat_num LIKE ? ESCAPE '\\' OR memo LIKE ? ESCAPE '\\')"
    log_params = [like_pattern(search_keyword)] * 3
    if action_filter != "ALL":
        log_query += " AND action_type LIKE ? ESCAPE '\\'"
        log_params.append(like_pattern(action_filter))
    if start_date:
        log_query += " AND timestamp >= ?"
        log_params.append(f"{start_date} 00:00:00")
    if end_date:
        log_query += " AND timestamp <= ?"
        log_params.append(f"{end_date} 23:59:59")
    logs = conn.execute(log_query + " ORDER BY id DESC LIMIT 100", tuple(log_params)).fetchall()
    conn.close()

    return render_template("admin.html", slots=slots, current_slot=current_slot, phase=slot_phase(current_slot), seats=seats, counts=counts,
                           queue_count=queue_count, all_notes=all_notes, logs=logs, search_keyword=search_keyword, action_filter=action_filter,
                           start_date=start_date, end_date=end_date, clock=clock_info(),
                           public_url=(PUBLIC_TUNNEL_URL.rstrip("/") if PUBLIC_TUNNEL_URL else request.host_url.rstrip("/")))


@app.route("/admin/set_clock", methods=["POST"])
@admin_required
def admin_set_clock():
    """시연용 시계: 앱이 쓰는 현재 시각을 원하는 시각으로 맞춤(이후 실제와 같은 속도로 흐름). 진행중/예정 구분을 시연·점검할 때 사용."""
    global _clock_offset
    slot_id = request.form.get("slot_id", type=int)
    if request.form.get("mode") == "RESET":
        _clock_offset = timedelta(0)
        flash("시연용 시계를 해제했습니다. 실제 시각으로 돌아왔습니다.")
    else:
        try:
            hh, mm = (int(x) for x in request.form.get("time", "").split(":"))
            real = datetime.now()
            _clock_offset = real.replace(hour=hh, minute=mm, second=0, microsecond=0) - real
            flash(f"시연용 시계를 {hh:02d}:{mm:02d}(으)로 맞췄습니다. 이후 실제 시간과 같은 속도로 흐릅니다.")
        except ValueError:
            flash("시각 형식이 올바르지 않습니다. 예: 10:05")
    return redirect(url_for("admin_dashboard", slot_id=slot_id))


init_db()   # 서버가 켜질 때 DB를 자동으로 준비 (flask run 으로 실행해도 동일)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
