"""
auth.py — login layer for the study app.

    * Passkeys (WebAuthn, py_webauthn) as the primary login.
    * TOTP (authenticator app, pyotp) as the mandatory fallback.
    * Invite-only: no public sign-up. An admin creates an invite link; the
      invitee enrolls TOTP (required) and a passkey (recommended) on first visit.
    * Sessions are Flask signed cookies. SECRET_KEY must come from .env.
    * Identity is always taken from the session on the server. Clients never
      supply a user id.

Tables (added to the same SQLite file as progress):
    accounts(id, username, is_admin, status, totp_secret, totp_last_step,
             user_id -> users.id, created, last_login)
    passkeys(id, account_id, credential_id, public_key, sign_count,
             transports, label, created, last_used)
    invites(id, account_id, token_hash, expires, used)
    login_attempts(key, ts)
"""
import os, json, time, secrets, hashlib, functools, sqlite3, base64, hmac, smtplib, threading, sys
from datetime import datetime, timezone, date
from email.message import EmailMessage

import pyotp, segno
from flask import (Blueprint, request, session, g, jsonify, redirect, url_for,
                   render_template_string, abort, current_app)
from webauthn import (generate_registration_options, verify_registration_response,
                      generate_authentication_options, verify_authentication_response,
                      options_to_json)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                                      UserVerificationRequirement, PublicKeyCredentialDescriptor)
from webauthn.helpers.exceptions import InvalidRegistrationResponse, InvalidAuthenticationResponse

bp = Blueprint("auth", __name__)

APP_NAME = "StudyBuddy"
INVITE_TTL_H = 72          # invite links expire after this many hours
MAX_FAILS_USER = 5         # failed TOTP logins per username per window
MAX_FAILS_IP = 15          # failed TOTP logins per IP per window
FAIL_WINDOW_S = 15 * 60

# db() is injected by app.py so both modules share one connection factory.
db = None

def init_auth_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
        is_admin INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
        totp_secret TEXT, totp_last_step INTEGER DEFAULT 0,
        user_id INTEGER, created REAL, last_login REAL);
    CREATE TABLE IF NOT EXISTS passkeys(
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
        credential_id TEXT UNIQUE NOT NULL, public_key BLOB NOT NULL,
        sign_count INTEGER DEFAULT 0, transports TEXT, label TEXT,
        created REAL, last_used REAL);
    CREATE TABLE IF NOT EXISTS invites(
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
        token_hash TEXT UNIQUE NOT NULL, expires REAL, used INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS login_attempts(key TEXT, ts REAL);
    CREATE INDEX IF NOT EXISTS ix_attempts ON login_attempts(key, ts);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    """)
    con.commit(); con.close()

def get_setting(k, default=None):
    con = db(); r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone(); con.close()
    return r["v"] if r else default

def set_setting(k, v):
    con = db()
    if v is None: con.execute("DELETE FROM settings WHERE k=?", (k,))
    else: con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))
    con.commit(); con.close()

# ---------------------------------------------------------------------------
# helpers
def now(): return time.time()

def rp_id():
    v = os.environ.get("RP_ID")
    return v or request.host.split(":")[0]

def origin():
    v = os.environ.get("ORIGIN")
    if v: return v
    host = request.host
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    return f"{scheme}://{host}"

def client_ip():
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"

def get_account(account_id):
    con = db(); r = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone(); con.close()
    return r

def current_account():
    if "account" not in g:
        aid = session.get("aid")
        g.account = get_account(aid) if aid else None
        if g.account is not None and g.account["status"] != "active":
            g.account = None
    return g.account

def start_session(acc):
    session.clear()
    session.permanent = True
    session["aid"] = acc["id"]
    con = db(); con.execute("UPDATE accounts SET last_login=? WHERE id=?", (now(), acc["id"])); con.commit(); con.close()

def wants_json():
    return request.path.startswith("/api/") or request.path.startswith("/auth/") \
        or request.headers.get("Content-Type", "").startswith("application/json")

def login_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        acc = current_account()
        if acc is None:
            if wants_json(): return jsonify({"error": "login required"}), 401
            return redirect(url_for("auth.login_page"))
        g.user_id = acc["user_id"]
        return f(*a, **k)
    return w

def admin_required(f):
    @functools.wraps(f)
    @login_required
    def w(*a, **k):
        if not g.account["is_admin"]:
            if wants_json(): return jsonify({"error": "admin only"}), 403
            abort(403)
        return f(*a, **k)
    return w

def json_body():
    d = request.get_json(silent=True)
    if not isinstance(d, dict): abort(400)
    return d

# --- brute-force limiter -----------------------------------------------------
def fails(key):
    con = db()
    con.execute("DELETE FROM login_attempts WHERE ts < ?", (now() - FAIL_WINDOW_S,))
    n = con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
    con.commit(); con.close(); return n

THRESHOLDS = {"u:": MAX_FAILS_USER, "ip:": MAX_FAILS_IP, "inv:": MAX_FAILS_USER, "code:": MAX_FAILS_USER}
KIND = {"u:": "TOTP login for account", "ip:": "TOTP logins from IP", "inv:": "invite-link enrolment from IP",
        "code:": "invite-code sign-up from IP"}

def record_fail(*keys):
    """Log a failed attempt; when a key crosses its lockout threshold, email the admin."""
    con = db()
    for k in keys: con.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", (k, now()))
    con.commit()
    tripped = []
    for k in keys:
        pre = next((p for p in THRESHOLDS if k.startswith(p)), None)
        if pre is None: continue
        n = con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>?", (k, now() - FAIL_WINDOW_S)).fetchone()[0]
        if n == THRESHOLDS[pre]: tripped.append((k, pre, n))
    con.close()
    for k, pre, n in tripped:
        who = k[len(pre):]
        alert(f"lockout:{k}",
              f"StudyBuddy lockout: {KIND[pre]} {who}",
              f"{n} failed attempts in {FAIL_WINDOW_S // 60} minutes — {KIND[pre]} {who}.\n"
              f"Locked out for {FAIL_WINDOW_S // 60} minutes.\n\n"
              f"When: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
              f"Client IP: {client_ip()}\nPath: {request.path}\n"
              f"User-Agent: {request.headers.get('User-Agent', '?')[:200]}\n\n"
              f"If this was you, wait 15 minutes or sign in with a passkey. "
              f"To re-invite yourself from the server: python manage.py invite <name> --admin")

# --- email alerts -----------------------------------------------------------------
ALERT_THROTTLE_S = 3600     # at most one email per subject key per hour

def alert(key, subject, body):
    """Send an alert email to ALERT_EMAIL (throttled per key). Never raises."""
    try:
        last = float(get_setting("alert:" + key, 0) or 0)
        if now() - last < ALERT_THROTTLE_S: return False
        set_setting("alert:" + key, now())
        return send_email(subject, body)
    except Exception as e:
        print("alert failed:", e, file=sys.stderr); return False

def send_email(subject, body, block=False):
    to = os.environ.get("ALERT_EMAIL"); host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER"); pw = os.environ.get("SMTP_PASS")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not (to and host and user and pw):
        print("email not configured (ALERT_EMAIL/SMTP_*); would have sent:", subject, file=sys.stderr); return False
    msg = EmailMessage(); msg["From"] = user; msg["To"] = to; msg["Subject"] = subject; msg.set_content(body)
    def go():
        try:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo(); s.starttls(); s.login(user, pw); s.send_message(msg)
        except Exception as e:
            print("email send failed:", e, file=sys.stderr)
            if block: raise
    if block: go()
    else: threading.Thread(target=go, daemon=True).start()
    return True

def clear_fails(*keys):
    con = db()
    for k in keys: con.execute("DELETE FROM login_attempts WHERE key=?", (k,))
    con.commit(); con.close()

# --- invites ------------------------------------------------------------------
def create_invite(username, is_admin=False):
    """Create (or re-invite) an account and return the raw invite token.
    Links the account to an existing study profile of the same name so
    progress carries over."""
    username = username.strip()
    if not username or len(username) > 40: raise ValueError("bad username")
    con = db()
    acc = con.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    if acc is None:
        prof = con.execute("SELECT id FROM users WHERE name=? COLLATE NOCASE", (username,)).fetchone()
        if prof is None:
            prof_id = con.execute("INSERT INTO users(name) VALUES(?)", (username,)).lastrowid
        else:
            prof_id = prof["id"]
        cur = con.execute("""INSERT INTO accounts(username,is_admin,status,totp_secret,user_id,created)
                             VALUES(?,?,'pending',?,?,?)""",
                          (username, int(is_admin), pyotp.random_base32(), prof_id, now()))
        aid = cur.lastrowid
    else:
        aid = acc["id"]
        # Re-invite: fresh TOTP secret, back to pending, passkeys wiped.
        con.execute("UPDATE accounts SET status='pending', totp_secret=?, totp_last_step=0, is_admin=MAX(is_admin,?) WHERE id=?",
                    (pyotp.random_base32(), int(is_admin), aid))
        con.execute("DELETE FROM passkeys WHERE account_id=?", (aid,))
        con.execute("UPDATE invites SET used=1 WHERE account_id=?", (aid,))
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO invites(account_id,token_hash,expires) VALUES(?,?,?)",
                (aid, hashlib.sha256(token.encode()).hexdigest(), now() + INVITE_TTL_H * 3600))
    con.commit(); con.close()
    return token

def invite_url(token):
    base = os.environ.get("ORIGIN") or "http://127.0.0.1:5000"
    return f"{base}/enroll/{token}"

def lookup_invite(token):
    con = db()
    r = con.execute("""SELECT i.*, a.username, a.totp_secret, a.status FROM invites i
                       JOIN accounts a ON a.id=i.account_id
                       WHERE i.token_hash=? AND i.used=0 AND i.expires>?""",
                    (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
    con.close(); return r

# --- self-service sign-up with a shared invite code ----------------------------------
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L look-alikes

def new_code():
    while True:
        c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if all(c[i] != c[i + 1] for i in range(7)): return c[:4] + "-" + c[4:]

def norm_code(s):
    return "".join(ch for ch in str(s).upper() if ch.isalnum())

def code_status():
    """-> (code, expires_iso, state) where state is 'off' | 'expired' | 'active'."""
    code = get_setting("invite_code"); exp = get_setting("invite_code_expires")
    if not code: return None, exp, "off"
    if exp and date.today().isoformat() >= exp: return code, exp, "expired"
    return code, exp, "active"

def name_taken(name):
    con = db()
    a = con.execute("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone()
    u = con.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    con.close(); return bool(a or u)

@bp.get("/api/signup/status")
def signup_status():
    _, exp, state = code_status()
    return jsonify({"open": state == "active", "state": state})

@bp.post("/auth/signup")
def signup():
    d = json_body(); given = norm_code(d.get("code") or ""); name = (d.get("username") or "").strip()
    ip = client_ip(); k = "code:" + ip
    if fails(k) >= MAX_FAILS_USER: return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429
    code, exp, state = code_status()
    if state == "off": return jsonify({"error": "Self sign-up is off. Ask an admin for an invite link."}), 403
    if state == "expired": return jsonify({"error": "That code has expired — ask a club member for the current one."}), 403
    if not given or not hmac.compare_digest(given, norm_code(code)):
        record_fail(k); return jsonify({"error": "Wrong code."}), 401
    if not name or len(name) > 40: return jsonify({"error": "Pick a name, 1–40 characters."}), 400
    if name_taken(name): return jsonify({"error": "That name is taken — pick another."}), 409
    token = create_invite(name)
    return jsonify({"ok": True, "url": f"/enroll/{token}"})

@bp.post("/admin/code")
@admin_required
def admin_code():
    d = json_body(); action = d.get("action")
    if action == "off":
        set_setting("invite_code", None); set_setting("invite_code_expires", None)
    else:
        code = norm_code(d.get("code") or "") if action == "set" else norm_code(new_code())
        if len(code) < 8: return jsonify({"error": "Code must be at least 8 letters/digits."}), 400
        exp = (d.get("expires") or "").strip()
        if exp:
            try: date.fromisoformat(exp)
            except ValueError: return jsonify({"error": "Expiry must be YYYY-MM-DD."}), 400
        set_setting("invite_code", code[:4] + "-" + code[4:] if len(code) == 8 else code)
        set_setting("invite_code_expires", exp or None)
    code, exp, state = code_status()
    return jsonify({"ok": True, "code": code, "expires": exp, "state": state})

# --- TOTP ---------------------------------------------------------------------
def totp_ok(acc, code):
    """Verify a TOTP code and refuse replay of the same 30-second step."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6: return False
    t = pyotp.TOTP(acc["totp_secret"])
    step = int(now() // 30)
    for s in (step, step - 1, step + 1):
        if s <= acc["totp_last_step"]: continue
        if t.at(s * 30) == code:
            con = db(); con.execute("UPDATE accounts SET totp_last_step=? WHERE id=?", (s, acc["id"])); con.commit(); con.close()
            return True
    return False

def totp_qr_svg(acc):
    uri = pyotp.TOTP(acc["totp_secret"]).provisioning_uri(name=acc["username"], issuer_name=APP_NAME)
    return segno.make(uri, error="m").svg_data_uri(scale=5, dark="#0b1014", light="#ffffff")

# ---------------------------------------------------------------------------
# JSON auth endpoints
@bp.post("/auth/totp/login")
def totp_login():
    d = json_body()
    username = (d.get("username") or "").strip(); code = d.get("code") or ""
    ip = client_ip(); k_user = "u:" + username.lower(); k_ip = "ip:" + ip
    if fails(k_user) >= MAX_FAILS_USER or fails(k_ip) >= MAX_FAILS_IP:
        return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429
    con = db(); acc = con.execute("SELECT * FROM accounts WHERE username=? AND status='active'", (username,)).fetchone(); con.close()
    if acc is None or not totp_ok(acc, code):
        record_fail(k_user, k_ip)
        return jsonify({"error": "Wrong name or code."}), 401
    clear_fails(k_user)
    start_session(acc)
    return jsonify({"ok": True})

@bp.post("/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@bp.post("/auth/passkey/login/options")
def passkey_login_options():
    opts = generate_authentication_options(rp_id=rp_id(), user_verification=UserVerificationRequirement.PREFERRED)
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return current_app.response_class(options_to_json(opts), mimetype="application/json")

@bp.post("/auth/passkey/login/verify")
def passkey_login_verify():
    cred = json_body(); chal = session.pop("pk_challenge", None)
    if not chal: return jsonify({"error": "no challenge"}), 400
    con = db()
    pk = con.execute("""SELECT p.*, a.status FROM passkeys p JOIN accounts a ON a.id=p.account_id
                        WHERE p.credential_id=?""", (cred.get("id"),)).fetchone()
    con.close()
    if pk is None or pk["status"] != "active":
        return jsonify({"error": "Unknown passkey."}), 401
    try:
        v = verify_authentication_response(
            credential=cred, expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=rp_id(), expected_origin=origin(),
            credential_public_key=pk["public_key"], credential_current_sign_count=pk["sign_count"])
    except InvalidAuthenticationResponse as e:
        return jsonify({"error": f"Passkey rejected: {e}"}), 401
    con = db()
    con.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (v.new_sign_count, now(), pk["id"]))
    con.commit(); con.close()
    start_session(get_account(pk["account_id"]))
    return jsonify({"ok": True})

@bp.post("/auth/passkey/register/options")
@login_required
def passkey_register_options():
    acc = g.account
    con = db(); existing = con.execute("SELECT credential_id FROM passkeys WHERE account_id=?", (acc["id"],)).fetchall(); con.close()
    opts = generate_registration_options(
        rp_id=rp_id(), rp_name=APP_NAME,
        user_id=str(acc["id"]).encode(), user_name=acc["username"], user_display_name=acc["username"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in existing])
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return current_app.response_class(options_to_json(opts), mimetype="application/json")

@bp.post("/auth/passkey/register/verify")
@login_required
def passkey_register_verify():
    d = json_body(); cred = d.get("credential"); label = (d.get("label") or "Passkey").strip()[:40]
    chal = session.pop("pk_challenge", None)
    if not chal or not isinstance(cred, dict): return jsonify({"error": "no challenge"}), 400
    try:
        v = verify_registration_response(credential=cred, expected_challenge=base64url_to_bytes(chal),
                                         expected_rp_id=rp_id(), expected_origin=origin())
    except InvalidRegistrationResponse as e:
        return jsonify({"error": f"Registration rejected: {e}"}), 400
    transports = json.dumps((cred.get("response") or {}).get("transports") or [])
    con = db()
    con.execute("""INSERT INTO passkeys(account_id,credential_id,public_key,sign_count,transports,label,created)
                   VALUES(?,?,?,?,?,?,?)""",
                (g.account["id"], bytes_to_base64url(v.credential_id), v.credential_public_key,
                 v.sign_count, transports, label, now()))
    con.commit(); con.close()
    return jsonify({"ok": True})

@bp.post("/auth/passkey/delete")
@login_required
def passkey_delete():
    pid = int(json_body().get("id", 0))
    con = db(); con.execute("DELETE FROM passkeys WHERE id=? AND account_id=?", (pid, g.account["id"])); con.commit(); con.close()
    return jsonify({"ok": True})

@bp.get("/api/me")
@login_required
def api_me():
    con = db(); n = con.execute("SELECT COUNT(*) FROM passkeys WHERE account_id=?", (g.account["id"],)).fetchone()[0]; con.close()
    return jsonify({"id": g.user_id, "name": g.account["username"],
                    "is_admin": bool(g.account["is_admin"]), "passkeys": n})

# --- enrollment -----------------------------------------------------------------
@bp.get("/enroll/<token>")
def enroll_page(token):
    inv = lookup_invite(token)
    if inv is None:
        return render_template_string(BASE, title="Invite invalid", body=
            "<div class='card'><h2>This invite link is invalid or has expired.</h2>"
            "<p class='sub'>Ask the admin for a new one.</p></div>"), 410
    acc = get_account(inv["account_id"])
    body = render_template_string(ENROLL_BODY, username=acc["username"], qr=totp_qr_svg(acc),
                                  secret=acc["totp_secret"], token=token)
    return render_template_string(BASE, title="Set up your login", body=body)

@bp.post("/auth/enroll/<token>/totp")
def enroll_totp(token):
    inv = lookup_invite(token)
    if inv is None: return jsonify({"error": "Invite invalid or expired."}), 410
    ip = client_ip(); k = "inv:" + ip
    if fails(k) >= MAX_FAILS_USER: return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429
    acc = get_account(inv["account_id"])
    if not totp_ok(acc, json_body().get("code")):
        record_fail(k); return jsonify({"error": "That code didn't match. Check the time on your phone and try the next one."}), 401
    con = db()
    con.execute("UPDATE accounts SET status='active' WHERE id=?", (acc["id"],))
    con.execute("UPDATE invites SET used=1 WHERE id=?", (inv["id"],))
    con.commit(); con.close()
    start_session(get_account(acc["id"]))
    return jsonify({"ok": True})

# --- pages ---------------------------------------------------------------------
@bp.get("/login")
def login_page():
    if current_account(): return redirect("/")
    return render_template_string(BASE, title="Sign in", body=LOGIN_BODY)

@bp.get("/settings")
@login_required
def settings_page():
    con = db(); pks = con.execute("SELECT * FROM passkeys WHERE account_id=? ORDER BY created", (g.account["id"],)).fetchall(); con.close()
    body = render_template_string(SETTINGS_BODY, acc=g.account, passkeys=pks, fmt=fmt_ts)
    return render_template_string(BASE, title="Security settings", body=body)

@bp.get("/admin")
@admin_required
def admin_page():
    con = db()
    rows = con.execute("""SELECT a.*, (SELECT COUNT(*) FROM passkeys p WHERE p.account_id=a.id) AS npk
                          FROM accounts a ORDER BY username COLLATE NOCASE""").fetchall()
    con.close()
    code, exp, state = code_status()
    body = render_template_string(ADMIN_BODY, accounts=rows, me=g.account, fmt=fmt_ts,
                                  code=code, code_exp=exp, code_state=state,
                                  email_on=bool(os.environ.get("ALERT_EMAIL") and os.environ.get("SMTP_PASS")))
    return render_template_string(BASE, title="Admin", body=body)

@bp.post("/admin/invite")
@admin_required
def admin_invite():
    d = json_body()
    try: token = create_invite(d.get("username", ""), bool(d.get("is_admin")))
    except ValueError: return jsonify({"error": "Username must be 1–40 characters."}), 400
    return jsonify({"ok": True, "url": invite_url(token), "expires_h": INVITE_TTL_H})

@bp.post("/admin/delete")
@admin_required
def admin_delete():
    aid = int(json_body().get("id", 0))
    if aid == g.account["id"]: return jsonify({"error": "You can't delete yourself."}), 400
    con = db()
    con.execute("DELETE FROM passkeys WHERE account_id=?", (aid,))
    con.execute("DELETE FROM invites WHERE account_id=?", (aid,))
    con.execute("DELETE FROM accounts WHERE id=?", (aid,))   # study profile + progress kept
    con.commit(); con.close()
    return jsonify({"ok": True})

def fmt_ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "—"

# ---------------------------------------------------------------------------
# Templates. Same visual language as the trainer. Jinja autoescape is on.
BASE = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} · StudyBuddy</title>
<style>
:root{--bg:#0b1014;--panel:#121a20;--panel2:#16212a;--ink:#e9f1f4;--muted:#7e94a0;--line:#243038;--accent:#ff7a18;--accent2:#19c2c2;--good:#36c46e;--again:#ef5350}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Sora',system-ui,sans-serif;background:radial-gradient(1200px 600px at 80% -10%,#15323a 0,transparent 55%),var(--bg);color:var(--ink);min-height:100vh;padding:24px 16px 60px}
.wrap{max-width:560px;margin:0 auto}h1{font-size:26px;font-weight:800;margin-bottom:4px}h1 span{color:var(--accent)}h2{font-size:18px;margin-bottom:10px}
.sub{color:var(--muted);font-family:ui-monospace,monospace;font-size:12px;letter-spacing:1px;text-transform:uppercase}
.card{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:20px;padding:28px 26px;margin-top:18px}
p{line-height:1.5;margin:8px 0;color:var(--muted)}p b{color:var(--ink)}
input{width:100%;font-family:ui-monospace,monospace;font-size:17px;padding:13px;border-radius:12px;border:1px solid var(--line);background:#0c1318;color:var(--ink);margin-top:8px}
input:focus{outline:none;border-color:var(--accent2)}
.btn{display:inline-block;width:100%;margin-top:12px;padding:14px;border-radius:12px;border:none;background:var(--accent2);color:#04201f;font-weight:700;font-size:15px;cursor:pointer;text-align:center;text-decoration:none}
.btn.alt{background:var(--panel2);color:var(--ink);border:1px solid var(--line)}.btn.warn{background:var(--again);color:#1a0606}
.btn:disabled{opacity:.5;cursor:default}
.msg{margin-top:12px;font-family:ui-monospace,monospace;font-size:13px;min-height:18px}.msg.ok{color:var(--good)}.msg.no{color:var(--again)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}
.row small{color:var(--muted);font-family:ui-monospace,monospace;font-size:11px}
.pill{font-family:ui-monospace,monospace;font-size:10px;padding:2px 8px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);color:var(--muted)}
.link{color:var(--accent2);text-decoration:none}.hr{border:0;border-top:1px solid var(--line);margin:18px 0}
code{font-family:ui-monospace,monospace;background:#0c1318;padding:2px 6px;border-radius:6px;word-break:break-all}
.qr{display:block;margin:12px auto;width:220px;height:220px;border-radius:12px;background:#fff}
label.chk{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:13px;margin-top:10px}label.chk input{width:auto;margin:0}
</style></head><body><div class="wrap">
<script>
const b64u={enc:b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''),
  dec:s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0))};
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(async r=>{const j=await r.json().catch(()=>({}));j._status=r.status;return j;});
function say(id,txt,ok){const m=document.getElementById(id);m.textContent=txt;m.className='msg '+(ok?'ok':'no');}
const hasPasskeys=()=>!!(window.PublicKeyCredential&&navigator.credentials&&navigator.credentials.create);
async function passkeyLogin(){
  try{
    const o=await post('/auth/passkey/login/options');
    o.challenge=b64u.dec(o.challenge);(o.allowCredentials||[]).forEach(c=>c.id=b64u.dec(c.id));
    const cred=await navigator.credentials.get({publicKey:o});
    const r=cred.response;
    const j=await post('/auth/passkey/login/verify',{id:cred.id,rawId:b64u.enc(cred.rawId),type:cred.type,
      response:{clientDataJSON:b64u.enc(r.clientDataJSON),authenticatorData:b64u.enc(r.authenticatorData),
        signature:b64u.enc(r.signature),userHandle:r.userHandle?b64u.enc(r.userHandle):null},
      clientExtensionResults:cred.getClientExtensionResults?cred.getClientExtensionResults():{}});
    if(j.ok) location.href='/'; else say('msg',j.error||'Passkey sign-in failed.',false);
  }catch(e){say('msg','Passkey sign-in cancelled or unavailable ('+e.name+').',false);}
}
async function passkeyRegister(label,msgId){
  try{
    const o=await post('/auth/passkey/register/options');
    if(o._status===401){location.href='/login';return false;}
    o.challenge=b64u.dec(o.challenge);o.user.id=b64u.dec(o.user.id);(o.excludeCredentials||[]).forEach(c=>c.id=b64u.dec(c.id));
    const cred=await navigator.credentials.create({publicKey:o});
    const r=cred.response;
    const j=await post('/auth/passkey/register/verify',{label,credential:{id:cred.id,rawId:b64u.enc(cred.rawId),type:cred.type,
      response:{clientDataJSON:b64u.enc(r.clientDataJSON),attestationObject:b64u.enc(r.attestationObject),
        transports:r.getTransports?r.getTransports():[]},
      clientExtensionResults:cred.getClientExtensionResults?cred.getClientExtensionResults():{}}});
    if(j.ok){say(msgId,'✓ Passkey saved.',true);return true;}
    say(msgId,j.error||'Could not save passkey.',false);return false;
  }catch(e){say(msgId,'Passkey setup cancelled or unavailable ('+e.name+').',false);return false;}
}
async function logout(){await post('/auth/logout');location.href='/login';}
</script>
<h1>Study<span>Buddy</span></h1><div class="sub">{{ title }}</div>
{{ body|safe }}
</div>
</body></html>"""

LOGIN_BODY = r"""
<div class="card">
  <h2>Sign in with a passkey</h2>
  <p>Face, fingerprint, or PIN on a device where you've saved one.</p>
  <button class="btn" id="pkBtn" onclick="passkeyLogin()">Use passkey</button>
  <div class="msg" id="msg"></div>
  <hr class="hr">
  <h2>…or an authenticator code</h2>
  <input id="u" placeholder="name" autocomplete="username" autocapitalize="off">
  <input id="c" placeholder="6-digit code" inputmode="numeric" autocomplete="one-time-code" onkeydown="if(event.key==='Enter')totpLogin()">
  <button class="btn alt" onclick="totpLogin()">Sign in</button>
</div>
<div class="card" id="signup" style="display:none">
  <h2>New here? Have an invite code?</h2>
  <p>It's on the card or from a club member. Pick the name you'll study under.</p>
  <input id="ic" placeholder="invite code, e.g. AB2C-DEF3" autocapitalize="characters" autocomplete="off" style="text-transform:uppercase">
  <input id="in" placeholder="your name" autocomplete="off" maxlength="40" onkeydown="if(event.key==='Enter')signup()">
  <button class="btn alt" onclick="signup()">Create my login</button>
  <div class="msg" id="msg2"></div>
</div>
<script>
if(!hasPasskeys()){document.getElementById('pkBtn').disabled=true;say('msg','This browser has no passkey support — use a code.',false);}
fetch('/api/signup/status').then(r=>r.json()).then(j=>{if(j.open)document.getElementById('signup').style.display='';}).catch(()=>{});
async function totpLogin(){
  const j=await post('/auth/totp/login',{username:document.getElementById('u').value,code:document.getElementById('c').value});
  if(j.ok) location.href='/'; else say('msg',j.error||'Sign-in failed.',false);
}
async function signup(){
  const j=await post('/auth/signup',{code:document.getElementById('ic').value,username:document.getElementById('in').value});
  if(j.ok) location.href=j.url; else say('msg2',j.error||'Sign-up failed.',false);
}
</script>"""

ENROLL_BODY = r"""
<div class="card" id="step1">
  <h2>Hi {{ username }} — step 1: authenticator app</h2>
  <p>Open Google Authenticator, Microsoft Authenticator, Authy, 1Password, or similar and scan this:</p>
  <img class="qr" src="{{ qr }}" alt="TOTP QR code">
  <p>Can't scan? Enter this key manually: <code>{{ secret }}</code></p>
  <p>Then type the 6-digit code it shows:</p>
  <input id="c" placeholder="6-digit code" inputmode="numeric" autocomplete="one-time-code" onkeydown="if(event.key==='Enter')confirmTotp()">
  <button class="btn" onclick="confirmTotp()">Confirm</button>
  <div class="msg" id="msg1"></div>
</div>
<div class="card" id="step2" style="display:none">
  <h2>Step 2: add a passkey (recommended)</h2>
  <p>A passkey lets this device sign you in with face/fingerprint/PIN — no code to type. The authenticator app stays as your backup.</p>
  <input id="label" placeholder="label, e.g. Tim's laptop" maxlength="40">
  <button class="btn" id="pkBtn" onclick="doRegister()">Create passkey on this device</button>
  <a class="btn alt" href="/">Skip for now → open the trainer</a>
  <div class="msg" id="msg2"></div>
</div>
<script>
async function confirmTotp(){
  const j=await post('/auth/enroll/{{ token }}/totp',{code:document.getElementById('c').value});
  if(j.ok){document.getElementById('step1').style.display='none';document.getElementById('step2').style.display='';
    if(!hasPasskeys()){document.getElementById('pkBtn').disabled=true;say('msg2','No passkey support in this browser — skip for now.',false);}}
  else say('msg1',j.error||'Failed.',false);
}
async function doRegister(){
  const ok=await passkeyRegister(document.getElementById('label').value||'Passkey','msg2');
  if(ok) setTimeout(()=>location.href='/',700);
}
</script>"""

SETTINGS_BODY = r"""
<div class="card">
  <h2>{{ acc.username }}{% if acc.is_admin %} <span class="pill">admin</span>{% endif %}</h2>
  <p>Authenticator app: <b>enrolled</b>. To move it to a new phone, ask an admin to re-invite you.</p>
  <a class="link" href="/">← back to the trainer</a>{% if acc.is_admin %} · <a class="link" href="/admin">admin</a>{% endif %}
</div>
<div class="card">
  <h2>Passkeys</h2>
  {% for p in passkeys %}
  <div class="row"><div>{{ p.label }}<br><small>added {{ fmt(p.created) }} · last used {{ fmt(p.last_used) }}</small></div>
    <button class="btn warn" style="width:auto;margin:0;padding:8px 12px;font-size:12px" onclick="del({{ p.id }})">remove</button></div>
  {% else %}<p>No passkeys yet.</p>{% endfor %}
  <hr class="hr">
  <input id="label" placeholder="label for this device" maxlength="40">
  <button class="btn" id="pkBtn" onclick="add()">Add a passkey on this device</button>
  <div class="msg" id="msg"></div>
</div>
<div class="card"><button class="btn alt" onclick="logout()">Sign out</button></div>
<script>
if(!hasPasskeys()){document.getElementById('pkBtn').disabled=true;say('msg','No passkey support in this browser.',false);}
async function add(){ if(await passkeyRegister(document.getElementById('label').value||'Passkey','msg')) setTimeout(()=>location.reload(),600); }
async function del(id){ if(confirm('Remove this passkey?')){await post('/auth/passkey/delete',{id});location.reload();} }
</script>"""

ADMIN_BODY = r"""
<div class="card">
  <h2>Self-service invite code</h2>
  <p>Anyone with this code can create their own login from the sign-in page (rate-limited: 5 guesses per 15 min per IP). Print it on the card; rotate it when you reprint.</p>
  {% if code_state == 'off' %}<p><b>Off</b> — sign-up is invite-link only.</p>
  {% else %}<p>Current code: <code style="font-size:16px">{{ code }}</code>
    {% if code_exp %}· valid through {{ code_exp }}{% endif %}
    {% if code_state == 'expired' %} <span class="pill" style="color:var(--again)">EXPIRED</span>{% else %} <span class="pill">active</span>{% endif %}</p>{% endif %}
  <input id="cc" placeholder="set a specific code (blank = generate one)" autocapitalize="characters" style="text-transform:uppercase" maxlength="12">
  <input id="ce" placeholder="expires (YYYY-MM-DD, blank = never)" value="{{ code_exp or '' }}">
  <button class="btn" onclick="setCode()">Save / rotate code</button>
  {% if code_state != 'off' %}<button class="btn warn" onclick="codeOff()">Turn self sign-up off</button>{% endif %}
  <div class="msg" id="msgc"></div>
  <p style="margin-top:14px">Lockout email alerts: <b>{{ 'on' if email_on else 'not configured' }}</b>{% if not email_on %} — set ALERT_EMAIL and SMTP_* in .env{% endif %}.</p>
</div>
<div class="card">
  <h2>Invite someone directly</h2>
  <p>Creates an account and a one-time link (valid 72 h). Send the link to them however you like. Re-inviting an existing name resets their login (new authenticator secret, passkeys removed) but keeps their study progress.</p>
  <input id="u" placeholder="name" autocapitalize="off" maxlength="40">
  <label class="chk"><input type="checkbox" id="adm"> make admin</label>
  <button class="btn" onclick="invite()">Create invite link</button>
  <div class="msg" id="msg"></div>
  <p id="out" style="display:none">Link: <code id="link"></code></p>
</div>
<div class="card">
  <h2>Accounts</h2>
  {% for a in accounts %}
  <div class="row"><div>{{ a.username }}
      {% if a.is_admin %}<span class="pill">admin</span>{% endif %}
      <span class="pill">{{ a.status }}</span>
      <br><small>{{ a.npk }} passkey(s) · last login {{ fmt(a.last_login) }}</small></div>
    {% if a.id != me.id %}<button class="btn warn" style="width:auto;margin:0;padding:8px 12px;font-size:12px" onclick="del({{ a.id }},'{{ a.username }}')">delete login</button>{% endif %}</div>
  {% endfor %}
</div>
<div class="card"><a class="link" href="/settings">← security settings</a> · <a class="link" href="/">trainer</a></div>
<script>
async function invite(){
  const j=await post('/admin/invite',{username:document.getElementById('u').value,is_admin:document.getElementById('adm').checked});
  if(j.ok){document.getElementById('out').style.display='';document.getElementById('link').textContent=j.url;say('msg','Invite created.',true);setTimeout(()=>location.reload(),4000);}
  else say('msg',j.error||'Failed.',false);
}
async function del(id,name){ if(confirm('Delete the login for '+name+'? Their study progress is kept.')){await post('/admin/delete',{id});location.reload();} }
async function setCode(){
  const c=document.getElementById('cc').value.trim();
  const j=await post('/admin/code',{action:c?'set':'generate',code:c,expires:document.getElementById('ce').value});
  if(j.ok){say('msgc','Code is now '+j.code,true);setTimeout(()=>location.reload(),900);} else say('msgc',j.error||'Failed.',false);
}
async function codeOff(){ if(confirm('Turn off self sign-up? Existing logins are unaffected.')){await post('/admin/code',{action:'off'});location.reload();} }
</script>"""
