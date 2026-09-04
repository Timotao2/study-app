# StudyBuddy — SR20 Trainer

Spaced-repetition study app, live at **https://studybuddy.foo**. Currently one
deck (Cirrus SR20 reference numbers and procedures, 36 cards); built to grow
into a subject-agnostic study app where each deck is a module.

## How it works

- **Cloze with a moving blank.** Each fact is a full sentence; a different
  token is hidden each time the card comes up.
- **Number blanks** start as multiple choice (distractors are other real
  values with the same unit, plus near-misses) and graduate to type-in once
  the card reaches Box 3. **Word blanks** are always type-in, fuzzy-matched.
- **Leitner 5-box**, session-based: Again → Box 1, Hard → stay, Good → +1,
  Easy → +2; boxes resurface every 1 / 2 / 4 / 8 / 16 sessions.
- Correct answers auto-grade Good, wrong ones Again; Enter accepts, buttons
  override.

## Accounts and login

Invite-only. There is no sign-up page.

- Sign in with a **passkey** (Windows Hello, Face ID, fingerprint) or an
  **authenticator-app code** (TOTP). Every account has TOTP; passkeys are
  added per device from `/settings`.
- Admins create invite links on `/admin` (or `python manage.py invite <name>
  [--admin]` on the server). Links are single-use and expire after 72 h.
- Re-inviting an existing name resets their login (new TOTP secret, passkeys
  removed) but keeps their study progress. Lost admin access: run the
  `manage.py invite` command in a PythonAnywhere console.
- Progress is per account per deck, stored server-side in `sr20_progress.db`.

## Hosting

| Layer | Where | Notes |
|---|---|---|
| DNS + edge | Cloudflare (proxied) | Full (strict) TLS, Always-HTTPS, min TLS 1.2, Bot Fight Mode, `www` → apex redirect |
| App | PythonAnywhere web app `studybuddy.foo` | Python 3.13, virtualenv `study-venv`, WSGI `/var/www/studybuddy_foo_wsgi.py`, Cloudflare origin cert (exp. 2041) |
| Code | GitHub `Timotao2/study-app` | working copy `~/code/study-app` on Tim's PC |

PythonAnywhere shows two permanent warnings for this app — "unable to find a
CNAME" and "certificate CN mismatch". Both are artifacts of the Cloudflare
proxy and are expected. `.foo` is an HSTS-preloaded TLD: plain HTTP never
works, so HTTPS must be healthy end-to-end.

## Deploying a change

```bash
# on the PC (Git Bash)
cd ~/code/study-app && git add -A && git commit -m "what changed" && git push
# on PythonAnywhere (Bash console)
cd ~/study-app && git pull
```
Then **Reload** on the Web tab. If `requirements.txt` changed, also run
`workon study-venv && pip install -r requirements.txt` before reloading.

## Configuration

`.env` next to `app.py` (never committed; see `.env.example`):

```
SECRET_KEY=<python manage.py secret>
RP_ID=studybuddy.foo
ORIGIN=https://studybuddy.foo
```

## Running locally

```bash
pip install -r requirements.txt
python manage.py invite Tim --admin      # prints an http://127.0.0.1:5000/enroll/... link
python app.py
```
Open the invite link, enroll a TOTP code, and you're in. Passkeys work on
`localhost` too.

## Tests

```bash
python test_auth.py                # server-side flows, needs sr20_progress.db present
python test_passkey_browser.py     # full WebAuthn ceremony; needs `pip install playwright` + chromium
```

## Files

| File | Role |
|---|---|
| `app.py` | Flask app: deck data, Leitner logic, drill/stats API, embedded trainer UI |
| `auth.py` | Login blueprint: passkeys, TOTP, invites, admin, settings pages |
| `manage.py` | CLI: `invite`, `list`, `secret` |
| `legacy/` | Older standalone artifacts (Anki CSV, static flashcards) — unused |

## Adding a deck (current mechanism)

Define another card list like `DECK` in `app.py` and register it in `DECKS`.
This is the seam the modular version will replace with per-deck files and a
PDF → AI-drafted cards → human review pipeline.
