# Handoff — StudyBuddy

*Updated 2026-09-04.* Earlier session notes (2026-06-03) are preserved at the
bottom because the learning-model details still apply.

## State of play

- **Live** at https://studybuddy.foo behind Cloudflare, on PythonAnywhere as
  Tim's second web app (the CCV app is the first). Deployed and verified the
  same day: DNS, origin cert, Full (strict), www redirect, Bot Fight Mode.
- **Auth shipped**: passkeys + TOTP, invite-only, Tim is admin with one
  passkey enrolled. Unauthenticated probes of `/api/*` and `/admin` return 401
  / the login page. README has the deploy loop and config.
- **Tim's original progress preserved**: his account is linked to the legacy
  `users` row id 1, so all 36 cards' boxes and stats carried over.

## Decisions Tim made (don't relitigate)

- Cloudflare proxied in front of PA, origin certificate, not Let's Encrypt.
- Apex `studybuddy.foo` is the canonical host; `www` redirects.
- Passkeys primary, TOTP mandatory fallback. Invite-only, no open sign-up.
- Repo is **public** (no secrets in it; `.env` and `*.db` are ignored).
- Auth first, modularize second — the modular study app is the next phase.
- Tim declined PythonAnywhere's HTTP-basic password gate in favour of real auth.

## Next phase: subject-agnostic study app

Target workflow: **insert PDF → AI-drafted cards → human review/edit →
deploy as a deck module**, with the SR20 deck becoming module #1.

Open question at handoff: where card generation runs — (a) Claude API from
PythonAnywhere at upload time (API key in `.env`, per-PDF cost), (b) generate
in a Claude chat and commit the deck file, (c) both, starting with (b).
Recommended: (c). Either way the deck-file format and the review/edit UI are
needed first.

Design seams already present: `DECKS` registry in `app.py`; progress keyed
by `(user_id, deck, card_id)`; `NUM_POOLS` built per deck for distractors.

## Gotchas worth remembering

- `PAGE` in `app.py` is a Python **raw** string. Write JS escapes exactly as
  the browser should receive them (`<\/b>`, not `<\\/b>`).
- In `auth.py`, the shared JS helpers in `BASE` must stay above `{{ body }}`
  — page bodies contain inline scripts that call them.
- TOTP replay guard: the same 6-digit code cannot be used twice within its
  30 s step. Tests reset `accounts.totp_last_step`.
- Git for Windows prints "LF will be replaced by CRLF" on every commit —
  harmless.
- Never put the git working copy inside Google Drive.

## Security notes

Rate limits: 5 failed TOTP logins per username, 15 per IP, per 15 min.
Sessions: signed cookie, 30 days, Secure/HttpOnly/SameSite=Lax. Residual
items, deliberately not done: Cloudflare rate-limit rule on `/auth/*`;
Flask check that requests carry Cloudflare headers (PA origin hostname is
discoverable and bypasses the proxy but not the app auth).

---

# Earlier handoff — 2026-06-03 (learning model, still accurate)

## Fixed dead buttons

Every button was unresponsive because the JS regex on the cloze blank was
served as `/<b>_____<\\/b>/` from the raw-string `PAGE`. Fix: `<\/b>`.

## Multiple choice → type-in graduation

Number blanks start as multiple choice and switch to type-in at **Box 3**;
falling back to Box 1 returns them to MC. `num_core()` normalises typed
numbers leniently (`67` matches "67 KIAS", `0 to 200` matches "0-200 ft").

## Architecture

- `DECK` — sentence templates with `[[id]]` markers; each blank is
  `{"a": answer, "kind": "num"|"word", "alts": [...]}`.
- `DECKS` registry; `NUM_POOLS` per-deck distractor pools.
- SQLite: `users(id, name)`, `progress(user_id, deck, card_id, box,
  last_session, seen, correct)`, `sessions(user_id, deck, session)`; plus the
  auth tables documented in `auth.py`.
- API: `GET /api/me`, `GET /api/decks`, `GET /api/next`, `POST /api/answer`,
  `POST /api/grade`, `POST /api/session/advance`, `GET /api/stats`,
  `POST /api/reset`. All take `deck`; identity comes from the session.
- Per-card (not per-blank) Leitner state.

## Ideas discussed, not built

- Confusion-pairs mode (Vx/Vy, the three Va weights, Vs/Vso back-to-back).
- Per-category accuracy on the Progress tab.
