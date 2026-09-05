"""Server-side flow tests. Run:  python test_auth.py   (uses a scratch copy of the DB)"""
import os, shutil, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "_test_progress.db")
shutil.copy(os.path.join(HERE, "sr20_progress.db"), SCRATCH)
os.environ["SECRET_KEY"] = "test-secret"; os.environ["ORIGIN"] = "http://localhost"; os.environ["RP_ID"] = "localhost"
import app as trainer
trainer.DB = SCRATCH                      # repoint before tables are created
trainer.init_db(); trainer.auth.init_auth_db()
import auth, pyotp

app = trainer.app; app.testing = True
def code_for(username):
    con = auth.db(); s = con.execute("SELECT totp_secret FROM accounts WHERE username=?", (username,)).fetchone()["totp_secret"]; con.close()
    return pyotp.TOTP(s).now()
def new_totp_login(c, name):
    # reset replay guard so tests can log in repeatedly in one 30-s step
    con = auth.db(); con.execute("UPDATE accounts SET totp_last_step=0 WHERE username=?", (name,)); con.commit(); con.close()
    return c.post("/auth/totp/login", json={"username": name, "code": code_for(name)})

fails = 0
def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg); fails += 0 if cond else 1

c = app.test_client()
print("unauthenticated surface")
check(c.get("/").status_code == 302 and c.get("/").headers["Location"].endswith("/login"), "/ redirects to /login")
check(c.get("/api/next?deck=sr20").status_code == 401, "/api/next -> 401")
check(c.get("/api/stats?deck=sr20").status_code == 401, "/api/stats -> 401")
check(c.post("/api/reset", json={"deck": "sr20"}).status_code == 401, "/api/reset -> 401")
check(c.post("/api/grade", json={"id": 0, "grade": "good"}).status_code == 401, "/api/grade -> 401")
check(c.get("/admin").status_code == 302, "/admin redirects")
check(c.get("/api/users").status_code == 404, "/api/users gone (no user enumeration)")
check(c.post("/auth/passkey/register/options").status_code == 401, "passkey register needs login")
check(c.get("/enroll/nonsense").status_code == 410, "bad invite -> 410")

print("invite + enrol admin 'Tim' (links to existing profile id=1)")
token = auth.create_invite("Tim", is_admin=True)
con = auth.db(); acc = con.execute("SELECT * FROM accounts WHERE username='Tim'").fetchone(); con.close()
check(acc["user_id"] == 1 and acc["status"] == "pending", "account linked to existing study profile user_id=1, pending")
r = c.get(f"/enroll/{token}"); check(r.status_code == 200 and b"data:image/svg+xml" in r.data, "enroll page renders QR")
r = c.post(f"/auth/enroll/{token}/totp", json={"code": "000000"}); check(r.status_code == 401, "wrong TOTP rejected")
r = c.post(f"/auth/enroll/{token}/totp", json={"code": code_for("Tim")}); check(r.status_code == 200 and r.get_json()["ok"], "correct TOTP activates + logs in")
check(c.get(f"/enroll/{token}").status_code == 410, "invite single-use")
me = c.get("/api/me").get_json(); check(me["id"] == 1 and me["name"] == "Tim" and me["is_admin"], f"/api/me -> {me}")
st = c.get("/api/stats?deck=sr20").get_json(); check(st["total"] == 36 and st["session"] == 1, "Tim's existing progress visible (36 cards)")
nx = c.get("/api/next?deck=sr20").get_json(); check("sentence" in nx or nx.get("done"), f"drill endpoint answers ({'card' if 'sentence' in nx else 'all caught up'})")
check(c.get("/").status_code == 200, "/ serves trainer when logged in")
check(c.get("/admin").status_code == 200, "admin page for admin")
check(c.get("/settings").status_code == 200, "settings page")

print("client-supplied user id is ignored")
con = auth.db(); con.execute("INSERT OR IGNORE INTO users(name) VALUES('Mallory')"); con.commit()
mid = con.execute("SELECT id FROM users WHERE name='Mallory'").fetchone()["id"]; con.close()
c.post("/api/reset", json={"deck": "sr20", "user": mid})
con = auth.db(); n = con.execute("SELECT COUNT(*) FROM progress WHERE user_id=?", (mid,)).fetchone()[0]; con.close()
check(n == 0, "reset with forged user id touched nobody else's rows")

print("logout + TOTP login + replay guard + lockout")
c.post("/auth/logout"); check(c.get("/api/me").status_code == 401, "logout kills session")
r = new_totp_login(c, "Tim"); check(r.status_code == 200, "TOTP login works")
c.post("/auth/logout")
r = c.post("/auth/totp/login", json={"username": "Tim", "code": code_for("Tim")}); check(r.status_code == 401, "same code cannot be replayed")
for i in range(5): c.post("/auth/totp/login", json={"username": "Tim", "code": "111111"})
r = new_totp_login(c, "Tim"); check(r.status_code == 429, "locked out after 5 failures even with right code")
auth.clear_fails("u:tim", "ip:" + "?"); auth.clear_fails("ip:127.0.0.1")
r = new_totp_login(c, "Tim"); check(r.status_code == 200, "login again after clearing fails")

print("admin invites a second user, non-admin cannot reach admin")
r = c.post("/admin/invite", json={"username": "Eva"}); j = r.get_json(); check(r.status_code == 200 and "/enroll/" in j["url"], "admin invite endpoint")
tok2 = j["url"].rsplit("/", 1)[1]
c2 = app.test_client()
r = c2.post(f"/auth/enroll/{tok2}/totp", json={"code": code_for("Eva")}); check(r.status_code == 200, "Eva enrols")
check(c2.get("/admin").status_code == 403, "Eva blocked from /admin")
check(c2.post("/admin/invite", json={"username": "x"}).status_code == 403, "Eva blocked from invite API")
me2 = c2.get("/api/me").get_json(); check(me2["name"] == "Eva" and not me2["is_admin"] and me2["id"] != 1, "Eva has her own profile")
st2 = c2.get("/api/stats?deck=sr20").get_json(); check(st2["seen"] == 0 and st2["total"] == 36, "Eva starts fresh")
r = c.post("/admin/delete", json={"id": acc["id"]}); check(r.status_code == 400, "admin cannot delete self")

print("re-invite resets login but keeps progress")
c.post("/api/grade", json={"deck": "sr20", "id": 0, "grade": "good"})
tok3 = auth.create_invite("Tim")
con = auth.db(); a = con.execute("SELECT * FROM accounts WHERE username='Tim'").fetchone()
seen = con.execute("SELECT SUM(seen) FROM progress WHERE user_id=1").fetchone()[0]; con.close()
check(a["status"] == "pending" and a["is_admin"] == 1 and seen >= 1, "pending again, still admin, progress intact")
check(c.get("/api/me").status_code == 401, "old session invalid while pending")

print("passkey option generation")
c.post(f"/auth/enroll/{tok3}/totp", json={"code": code_for("Tim")})
r = c.post("/auth/passkey/register/options"); j = r.get_json()
check(r.status_code == 200 and j["rp"]["id"] == "localhost" and j["authenticatorSelection"]["residentKey"] == "required", "registration options: rp + resident key")
r = c.post("/auth/passkey/login/options"); j = r.get_json(); check(r.status_code == 200 and j.get("allowCredentials", []) == [] and "challenge" in j, "login options: discoverable")
r = c.post("/auth/passkey/login/verify", json={"id": "nope", "response": {}}); check(r.status_code == 401, "unknown credential rejected")


print("invite-code sign-up")
c3 = app.test_client()
check(c3.get("/api/signup/status").get_json()["open"] is False, "sign-up closed by default")
check(c3.post("/auth/signup", json={"code": "X", "username": "Bob"}).status_code == 403, "signup refused while off")
r = c.post("/admin/code", json={"action": "set", "code": "x7c4-dvwm", "expires": "2027-01-01"}); j = r.get_json()
check(r.status_code == 200 and j["code"] == "X7C4-DVWM" and j["state"] == "active", f"admin sets code -> {j['code']} active until {j['expires']}")
check(c2.post("/admin/code", json={"action": "off"}).status_code == 403, "non-admin cannot change code")
check(c3.get("/api/signup/status").get_json()["open"] is True, "sign-up open")
check(c3.post("/auth/signup", json={"code": "X7C4-DVWN", "username": "Bob"}).status_code == 401, "wrong code rejected")
check(c3.post("/auth/signup", json={"code": "x7c4 dvwm", "username": "Tim"}).status_code == 409, "existing name refused (case/spacing normalised code accepted)")
r = c3.post("/auth/signup", json={"code": "X7C4-DVWM", "username": "Bob"}); j = r.get_json()
check(r.status_code == 200 and j["url"].startswith("/enroll/"), "correct code -> enrol link")
r = c3.post("/auth/enroll/" + j["url"].split("/")[-1] + "/totp", json={"code": code_for("Bob")})
check(r.status_code == 200 and c3.get("/api/me").get_json()["name"] == "Bob", "Bob enrols and is logged in")
check(c3.get("/api/stats?deck=sr20").get_json()["seen"] == 0, "Bob starts fresh, not linked to anyone's progress")
check(c3.post("/auth/signup", json={"code": "X7C4-DVWM", "username": "bob"}).status_code == 409, "duplicate name (case-insensitive) refused")
c.post("/admin/code", json={"action": "set", "code": "X7C4-DVWM", "expires": "2020-01-01"})
check(c3.post("/auth/signup", json={"code": "X7C4-DVWM", "username": "Carol"}).status_code == 403, "expired code refused")
check(c3.get("/api/signup/status").get_json()["open"] is False, "status reports closed when expired")
r = c.post("/admin/code", json={"action": "generate", "expires": ""}); j = r.get_json()
check(r.status_code == 200 and len(j["code"]) == 9 and all(ch not in "0O1IL" for ch in j["code"].replace("-", "")), f"generated code {j['code']} has no look-alikes")
c.post("/admin/code", json={"action": "off"}); check(c3.get("/api/signup/status").get_json()["state"] == "off", "code turned off")
c.post("/admin/code", json={"action": "set", "code": "X7C4-DVWM", "expires": "2027-01-01"})
for i in range(5): c3.post("/auth/signup", json={"code": "WRONG-WRONG", "username": "Dan"})
check(c3.post("/auth/signup", json={"code": "X7C4-DVWM", "username": "Dan"}).status_code == 429, "5 wrong codes -> locked out")
auth.clear_fails("code:127.0.0.1", "code:?")

print("lockout email alert (real SMTP round-trip to a local test server)")
from aiosmtpd.controller import Controller
class Sink:
    inbox = []
    async def handle_DATA(self, server, session, envelope):
        Sink.inbox.append(envelope.content.decode()); return "250 OK"
import smtplib as smtplib_mod
class PlainSMTP(smtplib_mod.SMTP):  # skip STARTTLS/login against the plaintext test server
    def starttls(self, *a, **k): return (220, b"ok")
    def login(self, *a, **k): return (235, b"ok")
auth.smtplib.SMTP = PlainSMTP
ctrl = Controller(Sink(), hostname="127.0.0.1", port=8025); ctrl.start()
os.environ.update(ALERT_EMAIL="timotao2@gmail.com", SMTP_HOST="127.0.0.1", SMTP_PORT="8025", SMTP_USER="timotao2@gmail.com", SMTP_PASS="x")
auth.clear_fails("u:tim", "ip:127.0.0.1", "ip:?")
con = auth.db(); con.execute("DELETE FROM settings WHERE k LIKE 'alert:%'"); con.commit(); con.close()   # reset alert throttle
c4 = app.test_client()
for i in range(5): c4.post("/auth/totp/login", json={"username": "Tim", "code": "000000"})
time.sleep(1.0)
check(len(Sink.inbox) == 1 and "lockout" in Sink.inbox[0] and "account tim" in Sink.inbox[0] and "To: timotao2@gmail.com" in Sink.inbox[0], f"one email on the 5th failure for account Tim (got {len(Sink.inbox)})")
for i in range(3): c4.post("/auth/totp/login", json={"username": "Tim", "code": "000000"})
time.sleep(0.5); check(len(Sink.inbox) == 1, "further failures in the window do not re-send (throttled)")
# IP threshold: 5 recorded so far on this IP (locked-out attempts are not counted); 10 more from distinct names -> 15
for i in range(10): c4.post("/auth/totp/login", json={"username": f"ghost{i}", "code": "000000"})
time.sleep(1.5)
check(len(Sink.inbox) == 2 and "from IP" in Sink.inbox[1], f"second email when the IP threshold (15) trips (got {len(Sink.inbox)}: {[m.splitlines()[2] for m in Sink.inbox]})")
ctrl.stop()
auth.clear_fails("u:tim", "ip:127.0.0.1", "ip:?")

print("security headers")
h = c.get("/login").headers; check(h.get("X-Frame-Options") == "DENY" and h.get("X-Content-Type-Options") == "nosniff", "headers present")

os.remove(SCRATCH)
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
