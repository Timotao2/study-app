# StudyBuddy

Spaced-repetition study app, live at **https://studybuddy.foo**. Training
material is organised in **modules** — JSON files an admin uploads on `/admin`
— so new material needs no code change. Ships with the Cirrus SR20 reference
deck (36 cloze cards); the DCMA 8210-1D bank (73 multiple-choice cards) was the
first uploaded module.

## How it works

- **Cloze cards** with a moving blank: each fact is a full sentence; a
  different token is hidden each time the card comes up. Number blanks start as
  multiple choice (distractors are other real values with the same unit, plus
  near-misses) and graduate to type-in once the card reaches Box 3. Word blanks
  are always type-in, fuzzy-matched.
- **Multiple-choice cards** (`mc`): a question, the answer and 1–5 distractors,
  shuffled every time. Always multiple choice. After answering you see the
  explanation, a note, and the source quote with its reference.
- **Leitner 5-box**, session-based: Again → Box 1, Hard → stay, Good → +1,
  Easy → +2; boxes resurface every 1 / 2 / 4 / 8 / 16 sessions.
- Correct answers auto-grade Good, wrong ones Again; Enter accepts, buttons
  override. Progress is per account, per module, per card id.

## Modules

### Uploading (admin)

`/admin` → **Modules** → pick the `.json` file. The browser reads it and posts it
as JSON; the server returns a **validation report** (errors listed by card id,
warnings for unknown fields, a preview of the first cards, and — when the id
already exists — how many cards are kept / new / removed and how many users
have progress). Nothing is published while there is any error. Click
**Publish**; it appears in everyone's material dropdown immediately.

- **Replace**: upload a file with the same `id`. Every card whose `id` is
  unchanged keeps everyone's progress; new cards start in Box 1; removed cards
  drop out of the drill (their rows are kept in the database, so restoring the
  card restores the progress).
- **Hide**: keeps the module and all progress but only admins can see or drill
  it (handy for checking a new bank before release).
- **Delete**: removes the module **and everyone's progress on it**.
- **Download**: the stored JSON, for editing and re-uploading.

The same operations from a PythonAnywhere console:
`python manage.py module list | check <file> | publish <file> | hide <id> | show <id> | delete <id> | export <id>`.

Uploads may be up to 1 MB (`/admin/modules/*` only; every other request stays
at the 64 KB app-wide limit).

### File format `studybuddy-module/1`

```json
{"format": "studybuddy-module/1",
 "id": "dcma-8210-1d",              // 1–64 of A-Z a-z 0-9 . _ -  — the permanent key
 "name": "DCMA-INST 8210-1D",       // dropdown label
 "version": "2026-09-24",
 "source": "where the facts came from",
 "cards": [ ... ]}
```

Every card has a stable string `id` (letters, digits, `. _ - :`), a `cat`
(category shown on the card), and a `type`:

```json
{"id": "v-speeds-01", "type": "cloze", "cat": "V-speeds",
 "tmpl": "Vr, normal rotation at 50% flaps, is [[0]].",
 "blanks": {"0": {"a": "67 KIAS", "kind": "num", "alts": ["67"]}}}
```
`kind` is `num` or `word`; `alts` (optional) are other accepted typed answers.
Every `[[key]]` in `tmpl` must have an entry in `blanks` and vice versa.

```json
{"id": "ro-01", "type": "mc", "cat": "§1–2 Roles & waivers", "ref": "8210-1D para 1.1.1",
 "q": "Who appoints GFRs and G-GFRs for contractor aircraft operations?",
 "answer": "The Approving Authority",
 "distractors": ["The CASC commander", "The ACO", "The Service waiver authority"],
 "explanation": "The Approving Authority, which also appoints alternates (1.1).",
 "note": "optional", "quote": "optional — shown with ref after answering"}
```

Rejected outright: wrong `format`, unknown card `type`, missing required field,
duplicate card `id`, answer repeated among the distractors (case/space
insensitive), duplicate distractors, 0 or more than 5 distractors, a blank with
no marker or a marker with no blank. Unknown fields are ignored with a warning.

Module text is never trusted: everything the API sends to the trainer page is
HTML-escaped on the server (`modules.esc`), so an uploaded `<script>` renders as
text.

The bundled SR20 deck lives in `modules/sr20.studybuddy.json` and is loaded
into the database on first start (once; afterwards it is an ordinary module you
can replace, hide or delete). Older databases that keyed progress by list index
are converted to card ids automatically on the first start after this version
(the old table is kept as `progress_legacy_index`).

## Accounts and login

Two ways in, both controlled by an admin:

- **Shared invite code** (the one printed on the business cards). Anyone with
  the current code creates their own login from the sign-in page. Set, rotate,
  expire, or switch it off on `/admin` (or `python manage.py code …`). Code
  guesses are rate-limited to 5 per 15 min per IP. When the expiry date
  passes, the code stops working and the sign-up box disappears.
- **Direct invite link** for one person: `/admin` → "Invite someone directly",
  or `python manage.py invite <name> [--admin]`. Single-use, 72 h.

- Sign in with a **passkey** (Windows Hello, Face ID, fingerprint) or an
  **authenticator-app code** (TOTP). Every account has TOTP; passkeys are
  added per device from `/settings`.
- Re-inviting an existing name resets their login (new TOTP secret, passkeys
  removed) but keeps their study progress. Lost admin access: run the
  `manage.py invite` command in a PythonAnywhere console.
- Progress is stored server-side in `sr20_progress.db`.

## Hosting

| Layer | Where | Notes |
|---|---|---|
| DNS + edge | Cloudflare (proxied) | Full (strict) TLS, Always-HTTPS, min TLS 1.2, Bot Fight Mode, `www` → apex redirect, WAF rule blocking CN/RU/IN |
| App | PythonAnywhere web app `studybuddy.foo` | Python 3.13, virtualenv `study-venv`, WSGI `/var/www/studybuddy_foo_wsgi.py`, Cloudflare origin cert (exp. 2041) |
| Code | GitHub `Timotao2/study-app` | working copy `~/code/study-app` on Tim's PC |

PythonAnywhere shows two permanent warnings for this app — "unable to find a
CNAME" and "certificate CN mismatch". Both are artifacts of the Cloudflare
proxy and are expected. `.foo` is an HSTS-preloaded TLD: plain HTTP never
works, so HTTPS must be healthy end-to-end.

**Geo-blocking** happens twice: a Cloudflare WAF custom rule
(`ip.src.country in {"CN" "RU" "IN"}` → Block) stops those hits at the edge so
they never count against PythonAnywhere, and the app refuses the same
countries itself via the `CF-IPCountry` header (`BLOCK_COUNTRIES` in `.env`,
default `CN,RU,IN`, empty = off) in case the edge rule is ever removed.
`REQUIRE_CLOUDFLARE=1` additionally refuses requests that hit the
PythonAnywhere hostname directly (off by default).

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
ALERT_EMAIL=…      # lockout alerts go here (optional)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=…        # the Gmail address that sends
SMTP_PASS=…        # a Gmail App Password, not the account password
BLOCK_COUNTRIES=CN,RU,IN
REQUIRE_CLOUDFLARE=0
```

Lockout alerts: one email when an account hits 5 failed code logins, an IP
hits 15, or an IP burns 5 invite-code guesses — throttled to one per key per
hour. `python manage.py testmail` sends a test message.

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
python test_modules.py             # module upload, validation, progress-by-id, migration, escaping, geo block (self-contained)
python test_auth.py                # server-side login flows, needs sr20_progress.db present
python test_passkey_browser.py     # full WebAuthn ceremony; needs `pip install playwright` + chromium
```
`test_modules.py` also validates `8210-1d.studybuddy.json` if that file sits in
the repo folder (module files in the repo root are git-ignored).

## Files

| File | Role |
|---|---|
| `app.py` | Flask app: Leitner logic, drill/stats API, embedded trainer UI, edge policy |
| `modules.py` | Module format, strict validator, storage + per-request cache, admin upload API and page section |
| `modules/sr20.studybuddy.json` | The bundled SR20 deck (module #1) |
| `auth.py` | Login blueprint: passkeys, TOTP, invites, admin, settings pages |
| `manage.py` | CLI: `invite`, `list`, `secret`, `code`, `testmail`, `module` |
| `legacy/` | Older standalone artifacts (Anki CSV, static flashcards) — unused |

## Making a module

Generate the JSON in a Claude chat from the source document (PDF → cards),
read it through, save it as `<id>.studybuddy.json`, and upload it on `/admin`.
The validation report catches structural mistakes; the preview is the place to
catch wrong facts before anyone drills them.
