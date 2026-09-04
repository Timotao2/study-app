# SR20 Trainer — Handoff Document

*Session date: 2026-06-03*

## What this is

A local Flask + SQLite spaced-repetition study app for memorizing Cirrus SR20
reference numbers and procedures, sourced from a one-page reference sheet.
Single file (`app.py`), ~36 cards. Run with `pip install flask`, then
`python3 app.py`, open http://127.0.0.1:5000. Progress persists to
`sr20_progress.db` next to the script.

## Work completed this session

### 1. Fixed dead buttons (bug fix)

Every button in the UI was unresponsive. Root cause: `PAGE` is a Python
**raw** string, so the JS regex on the cloze blank was served literally as
`/<b>_____<\\/b>/`. In JS, `\\` escapes the backslash, the next `/` terminates
the regex early, and the trailing `b>` parses as invalid regex flags — a
SyntaxError that killed the entire `<script>` block, leaving every `onclick`
pointing at undefined functions. Fix: `<\\/b>` → `<\/b>`, plus the same
class of bug in the apostrophe-escaper (`\\\\'` → `\\'`).

**Lesson for future edits:** inside the raw-string `PAGE`, write JS escapes
exactly as the browser should receive them — no doubling for Python.

### 2. Multiple choice → type-in graduation

Number blanks now start as multiple choice and switch to type-in once the
card reaches **Box 3 ("Familiar")**; if a card falls back to Box 1, it
returns to MC until it climbs again. Word blanks were always type-in. The
moving blank is unchanged.

Typed numbers are graded leniently via a new `num_core()` normalizer: bare
numbers accepted (`67` matches "67 KIAS"), commas/case/spacing ignored,
`0 to 200` matches "0-200 ft", `3.8` matches "+3.8 G". Per-blank `alts`
still apply. The drill header shows "type-in · graduated" for promoted cards.

### 3. Multi-user + selectable training material

- **User picker** on launch ("Who's studying?") — click a name or type a new
  one to add it. Last user remembered in browser localStorage; header button
  switches users. All progress, sessions, and stats are per user.
- **Material dropdown** in the header, driven by a `DECKS` registry in
  `app.py`. Only the SR20 deck exists; to add material, define another card
  list and add one entry:
  `"sr22": {"name": "Cirrus SR22 Reference", "cards": SR22_DECK}`.
  Progress is tracked per user *per deck*.
- **Reset** now scopes to current user + current material only.
- **Migration:** on first run, data from the old single-user schema
  auto-migrates to a user named "Tim" (boxes, session counter, stats all
  preserved). Old tables kept as `cards_legacy` / `meta_legacy` backups.

## Current architecture

Single `app.py`:

- `DECK` — card list: sentence templates with `[[id]]` markers; each blank is
  `{"a": answer, "kind": "num"|"word", "alts": [...]}`.
- `DECKS` — registry of materials; `NUM_POOLS` — per-deck distractor pools.
- SQLite tables: `users(id, name)`,
  `progress(user_id, deck, card_id, box, last_session, seen, correct)`,
  `sessions(user_id, deck, session)`.
- Leitner 5-box: Again→Box 1, Hard→stay, Good→+1, Easy→+2; review cadence
  1/2/4/8/16 sessions; correct answers auto-grade Good, wrong auto-Again
  (Enter accepts, buttons override).
- API: `GET/POST /api/users`, `GET /api/decks`, `GET /api/next`,
  `POST /api/answer`, `POST /api/grade`, `POST /api/session/advance`,
  `GET /api/stats`, `POST /api/reset`. All take `user` + `deck`
  (query params on GET, JSON body on POST).
- Frontend: embedded HTML/JS in the `PAGE` raw string; tabs Drill /
  Progress / All Facts; the JS `api()` helper injects user/deck on every call.

## Verification performed

All via a sandboxed copy: page JS parses (`node --check`); full drill loop
exercised through the API; `num_core` checked against 18 normalization cases;
MC vs type-in routing checked at Box 1 and Box 3 over repeated draws;
migration tested against a replica of the old schema (values preserved,
idempotent re-init); two-user isolation confirmed (grades, advances, and
resets don't leak between users); unknown decks rejected.

## Known limitations / notes

- Per-card (not per-blank) Leitner state: all blanks in a sentence share one
  box. Mode graduation keys off the card's box.
- `sr20_flashcards.html`, `sr20_deck.py`, `sr20_deck.json`, `sr20_anki.csv`
  are older standalone artifacts, untouched and unused by `app.py`.
- The app binds to 127.0.0.1 — local use only, no auth (user picker is
  convenience, not security).

## Ideas discussed but not built

- Confusion-pairs focused mode (Vx/Vy, three Va weights, Vs/Vso back-to-back).
- Per-category accuracy breakdown on the Progress tab.
