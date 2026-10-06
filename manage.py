#!/usr/bin/env python3
"""
manage.py — admin tasks from the command line (run in the app folder).

    python manage.py invite <name> [--admin]         create/reset a login, print the invite link
    python manage.py list                            show accounts
    python manage.py secret                          print a fresh SECRET_KEY for .env
    python manage.py code show                       show the self-service invite code
    python manage.py code set <CODE> [YYYY-MM-DD]    set the code (and optional expiry date)
    python manage.py code new [YYYY-MM-DD]           generate a new code
    python manage.py code off                        turn self sign-up off
    python manage.py testmail                        send a test alert email using .env SMTP settings
    python manage.py module list                     show published modules
    python manage.py module check <file.json>        validate a module file, print the report
    python manage.py module publish <file.json>      validate and publish (replaces the same id)
    python manage.py module hide|show|delete <id>    same as the buttons on /admin (delete wipes progress)
    python manage.py module export <id> [file.json]  write a published module back out as JSON
"""
import sys, secrets, json
import app as trainer          # importing app.py initialises the DB (incl. auth + modules tables)
import auth, modules

def module_cmd(argv):
    sub = argv[0] if argv else "list"
    if sub == "list":
        for m in modules.all_modules().values():
            types = ", ".join(f"{n} {t}" for t, n in sorted(m["types"].items()))
            print(f'{m["id"]:24} {m["name"][:34]:34} v{m["version"][:12]:12} {m["count"]:4} cards ({types}){"  HIDDEN" if m["hidden"] else ""}')
        return 0
    if sub in ("check", "publish"):
        if len(argv) < 2: print(f"usage: python manage.py module {sub} <file.json>"); return 1
        with open(argv[1], encoding="utf-8") as f: obj = json.load(f)
        rep = modules.validate_module(obj)
        import html
        for e in rep["errors"]: print(f'  ERROR   {html.unescape(e["card"])}: {html.unescape(e["msg"])}')
        for w in rep["warnings"]: print(f'  warning {html.unescape(w["card"])}: {html.unescape(w["msg"])}')
        if not rep["ok"]: print(f'{len(rep["errors"])} error(s) — nothing published'); return 1
        s = rep["summary"]; print(f'valid: {html.unescape(s["name"])} ({s["id"]}) — {s["count"]} cards ({s["types_text"]})')
        if rep["replace"]:
            r = rep["replace"]; print(f'  replaces v{html.unescape(r["version"])}: {r["kept"]} kept, {r["added"]} new, {r["removed"]} removed; {r["users"]} user(s) have progress')
        if sub == "publish":
            modules.publish_module(obj, uploaded_by="manage.py"); print("published")
        return 0
    if sub in ("hide", "show", "delete", "export"):
        if len(argv) < 2: print(f"usage: python manage.py module {sub} <id>"); return 1
        mid = argv[1]
        if sub == "export":
            data = modules.module_json(mid)
            if data is None: print("no such module"); return 1
            out = argv[2] if len(argv) > 2 else f"{mid}.studybuddy.json"
            with open(out, "w", encoding="utf-8") as f: f.write(json.dumps(json.loads(data), indent=1, ensure_ascii=False) + "\n")
            print("wrote", out); return 0
        ok = modules.delete_module(mid) if sub == "delete" else modules.set_hidden(mid, sub == "hide")
        print({"hide": "hidden", "show": "shown", "delete": "deleted (progress wiped)"}[sub] + ": " + mid if ok else "no such module")
        return 0 if ok else 1
    print(__doc__); return 1

def main(argv):
    if not argv or argv[0] not in ("invite", "list", "secret", "code", "testmail", "module"):
        print(__doc__); return 1
    cmd = argv[0]
    if cmd == "module":
        return module_cmd(argv[1:])
    if cmd == "secret":
        print(secrets.token_hex(32)); return 0
    if cmd == "list":
        con = auth.db()
        for a in con.execute("SELECT * FROM accounts ORDER BY username COLLATE NOCASE"):
            n = con.execute("SELECT COUNT(*) FROM passkeys WHERE account_id=?", (a["id"],)).fetchone()[0]
            print(f'{a["username"]:20} {a["status"]:8} {"admin" if a["is_admin"] else "":6} passkeys={n}')
        con.close(); return 0
    if cmd == "invite":
        if len(argv) < 2: print("usage: python manage.py invite <name> [--admin]"); return 1
        token = auth.create_invite(argv[1], is_admin="--admin" in argv)
        print("Invite link (valid %d h, single use):" % auth.INVITE_TTL_H)
        print("  " + auth.invite_url(token)); return 0
    if cmd == "code":
        sub = argv[1] if len(argv) > 1 else "show"
        if sub == "off":
            auth.set_setting("invite_code", None); auth.set_setting("invite_code_expires", None)
        elif sub in ("set", "new"):
            if sub == "set":
                if len(argv) < 3: print("usage: python manage.py code set <CODE> [YYYY-MM-DD]"); return 1
                c = auth.norm_code(argv[2]); exp = argv[3] if len(argv) > 3 else None
            else:
                c = auth.norm_code(auth.new_code()); exp = argv[2] if len(argv) > 2 else None
            if len(c) < 8: print("code must be at least 8 letters/digits"); return 1
            auth.set_setting("invite_code", c[:4] + "-" + c[4:] if len(c) == 8 else c)
            auth.set_setting("invite_code_expires", exp)
        code, exp, state = auth.code_status()
        print(f"invite code: {code or '(off)'}   expires: {exp or 'never'}   state: {state}"); return 0
    if cmd == "testmail":
        ok = auth.send_email("StudyBuddy test alert", "If you can read this, lockout alerts will reach you.", block=True)
        print("sent" if ok else "not configured — set ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in .env")
        return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
