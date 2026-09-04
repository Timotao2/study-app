#!/usr/bin/env python3
"""
manage.py — admin tasks from the command line (run in the app folder).

    python manage.py invite <name> [--admin]   create/reset a login, print the invite link
    python manage.py list                      show accounts
    python manage.py secret                    print a fresh SECRET_KEY for .env
"""
import sys, secrets
import app as trainer          # importing app.py initialises the DB (incl. auth tables)
import auth

def main(argv):
    if len(argv) < 1 or argv[0] not in ("invite", "list", "secret"):
        print(__doc__); return 1
    if argv[0] == "secret":
        print(secrets.token_hex(32)); return 0
    if argv[0] == "list":
        con = auth.db()
        for a in con.execute("SELECT * FROM accounts ORDER BY username COLLATE NOCASE"):
            n = con.execute("SELECT COUNT(*) FROM passkeys WHERE account_id=?", (a["id"],)).fetchone()[0]
            print(f'{a["username"]:20} {a["status"]:8} {"admin" if a["is_admin"] else "":6} passkeys={n}')
        con.close(); return 0
    if len(argv) < 2:
        print("usage: python manage.py invite <name> [--admin]"); return 1
    token = auth.create_invite(argv[1], is_admin="--admin" in argv)
    print("Invite link (valid %d h, single use):" % auth.INVITE_TTL_H)
    print("  " + auth.invite_url(token))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
