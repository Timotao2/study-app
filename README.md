# SR20 Trainer

A persistent, local study app for the Cirrus SR20 reference numbers and procedures.
Supports multiple users and multiple sets of training material.

## What it does

- **Multiple users.** Pick who's studying on the "Who's studying?" screen, or
  type a name to add someone — that's it. Each user has fully independent
  boxes, sessions, and stats. The browser remembers the last user; use the
  header button to switch.
- **Selectable material.** A dropdown in the header selects the training
  material (only the SR20 reference sheet for now). Progress is tracked
  per user *per material*.
- **Cloze deletion with a moving blank.** Each fact is a full sentence; a
  different token is hidden each time the card comes up, so you never memorize
  a fixed answer position.
- **Smart answer routing.** Number blanks (V-speeds, limits) are drilled with
  **multiple choice** whose wrong options are a mix of *other real SR20 values*
  (same unit, to force real discrimination) and *plausible near-misses*.
  Once a card reaches **Box 3**, its number blanks graduate to **type-in**
  (bare numbers accepted — units optional). Word/phrase blanks (procedures,
  cautions) are always type-in, checked with fuzzy matching.
- **Leitner 5-box spaced repetition.** Again → Box 1, Hard → stay, Good → +1,
  Easy → +2. Box review cadence: every 1 / 2 / 4 / 8 / 16 sessions, so mastered
  cards resurface rarely and unpredictably while weak cards keep coming back.
- **Auto-grading.** A correct answer defaults to Good, a wrong answer to Again —
  press Enter to accept, or tap a button to override.
- **Persistent.** All progress is written to `sr20_progress.db` (SQLite) right
  next to the script. Close it, reopen it, switch machines (copy the .db) — your
  boxes and stats are there. Progress from the old single-user version is
  migrated automatically to a user named "Tim" on first run.

## Run it

```bash
pip install flask
python3 app.py
```

Then open http://127.0.0.1:5000 in a browser.

Three tabs: **Drill** (study), **Progress** (box distribution, mastery %,
accuracy), **All Facts** (every card with its box and review count).

## Customizing the deck

Edit the `DECK` list in `app.py`. Each entry is a sentence template with
`[[id]]` blank markers and a `blanks` dict defining each answer, its `kind`
(`num` or `word`), and optional `alts` (accepted typed variants). Restart the
app after editing. **Reset** in the UI wipes only the current user's progress
on the current material; deleting `sr20_progress.db` wipes everything.

## Adding training material

Define another card list like `DECK` in `app.py`, then register it in `DECKS`:

```python
DECKS = {
    "sr20": {"name": "Cirrus SR20 Reference", "cards": DECK},
    "sr22": {"name": "Cirrus SR22 Reference", "cards": SR22_DECK},
}
```

It appears in the material dropdown automatically, with separate progress
tracking per user.
