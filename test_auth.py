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

print("security headers")
h = c.get("/login").headers; check(h.get("X-Frame-Options") == "DENY" and h.get("X-Content-Type-Options") == "nosniff", "headers present")

os.remove(SCRATCH)
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
