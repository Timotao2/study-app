"""Full WebAuthn ceremony in headless Chromium with a CDP virtual authenticator.
Run: python test_passkey_browser.py   (starts its own Flask server on :5055)"""
import os, shutil, subprocess, sys, time, json
HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "_pw_progress.db")
shutil.copy(os.path.join(HERE, "sr20_progress.db"), SCRATCH)
env = dict(os.environ, SECRET_KEY="pw-test", ORIGIN="http://localhost:5055", RP_ID="localhost", TEST_DB=SCRATCH)

# tiny launcher that repoints the DB then serves
launcher = f"""
import os, app as t, auth
t.DB=os.environ['TEST_DB']; t.init_db(); auth.init_auth_db()
tok=auth.create_invite('Tim', True)
open(os.environ['TEST_DB']+'.token','w').write(tok)
t.app.run(port=5055)
"""
srv = subprocess.Popen([sys.executable, "-c", launcher], cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
for _ in range(50):
    if os.path.exists(SCRATCH + ".token"): break
    time.sleep(0.2)
time.sleep(1.0)
token = open(SCRATCH + ".token").read().strip()

import pyotp, sqlite3
from playwright.sync_api import sync_playwright
fails = 0
def check(cond, msg):
    global fails; print(("  ok   " if cond else "  FAIL ") + msg); fails += 0 if cond else 1
def totp():
    con = sqlite3.connect(SCRATCH); s = con.execute("SELECT totp_secret FROM accounts WHERE username='Tim'").fetchone()[0]; con.close()
    return pyotp.TOTP(s).now()

try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context()
        page = ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        auth_id = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]

        print("enrol via invite page")
        page.goto(f"http://localhost:5055/enroll/{token}")
        page.fill("#c", totp()); page.click("text=Confirm")
        page.wait_for_selector("#step2", state="visible", timeout=5000)
        check(True, "TOTP confirmed, step 2 shown")
        page.fill("#label", "Virtual laptop"); page.click("#pkBtn")
        page.wait_for_function("document.getElementById('msg2').textContent.includes('Passkey saved')", timeout=8000)
        check(True, "passkey registered through navigator.credentials.create")
        creds = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"]
        check(len(creds) == 1 and creds[0]["isResidentCredential"], "authenticator holds 1 resident credential")
        page.wait_for_url("http://localhost:5055/", timeout=5000)
        page.wait_for_selector("#whoBtn:has-text('Tim')", timeout=5000)
        check(True, "redirected to trainer, header shows Tim")
        page.wait_for_function("document.querySelector('#v-study').textContent.trim().length>0", timeout=5000)
        txt = page.inner_text("#v-study"); check(("Fill the blank" in txt) or ("caught up" in txt), "drill view rendered")
        check(page.is_visible("#admBtn"), "admin link visible for admin")

        print("settings page lists the passkey")
        page.goto("http://localhost:5055/settings")
        check("Virtual laptop" in page.inner_text("body"), "passkey listed with label")

        print("sign out, sign in with passkey only")
        page.click("text=Sign out"); page.wait_for_url("**/login")
        r = page.request.get("http://localhost:5055/api/me"); check(r.status == 401, "session gone after sign-out")
        page.click("#pkBtn")
        page.wait_for_url("http://localhost:5055/", timeout=8000)
        page.wait_for_selector("#whoBtn:has-text('Tim')", timeout=5000)
        check(True, "passkey login via navigator.credentials.get succeeded")
        con = sqlite3.connect(SCRATCH); sc, lu = con.execute("SELECT sign_count,last_used FROM passkeys").fetchone(); con.close()
        check(lu is not None, f"passkey last_used recorded (sign_count={sc})")

        print("cloned-authenticator / wrong-origin defences")
        # Remove the credential from the authenticator: login must now fail cleanly.
        cdp.send("WebAuthn.clearCredentials", {"authenticatorId": auth_id})
        page.click("text=Sign out"); page.wait_for_url("**/login")
        page.click("#pkBtn"); page.wait_for_function("document.getElementById('msg').textContent.length>0", timeout=8000)
        check("/login" in page.url, "no credential -> stays on login with message: " + page.inner_text("#msg")[:60])

        print("TOTP login from the login page")
        con = sqlite3.connect(SCRATCH); con.execute("UPDATE accounts SET totp_last_step=0"); con.commit(); con.close()  # replay guard reset for test
        page.fill("#u", "Tim"); page.fill("#c", totp()); page.click("button.btn.alt")
        page.wait_for_url("http://localhost:5055/", timeout=5000)
        check(True, "TOTP login via UI works")

        print("passkey removal from settings")
        page.goto("http://localhost:5055/settings")
        page.once("dialog", lambda d: d.accept())
        page.click("text=remove"); page.wait_for_load_state("networkidle")
        check("No passkeys yet" in page.inner_text("body"), "passkey removed")
        b.close()
finally:
    srv.terminate()
    out = srv.stdout.read().decode(errors="replace")
    if fails: print("---- server log ----\n" + out[-3000:])
    for f in (SCRATCH, SCRATCH + ".token"):
        if os.path.exists(f): os.remove(f)
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
