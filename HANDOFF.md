# Handoff — StudyBuddy

*Updated 2026-09-24.* Earlier notes (2026-09-04 hosting/auth, 2026-06-03
learning model) are kept below because they still apply.

## State of play

- **Modules shipped** (this update): training material is uploaded as
  `studybuddy-module/1` JSON on `/admin` — validation report → preview →
  publish → in everyone's dropdown. `modules.py` holds the format, validator,
  SQLite storage, admin API and the admin-page section. README documents the
  format and the admin flow.
- **SR20 deck is module #1**: `modules/sr20.studybuddy.json`, seeded into the
  `modules` table on first start. The `DECK` list is gone from `app.py`.
- **Progress is keyed by card id** (`progress.card_id` is TEXT). The first
  start converts the old index-keyed rows through `LEGACY_SR20_IDS` in
  `app.py` and keeps the old table as `progress_legacy_index`.
- **MC card type** (question + answer + 1–5 distractors, explanation / note /
  quote+ref after answering). First real bank: DCMA 8210-1D, 73 cards
  (`8210-1d.studybuddy.json`, git-ignored, uploaded through /admin).
- **Geo-block**: app refuses `CF-IPCountry` ∈ `BLOCK_COUNTRIES` (default
  CN,RU,IN) with 403 before routing; the matching Cloudflare WAF rule stops
  them at the edge. `REQUIRE_CLOUDFLARE=1` is available but off.
- Site was **disabled on PythonAnywhere** before this update because of the
  foreign traffic; re-enable on the Web tab after pulling.
- Tests: `test_modules.py` (91 checks, self-contained), `test_auth.py` (59),
  `test_passkey_browser.py`. All green on Flask 3.1.3 / Werkzeug 3.1.8.

## Decisions Tim made (don't relitigate)

- Cloudflare proxied in front of PA, origin certificate, not Let's Encrypt.
- Apex `studybuddy.foo` is the canonical host; `www` redirects.
- Passkeys primary, TOTP mandatory fallback. Invite-only, no open sign-up.
- Repo is **public** (no secrets in it; `.env`, `*.db` and root-level
  `*.studybuddy.json` are ignored).
- Auth first, modularize second — done in that order.
- Modules are stored in SQLite (not files) and loaded per request; uploads
  go through `/admin` as JSON, with server-side escaping of all module text.
- Module cards are keyed by their own string `id`, never by position.
- Uploads get a 1 MB limit on `/admin/modules/*` only; everything else stays
  at 64 KB.
- Block China, Russia and India.

## How the module layer fits together

- `modules.all_modules()` is the registry. It re-reads the `modules` table
  only when the `settings.modules_rev` row changed (every write bumps it), so
  several PA workers stay consistent at the cost of one tiny query per request.
  Within a request the result is memoised on `flask.g`.
- `app.py` asks `modules.get_module(deck, allow_hidden=is_admin)` for every
  API call; hidden modules are admin-only. Unknown/hidden → 400 "unknown deck".
- Everything module-derived that the API returns is passed through
  `modules.esc()` (html.escape). The trainer page therefore writes API fields
  into innerHTML **without** escaping again; only non-module values
  (`USER.name`) go through the client-side `esc()`. MC choices go to the
  browser escaped and come back escaped; `/api/answer` unescapes `answer`
  before comparing. Typed cloze answers are compared raw.
- Choice buttons carry an index (`pick(this, i)`), never text, so no module
  string is ever interpolated into a JS/onclick context.
- `SizedRequest` (subclass of `flask.Request`) overrides `max_content_length`
  by path — works on Flask 3.0 and 3.1 without the per-request setter.
- Re-upload keeps rows for removed cards; `progress_rows()` filters progress
  to the module's current ids. Delete wipes progress+sessions for that module.
- `auth.admin_extra` is a hook `app.py` points at `modules.admin_fragment` so
  `auth.py` renders the Modules section without importing `modules`
  (avoids a circular import).

## Ideas discussed, not built

- Editing a card in the browser (today: download → edit → re-upload).
- AI card generation on the server (today: generate in a Claude chat, upload).
- Per-category accuracy on the Progress tab; confusion-pairs mode.
- Module-level stats on `/admin` (who has drilled what).

## Gotchas worth remembering

- `PAGE` in `app.py` is a Python **raw** string. Write JS escapes exactly as
  the browser should receive them (`<\/b>`, not `<\\/b>`).
- In `auth.py`, the shared JS helpers in `BASE` must stay above `{{ body }}`
  — page bodies contain inline scripts that call them.
- Report strings from `validate_module()` are already HTML-escaped; tests and
  `manage.py` unescape them before printing/matching.
- Tests import `app.py`, which initialises whatever `sr20_progress.db` sits in
  the folder (harmless, idempotent) before they repoint `trainer.DB`; they
  also reset `modules._cache["rev"]` so the scratch DB is reloaded.
- TOTP replay guard: the same 6-digit code cannot be used twice within its
  30 s step. Tests reset `accounts.totp_last_step`.
- Git for Windows prints "LF will be replaced by CRLF" on every commit —
  harmless.
- Never put the git working copy inside Google Drive.
- Jinja-escaped names inside an `onclick="…('{{ x }}')"` break on quotes —
  use `data-*` attributes (the Modules section does).

## Security notes

Rate limits: 5 failed TOTP logins per username, 15 per IP, per 15 min.
Sessions: signed cookie, 30 days, Secure/HttpOnly/SameSite=Lax. Module
uploads: admin-only, strict schema, 1 MB, all text escaped server-side, module
ids restricted to `[A-Za-z0-9._-]` (used in URLs and DB keys). Residual,
deliberately not done: Cloudflare rate-limit rule on `/auth/*`;
`REQUIRE_CLOUDFLARE` left off until Tim wants it.

---

# Earlier handoff — 2026-09-04 (hosting + auth, still accurate)

- **Live** at https://studybuddy.foo behind Cloudflare, on PythonAnywhere as
  Tim's second web app (the CCV app is the first). Deployed and verified the
  same day: DNS, origin cert, Full (strict), www redirect, Bot Fight Mode.
- **Auth shipped**: passkeys + TOTP, invite-only, Tim is admin with one
  passkey enrolled. Unauthenticated probes of `/api/*` and `/admin` return 401
  / the login page. README has the deploy loop and config.
- **Tim's original progress preserved**: his account is linked to the legacy
  `users` row id 1.
- Tim declined PythonAnywhere's HTTP-basic password gate in favour of real auth.

# Earlier handoff — 2026-06-03 (learning model, still accurate)

## Multiple choice → type-in graduation

Number blanks start as multiple choice and switch to type-in at **Box 3**;
falling back to Box 1 returns them to MC. `num_core()` normalises typed
numbers leniently (`67` matches "67 KIAS", `0 to 200` matches "0-200 ft").

## Architecture

- Cloze card: sentence template with `[[id]]` markers; each blank is
  `{"a": answer, "kind": "num"|"word", "alts": [...]}`.
- Numeric distractor pool (`num_pool`) is built per module at load time.
- SQLite: `users(id, name)`, `progress(user_id, deck, card_id TEXT, box,
  last_session, seen, correct)`, `sessions(user_id, deck, session)`,
  `modules(...)`, `settings(k, v)`; plus the auth tables documented in `auth.py`.
- API: `GET /api/me`, `GET /api/decks`, `GET /api/next`, `POST /api/answer`,
  `POST /api/grade`, `POST /api/session/advance`, `GET /api/stats`,
  `POST /api/reset`. All take `deck` (= module id); identity comes from the
  session. Admin: `GET /admin/modules`, `POST /admin/modules/{validate,publish,
  hide,delete}`, `GET /admin/modules/<id>.json`.
- Per-card (not per-blank) Leitner state.
