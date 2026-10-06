#!/usr/bin/env python3
"""
StudyBuddy — persistent study app (multi-user, multi-module).

Run:
    pip install -r requirements.txt
    python3 app.py
Then open http://127.0.0.1:5000

Persistence: SQLite file 'sr20_progress.db' created next to this script.
    users(id, name)                          -- study profiles (linked from accounts, see auth.py)
    progress(user_id, deck, card_id, ...)    -- Leitner state per user per module per card id
    sessions(user_id, deck, session)         -- session counter per user per module
    modules(id, name, version, ...)          -- the training material itself (see modules.py)
`deck` is the module id; `card_id` is the card's stable string id from the module
file. Older databases keyed progress by list index — init_db() converts them once.

Adding training material: no code change. An admin uploads a
"studybuddy-module/1" JSON file on /admin (validation report, preview, publish)
and it appears in the material dropdown for everyone. Format in modules.py.
The Cirrus SR20 deck ships as modules/sr20.studybuddy.json and is loaded on
first run.

Adding users: invite-only. An admin creates an invite link on /admin (or with
    python manage.py invite <name> [--admin]); the invitee enrolls an
    authenticator app and, optionally, a passkey. See auth.py.

Configuration (.env next to this file, loaded at import):
    SECRET_KEY        required in production — python manage.py secret
    RP_ID             WebAuthn relying-party id, e.g. studybuddy.foo (defaults to request host)
    ORIGIN            e.g. https://studybuddy.foo (defaults from request; used for invite links)
    BLOCK_COUNTRIES   comma-separated ISO codes refused with 403 when Cloudflare's
                      CF-IPCountry header matches (default CN,RU,IN; empty = off)
    REQUIRE_CLOUDFLARE  1 = refuse requests that did not come through Cloudflare (default off)

Learning model:
    * Leitner 5-box, session-based scheduling (cadence 1/2/4/8/16).
    * Cloze cards: one sentence, multiple blankable tokens; a different
      token is hidden each time the card appears (the "moving blank").
      Number blanks -> multiple choice while the card is in Box 1-2;
      from Box 3 they graduate to type-in (bare numbers accepted).
      Word blanks -> always type-in, checked with fuzzy match.
    * MC cards: a question with the answer + 1-5 distractors, shuffled;
      always multiple choice. Explanation, note and quote show after answering.
    * Grading maps to Leitner: Again->box1, Hard->stay, Good->+1, Easy->+2.
"""
import html, os, re, sqlite3, random, secrets
from datetime import timedelta
from flask import Flask, Request, request, jsonify, Response, g, redirect
from dotenv import load_dotenv
import auth, modules
from modules import esc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DB = os.path.join(BASE_DIR, "sr20_progress.db")
CADENCE = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
BOX_LABEL = {1: "Learning", 2: "Shaky", 3: "Familiar", 4: "Solid", 5: "Mastered"}
DEFAULT_DECK = "sr20"

# Card ids of the original 36-card SR20 deck, in the order it was defined in this
# file before modules existed. progress.card_id used to be that list index; the
# one-time migration in init_db() maps index -> id with this table. Do not reorder.
LEGACY_SR20_IDS = [
    'v-speeds-01', 'v-speeds-02', 'v-speeds-03', 'v-speeds-04',
    'v-speeds-05', 'v-speeds-06', 'v-speeds-07', 'v-speeds-08',
    'v-speeds-09', 'limits-01', 'limits-02', 'limits-03',
    'limits-04', 'limits-05', 'limits-06', 'limits-07',
    'limits-08', 'limits-09', 'takeoff-01', 'takeoff-02',
    'takeoff-03', 'cruise-01', 'landing-01', 'landing-02',
    'landing-03', 'landing-04', 'maneuvers-01', 'maneuvers-02',
    'maneuvers-03', 'instrument-01', 'emergency-01', 'cautions-01',
    'cautions-02', 'cautions-03', 'cautions-04', 'cautions-05',
]

# ---------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
    con.execute("""CREATE TABLE IF NOT EXISTS progress(
        user_id INTEGER, deck TEXT, card_id TEXT,
        box INTEGER DEFAULT 1, last_session INTEGER DEFAULT 0,
        seen INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, deck, card_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        user_id INTEGER, deck TEXT, session INTEGER DEFAULT 1,
        PRIMARY KEY(user_id, deck))""")
    # One-time migration from the original single-user schema (June 2026).
    legacy = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cards'").fetchone()
    if legacy:
        con.execute("INSERT OR IGNORE INTO users(name) VALUES('Tim')")
        uid = con.execute("SELECT id FROM users WHERE name='Tim'").fetchone()["id"]
        for r in con.execute("SELECT * FROM cards").fetchall():
            cid = LEGACY_SR20_IDS[r["id"]] if 0 <= r["id"] < len(LEGACY_SR20_IDS) else str(r["id"])
            con.execute("INSERT OR IGNORE INTO progress(user_id,deck,card_id,box,last_session,seen,correct) VALUES(?,?,?,?,?,?,?)",
                        (uid, "sr20", cid, r["box"], r["last_session"], r["seen"], r["correct"]))
        s = con.execute("SELECT v FROM meta WHERE k='session'").fetchone()
        con.execute("INSERT OR IGNORE INTO sessions(user_id,deck,session) VALUES(?,'sr20',?)",
                    (uid, int(s["v"]) if s else 1))
        con.execute("ALTER TABLE cards RENAME TO cards_legacy")
        con.execute("ALTER TABLE meta RENAME TO meta_legacy")
    con.commit()
    migrate_progress_ids(con)
    con.close()

def _card_id_type(con):
    for r in con.execute("PRAGMA table_info(progress)"):
        if r["name"] == "card_id": return (r["type"] or "").upper()
    return None

def migrate_progress_ids(con):
    """One-time: progress.card_id was the card's list index (INTEGER); modules key
    cards by a string id. Rebuilds the table, mapping SR20 indexes through
    LEGACY_SR20_IDS. Safe with several workers starting at once (BEGIN IMMEDIATE)."""
    if _card_id_type(con) != "INTEGER": return False
    con.execute("BEGIN IMMEDIATE")
    if _card_id_type(con) != "INTEGER":          # another worker got here first
        con.execute("ROLLBACK"); return False
    con.execute("""CREATE TABLE progress_v2(
        user_id INTEGER, deck TEXT, card_id TEXT,
        box INTEGER DEFAULT 1, last_session INTEGER DEFAULT 0,
        seen INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, deck, card_id))""")
    for r in con.execute("SELECT * FROM progress").fetchall():
        cid = r["card_id"]
        if r["deck"] == "sr20" and isinstance(cid, int) and 0 <= cid < len(LEGACY_SR20_IDS):
            cid = LEGACY_SR20_IDS[cid]
        con.execute("INSERT OR IGNORE INTO progress_v2 VALUES(?,?,?,?,?,?,?)",
                    (r["user_id"], r["deck"], str(cid), r["box"], r["last_session"], r["seen"], r["correct"]))
    con.execute("ALTER TABLE progress RENAME TO progress_legacy_index")   # kept as a backup
    con.execute("ALTER TABLE progress_v2 RENAME TO progress")
    con.commit()
    print("progress table migrated to string card ids (old table kept as progress_legacy_index)")
    return True

def ensure_rows(uid, mod):
    """Make sure this user has progress rows + a session counter for this module."""
    con = db()
    con.executemany("INSERT OR IGNORE INTO progress(user_id,deck,card_id) VALUES(?,?,?)",
                    [(uid, mod["id"], cid) for cid in mod["by_id"]])
    con.execute("INSERT OR IGNORE INTO sessions(user_id,deck,session) VALUES(?,?,1)", (uid, mod["id"]))
    con.commit(); con.close()

def get_session(uid, deck):
    con=db(); r=con.execute("SELECT session FROM sessions WHERE user_id=? AND deck=?", (uid,deck)).fetchone(); con.close()
    return r["session"] if r else 1

def progress_rows(uid, mod):
    """This user's progress rows for the module's current cards (rows for cards
    removed by a re-upload stay in the table but are ignored)."""
    con=db(); rows=con.execute("SELECT * FROM progress WHERE user_id=? AND deck=?", (uid,mod["id"])).fetchall(); con.close()
    return [r for r in rows if r["card_id"] in mod["by_id"]]

def near_miss(ans):
    """Generate a plausible +/- variant of a numeric answer like '67 KIAS'."""
    m = re.match(r"([+-]?\d[\d,\.]*)(.*)", ans)
    if not m: return None
    num_s, suffix = m.group(1).replace(",",""), m.group(2)
    try: val = float(num_s)
    except: return None
    delta = random.choice([-10,-5,-4,-3,3,4,5,10]) if val>=50 else random.choice([-2,-1,1,2])
    nv = val+delta
    nv = int(nv) if nv==int(nv) else round(nv,1)
    return f"{nv}{suffix}"

def unit_of(s):
    m=re.search(r"[A-Za-z%/]+.*$", s.strip())
    return (m.group(0).strip() if m else "")

def num_core(s):
    """Lenient numeric core for typed answers: '67 KIAS'->'67',
    '17,500 ft MSL'->'17500', '0 to 200 ft'->'0-200', '+3.8 G'->'3.8'.
    Returns None if no number present."""
    s = s.lower().replace(",", "")
    s = re.sub(r"\s*\bto\b\s*", "-", s)
    s = re.sub(r"\s*([/:\-])\s*", r"\1", s)
    m = re.search(r"[+-]?\d[\d./:\-]*", s)
    return m.group(0).lstrip("+-").rstrip("./:-") if m else None

def make_choices(answer, pool_all):
    opts={answer}
    au=unit_of(answer)
    same=[x for x in pool_all if x!=answer and unit_of(x)==au]
    other=[x for x in pool_all if x!=answer and unit_of(x)!=au]
    random.shuffle(same); random.shuffle(other)
    pool=same+other                      # same-unit values first
    for p in pool:
        if len(opts)>=3: break
        opts.add(p)
    tries=0
    while len(opts)<4 and tries<20:      # plausible near-misses (same unit by construction)
        nm=near_miss(answer); tries+=1
        if nm and nm not in opts: opts.add(nm)
    out=list(opts); random.shuffle(out)
    return out

def render_cloze(card, hide_key):
    """Sentence HTML with one blank hidden. Module text is escaped first; the
    [[key]] markers survive escaping and are replaced afterwards."""
    txt = esc(card["tmpl"])
    for k, b in card["blanks"].items():
        token = "_____" if k == hide_key else esc(b["a"])
        txt = txt.replace(f"[[{k}]]", f"<b>{token}</b>")
    return txt

def fact_text(card):
    """One-line summary for the All Facts table (escaped)."""
    if card["type"] == "mc":
        return f'{esc(card["q"])} <b>[{esc(card["answer"])}]</b>'
    txt = esc(card["tmpl"])
    for k, b in card["blanks"].items(): txt = txt.replace(f"[[{k}]]", f"[{esc(b['a'])}]")
    return txt

# ---------------------------------------------------------------------------
class SizedRequest(Request):
    """Per-route body limit: module uploads may be up to modules.UPLOAD_MAX_BYTES,
    everything else keeps the app-wide MAX_CONTENT_LENGTH."""
    @property
    def max_content_length(self):
        if self.path.startswith("/admin/modules/"): return modules.UPLOAD_MAX_BYTES
        return app.config.get("MAX_CONTENT_LENGTH")

app = Flask(__name__)
app.request_class = SizedRequest

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set in .env — sessions will not survive a restart.")
app.config.update(
    SECRET_KEY=_secret,
    SESSION_COOKIE_NAME="sb_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ORIGIN", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)
auth.db = db
modules.db = db
auth.admin_extra = modules.admin_fragment
app.register_blueprint(auth.bp)
app.register_blueprint(modules.bp)
login_required = auth.login_required

BLOCK_COUNTRIES = {c.strip().upper() for c in os.environ.get("BLOCK_COUNTRIES", "CN,RU,IN").split(",") if c.strip()}
REQUIRE_CLOUDFLARE = os.environ.get("REQUIRE_CLOUDFLARE", "").strip().lower() in ("1", "true", "yes")

@app.before_request
def edge_policy():
    """Cloudflare tags every proxied request with the visitor's country (CF-IPCountry).
    Refuse the countries in BLOCK_COUNTRIES before any routing or DB work."""
    country = (request.headers.get("CF-IPCountry") or "").upper()
    if country in BLOCK_COUNTRIES:
        return Response("Not available in your region.\n", status=403, mimetype="text/plain")
    if REQUIRE_CLOUDFLARE and not request.headers.get("CF-Connecting-IP"):
        return Response("Direct access is not allowed.\n", status=403, mimetype="text/plain")

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp

@app.errorhandler(413)
def too_large(e):
    if auth.wants_json():
        return jsonify({"error": f"Request too large (limit {request.max_content_length // 1024} KB)."}), 413
    return e

def module_or_none(deck):
    """The module this request may use: hidden modules are for admins only."""
    return modules.get_module(deck, allow_hidden=bool(g.account["is_admin"]))

@app.route("/")
def index():
    if auth.current_account() is None:
        return redirect("/login")
    return Response(PAGE, mimetype="text/html")

@app.route("/api/decks")
@login_required
def api_decks():
    mods = modules.visible_modules(bool(g.account["is_admin"]))
    return jsonify({"decks":[{"id":m["id"],"name":esc(m["name"]),"count":m["count"],"hidden":m["hidden"],
                              "types":m["types"]} for m in mods.values()]})

@app.route("/api/next")
@login_required
def api_next():
    uid=g.user_id; deck=request.args.get("deck",DEFAULT_DECK)
    mod=module_or_none(deck)
    if mod is None: return jsonify({"error":"unknown deck"}), 400
    ensure_rows(uid, mod)
    session=get_session(uid, deck)
    rows=progress_rows(uid, mod)
    due=[r for r in rows if (session - r["last_session"]) >= CADENCE[r["box"]]]
    if not due:
        return jsonify({"done":True,"session":session,
                        "next_due_in":min((CADENCE[r["box"]]-(session-r["last_session"]) for r in rows), default=1)})
    r=random.choice(due)
    card=mod["by_id"][r["card_id"]]
    payload={"done":False,"id":r["card_id"],"type":card["type"],"cat":esc(card["cat"]),
             "box":r["box"],"box_label":BOX_LABEL[r["box"]],"session":session,"due_count":len(due)}
    if card["type"]=="mc":
        opts=[card["answer"]]+list(card["distractors"]); random.shuffle(opts)
        payload.update(mode="mc", question=esc(card["q"]), choices=[esc(o) for o in opts])
        return jsonify(payload)
    bid=random.choice(list(card["blanks"].keys()))   # MOVING BLANK
    blank=card["blanks"][bid]
    # Numbers start as multiple choice; once the card reaches Box 3+
    # ("Familiar") they graduate to type-in.  Words are always type-in.
    mode = "mc" if (blank["kind"]=="num" and r["box"] < 3) else "type"
    payload.update(blank_id=bid, sentence=render_cloze(card, bid), kind=blank["kind"], mode=mode)
    if mode=="mc":
        payload["choices"]=[esc(c) for c in make_choices(blank["a"], mod["num_pool"])]
    return jsonify(payload)

@app.route("/api/answer", methods=["POST"])
@login_required
def api_answer():
    d=request.get_json(silent=True) or {}
    deck=d.get("deck",DEFAULT_DECK)
    mod=module_or_none(deck)
    if mod is None: return jsonify({"error":"unknown deck"}), 400
    card=mod["by_id"].get(str(d.get("id","")))
    if card is None: return jsonify({"error":"unknown card"}), 400
    # Choices reach the browser escaped and come back the same way; typed text is raw.
    given=html.unescape(str(d.get("answer") or "")).strip()
    norm=lambda s: re.sub(r"\s+"," ",s.lower().strip())
    if card["type"]=="mc":
        correct = norm(given)==norm(card["answer"])
        return jsonify({"correct":correct,"answer":esc(card["answer"]),
                        "explanation":esc(card.get("explanation","")),"note":esc(card.get("note","")),
                        "quote":esc(card.get("quote","")),"ref":esc(card.get("ref",""))})
    blank=card["blanks"].get(str(d.get("blank_id","")))
    if blank is None: return jsonify({"error":"unknown blank"}), 400
    accepted=[blank["a"].lower()]+[a.lower() for a in blank.get("alts",[])]
    correct = norm(given) in [norm(a) for a in accepted]
    if not correct and blank["kind"]=="num":   # lenient: bare number, units optional
        gc=num_core(given)
        correct = gc is not None and gc in {num_core(a) for a in accepted}
    return jsonify({"correct":correct,"answer":esc(blank["a"])})

@app.route("/api/grade", methods=["POST"])
@login_required
def api_grade():
    d=request.get_json(silent=True) or {}
    uid=g.user_id; deck=d.get("deck",DEFAULT_DECK); cid=str(d.get("id","")); gr=d.get("grade")
    mod=module_or_none(deck)
    if mod is None: return jsonify({"error":"unknown deck"}), 400
    if cid not in mod["by_id"]: return jsonify({"error":"unknown card"}), 400
    session=get_session(uid, deck)
    con=db(); r=con.execute("SELECT * FROM progress WHERE user_id=? AND deck=? AND card_id=?",
                            (uid,deck,cid)).fetchone()
    if r is None: con.close(); return jsonify({"error":"unknown card"}), 400
    box=r["box"]; correct=r["correct"]
    if gr=="again": box=1
    elif gr=="hard": pass
    elif gr=="good": box=min(5,box+1); correct+=1
    elif gr=="easy": box=min(5,box+2); correct+=1
    else: con.close(); return jsonify({"error":"bad grade"}), 400
    con.execute("""UPDATE progress SET box=?, seen=seen+1, correct=?, last_session=?
                   WHERE user_id=? AND deck=? AND card_id=?""",
                (box,correct,session,uid,deck,cid)); con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/session/advance", methods=["POST"])
@login_required
def api_advance():
    d=request.get_json(silent=True) or {}; uid=g.user_id; deck=d.get("deck",DEFAULT_DECK)
    if module_or_none(deck) is None: return jsonify({"error":"unknown deck"}), 400
    con=db(); con.execute("UPDATE sessions SET session=session+1 WHERE user_id=? AND deck=?", (uid,deck))
    con.commit(); con.close()
    return jsonify({"session":get_session(uid,deck)})

@app.route("/api/stats")
@login_required
def api_stats():
    uid=g.user_id; deck=request.args.get("deck",DEFAULT_DECK)
    mod=module_or_none(deck)
    if mod is None: return jsonify({"error":"unknown deck"}), 400
    ensure_rows(uid, mod)
    by_card={r["card_id"]:r for r in progress_rows(uid, mod)}
    rows=[by_card[c["id"]] for c in mod["cards"] if c["id"] in by_card]   # module order
    counts={b:0 for b in range(1,6)}
    for r in rows: counts[r["box"]]+=1
    seen=sum(1 for r in rows if r["seen"]>0)
    ts=sum(r["seen"] for r in rows); tc=sum(r["correct"] for r in rows)
    cards=[{"id":r["card_id"],"cat":esc(mod["by_id"][r["card_id"]]["cat"]),
            "type":mod["by_id"][r["card_id"]]["type"],
            "sentence":fact_text(mod["by_id"][r["card_id"]]),
            "box":r["box"],"seen":r["seen"],"correct":r["correct"]} for r in rows]
    return jsonify({"counts":counts,"box_label":BOX_LABEL,"cadence":CADENCE,
                    "seen":seen,"total":len(rows),"mastered":counts[5],
                    "accuracy":round(tc/ts*100) if ts else 0,"session":get_session(uid,deck),
                    "cards":cards})

@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():
    d=request.get_json(silent=True) or {}; uid=g.user_id; deck=d.get("deck",DEFAULT_DECK)
    con=db()
    con.execute("UPDATE progress SET box=1,last_session=0,seen=0,correct=0 WHERE user_id=? AND deck=?", (uid,deck))
    con.execute("UPDATE sessions SET session=1 WHERE user_id=? AND deck=?", (uid,deck))
    con.commit(); con.close()
    return jsonify({"ok":True})

# ---------------------------------------------------------------------------
# Trainer page. Every module string the API sends is already HTML-escaped by
# the server (modules.esc), so the page writes those fields into innerHTML
# as-is and never escapes them a second time. Only non-module values
# (the user's name) go through esc() here.
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StudyBuddy</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Spline+Sans+Mono:wght@400;500;600&family=Sora:wght@400;600;700;800&display=swap');
:root{--bg:#0b1014;--panel:#121a20;--panel2:#16212a;--ink:#e9f1f4;--muted:#7e94a0;--line:#243038;
--accent:#ff7a18;--accent2:#19c2c2;--good:#36c46e;--hard:#e8b730;--again:#ef5350;--easy:#3aa0ff;
--b1:#ef5350;--b2:#e8b730;--b3:#cfd84a;--b4:#36c46e;--b5:#19c2c2;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Sora',sans-serif;background:radial-gradient(1200px 600px at 80% -10%,#15323a 0,transparent 55%),radial-gradient(900px 500px at -10% 110%,#2a1607 0,transparent 50%),var(--bg);color:var(--ink);min-height:100vh;padding:24px 16px 60px}
.wrap{max-width:860px;margin:0 auto}
header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px}
h1{font-size:26px;font-weight:800;letter-spacing:-.5px}h1 span{color:var(--accent)}
.sub{color:var(--muted);font-family:'Spline Sans Mono',monospace;font-size:12px;letter-spacing:1px;text-transform:uppercase}
.ctrls{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.mini{font-family:'Spline Sans Mono',monospace;font-size:11px;padding:9px 12px;border-radius:8px;background:var(--panel2);color:var(--muted);border:1px solid var(--line);cursor:pointer}
.mini:hover{color:var(--ink);border-color:var(--accent2)}
select.mini{appearance:none;max-width:260px}
nav{display:flex;gap:6px;margin:18px 0 22px;flex-wrap:wrap}
nav button{font-family:'Spline Sans Mono',monospace;font-size:12px;letter-spacing:.5px;background:var(--panel);color:var(--muted);border:1px solid var(--line);padding:9px 16px;border-radius:999px;cursor:pointer;text-transform:uppercase}
nav button.active{background:var(--accent);color:#1a0d00;border-color:var(--accent);font-weight:600}
.progress-line{width:100%;height:6px;background:var(--panel);border-radius:99px;overflow:hidden;margin-bottom:8px}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0;transition:width .3s}
.meta{display:flex;justify-content:space-between;font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--muted);margin-bottom:18px}
.card{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:20px;padding:38px 30px;box-shadow:0 20px 60px -30px rgba(0,0,0,.8);position:relative}
.cat-tag{position:absolute;top:14px;left:18px;font-family:'Spline Sans Mono',monospace;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent2)}
.box-pip{position:absolute;top:14px;right:18px;font-family:'Spline Sans Mono',monospace;font-size:10px;color:var(--muted)}
.face-label{font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin:6px 0 18px}
.sentence{font-size:21px;font-weight:600;line-height:1.5}
.sentence.q{font-size:19px}
.sentence b{color:var(--ink);font-weight:800}
.sentence b._blank{color:var(--accent);letter-spacing:2px}
.choices{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:26px}
.choices button{font-family:'Spline Sans Mono',monospace;font-size:16px;padding:16px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);cursor:pointer;transition:.12s}
.choices.stack{grid-template-columns:1fr}
.choices.stack button{font-family:'Sora',sans-serif;font-size:15px;line-height:1.4;text-align:left;padding:14px 16px}
.choices button:hover{border-color:var(--accent2)}
.choices button.right{background:var(--good);color:#06150c;border-color:var(--good)}
.choices button.wrong{background:var(--again);color:#1a0606;border-color:var(--again)}
.choices button:disabled{cursor:default}
.typein{display:flex;gap:8px;margin-top:24px}
.typein input{flex:1;font-family:'Spline Sans Mono',monospace;font-size:17px;padding:15px;border-radius:12px;border:1px solid var(--line);background:#0c1318;color:var(--ink)}
.typein input:focus{outline:none;border-color:var(--accent2)}
.typein button{padding:0 22px;border-radius:12px;border:none;background:var(--accent2);color:#04201f;font-weight:700;cursor:pointer}
.verdict{margin-top:18px;font-family:'Spline Sans Mono',monospace;font-size:13px}
.verdict.ok{color:var(--good)}.verdict.no{color:var(--again)}
.expl{margin-top:14px;font-size:15px;line-height:1.55}
.note{margin-top:8px;font-size:13px;line-height:1.5;color:var(--muted)}
.quote{margin-top:12px;padding:10px 14px;border-left:3px solid var(--accent2);background:var(--panel);border-radius:0 10px 10px 0;font-size:13px;line-height:1.5}
.quote cite{display:block;margin-top:6px;font-style:normal;font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--muted)}
.grades{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
.grades button{font-weight:700;font-size:14px;padding:14px 6px;border-radius:12px;border:none;cursor:pointer;color:#0b1014;display:flex;flex-direction:column;gap:2px;align-items:center}
.grades button small{font-family:'Spline Sans Mono',monospace;font-weight:400;font-size:9px;opacity:.75}
.g-again{background:var(--again)}.g-hard{background:var(--hard)}.g-good{background:var(--good)}.g-easy{background:var(--easy)}
.done{text-align:center;padding:60px 20px}.done h2{font-size:24px;margin-bottom:10px}.done p{color:var(--muted)}
.boxes{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:24px}
.boxcell{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 8px;text-align:center}
.boxcell .n{font-size:30px;font-weight:800}.boxcell .l{font-family:'Spline Sans Mono',monospace;font-size:10px;color:var(--muted);text-transform:uppercase;margin-top:4px}
.boxcell .cad{font-family:'Spline Sans Mono',monospace;font-size:9px;color:var(--muted);margin-top:6px}
.bar1{color:var(--b1)}.bar2{color:var(--b2)}.bar3{color:var(--b3)}.bar4{color:var(--b4)}.bar5{color:var(--b5)}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.stat{flex:1;min-width:110px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.stat .v{font-size:22px;font-weight:800}.stat .k{font-family:'Spline Sans Mono',monospace;font-size:10px;color:var(--muted);text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-family:'Spline Sans Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--muted);text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}
td{padding:10px 8px;border-bottom:1px solid var(--line)}tr:hover td{background:var(--panel)}
td .cat{display:block;font-family:'Spline Sans Mono',monospace;font-size:10px;color:var(--accent2);text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.pill{display:inline-block;font-family:'Spline Sans Mono',monospace;font-size:10px;padding:2px 8px;border-radius:99px;font-weight:600}
.userlist{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin-top:8px}
.userbtn{font-family:'Spline Sans Mono',monospace;font-size:16px;padding:16px 26px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);cursor:pointer}
.userbtn:hover{border-color:var(--accent)}
.hidden{display:none!important}
footer{margin-top:30px;font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--muted);text-align:center;line-height:1.7}
</style></head>
<body><div class="wrap">
<header><div><h1>Study<span>Buddy</span></h1><div class="sub">spaced repetition · Leitner</div></div>
<div class="ctrls">
<select id="deckSel" class="mini" onchange="setDeck(this.value)"></select>
<a class="mini" id="whoBtn" href="/settings" title="Security settings" style="text-decoration:none">&#128100; —</a>
<a class="mini hidden" id="admBtn" href="/admin" style="text-decoration:none">admin</a>
<button class="mini" onclick="resetAll()">↺ Reset</button>
<button class="mini" onclick="signOut()">Sign out</button>
</div></header>
<nav><button id="t-study" class="active" onclick="show('study')">Drill</button>
<button id="t-progress" onclick="show('progress')">Progress</button>
<button id="t-cards" onclick="show('cards')">All Facts</button></nav>
<section id="v-user" class="hidden"></section>
<section id="v-study"></section>
<section id="v-progress" class="hidden"></section>
<section id="v-cards" class="hidden"></section>
<footer>Your progress saves automatically on the server. Cloze number blanks → multiple choice until a card reaches Box 3, then type-in (bare numbers OK); word blanks → always type-in; question cards → always multiple choice.<br>Each correct answer auto-grades Good — override with the buttons if it felt harder or easier.</footer>
</div>
<script>
let cur=null, answered=false, USER=null, DECK='sr20', ALL_DECKS=[], currentTab='study';
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// Identity comes from the login session; the server ignores any client-supplied user id.
const api=(u,m,b)=>{
  let url=u+(u.includes('?')?'&':'?')+'deck='+encodeURIComponent(DECK);
  const body=b?JSON.stringify(Object.assign({deck:DECK},b)):undefined;
  return fetch(url,{method:m||'GET',headers:{'Content-Type':'application/json'},body}).then(r=>{
    if(r.status===401){location.href='/login';return new Promise(()=>{});}
    return r.json();});
};
async function signOut(){await fetch('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});location.href='/login';}

// ---------- identity & decks ----------
async function boot(){
  USER=await api('/api/me');
  ALL_DECKS=(await api('/api/decks')).decks;
  const savedDeck=localStorage.getItem('trainer_deck');
  if(savedDeck && ALL_DECKS.some(d=>d.id===savedDeck)) DECK=savedDeck;
  else if(ALL_DECKS.length && !ALL_DECKS.some(d=>d.id===DECK)) DECK=ALL_DECKS[0].id;
  const sel=document.getElementById('deckSel');
  // d.name arrives HTML-escaped from the server; d.id is validated [A-Za-z0-9._-].
  sel.innerHTML=ALL_DECKS.map(d=>`<option value="${d.id}">${d.name} (${d.count})${d.hidden?' · hidden':''}</option>`).join('');
  sel.value=DECK;
  updateHeader(); show('study');
}
function updateHeader(){
  document.getElementById('whoBtn').innerHTML='&#128100; '+esc(USER.name)+(USER.passkeys?'':' <span title="No passkey yet — click to add one">&#9888;</span>');
  document.getElementById('admBtn').classList.toggle('hidden',!USER.is_admin);
  document.getElementById('deckSel').value=DECK;
}
function setDeck(d){
  DECK=d; localStorage.setItem('trainer_deck',d);
  if(USER) show(currentTab);
}

// ---------- tabs ----------
function show(t){
  if(!USER)return;
  currentTab=t;
  document.getElementById('v-user').classList.add('hidden');
  ['study','progress','cards'].forEach(x=>{
  document.getElementById('v-'+x).classList.toggle('hidden',x!==t);
  document.getElementById('t-'+x).classList.toggle('active',x===t);});
  if(t==='study')loadNext(); if(t==='progress')loadStats(); if(t==='cards')loadCards();}

// ---------- drill ----------
async function loadNext(){
  const d=await api('/api/next'); cur=d; answered=false;
  const v=document.getElementById('v-study');
  if(d.error){v.innerHTML=`<div class="done"><h2>No material</h2><p>${esc(d.error)}</p></div>`;return;}
  if(d.done){
    v.innerHTML=`<div class="done"><h2>All caught up ✈</h2>
      <p>Nothing due for ${esc(USER.name)} in session #${d.session}.</p>
      <p style="margin-top:12px">Next cards unlock in ${d.next_due_in} session(s).</p>
      <div style="margin-top:22px"><button class="mini" onclick="advance()">Advance session →</button></div></div>`;
    return;}
  const isQ=d.type==='mc';
  const face=isQ?d.question:d.sentence.replace(/<b>_____<\/b>/,'<b class="_blank">_____</b>');
  let answerUI;
  if(d.mode==='mc'){
    // Choice text is server-escaped; buttons carry the index so no text is embedded in JS.
    const stack=isQ||d.choices.some(c=>c.length>28);
    answerUI=`<div class="choices${stack?' stack':''}">${d.choices.map((c,i)=>`<button onclick="pick(this,${i})">${c}</button>`).join('')}</div>`;
  }else{
    answerUI=`<div class="typein"><input id="ti" placeholder="${d.kind==='num'?'type the missing value…':'type the missing word…'}" autocomplete="off"
      onkeydown="if(event.key==='Enter')submitType()"><button onclick="submitType()">Check</button></div>`;
  }
  const modeLabel=d.mode==='mc'?'multiple choice':(d.kind==='num'?'type-in · graduated':'type-in');
  v.innerHTML=`<div class="meta"><span>${d.due_count} due · session #${d.session}</span><span>${modeLabel}</span></div>
    <div class="card"><div class="cat-tag">${d.cat}</div><div class="box-pip">Box ${d.box} · ${d.box_label}</div>
    <div class="face-label">${isQ?'Question':'Fill the blank'}</div><div class="sentence${isQ?' q':''}">${face}</div>${answerUI}
    <div class="verdict" id="verdict"></div><div id="explain"></div><div id="gradeRow"></div></div>`;
  if(d.mode==='type')setTimeout(()=>document.getElementById('ti').focus(),50);
}

async function pick(btn,i){ if(answered)return; await resolve(cur.choices[i],btn); }
async function submitType(){ if(answered)return; const val=document.getElementById('ti').value; await resolve(val,null); }

async function resolve(val,btn){
  answered=true;
  const body={id:cur.id,answer:val}; if(cur.type!=='mc')body.blank_id=cur.blank_id;
  const r=await api('/api/answer','POST',body);
  const vd=document.getElementById('verdict');
  if(cur.mode==='mc'){
    document.querySelectorAll('.choices button').forEach((b,i)=>{
      if(cur.choices[i]===r.answer)b.classList.add('right');
      else if(b===btn&&!r.correct)b.classList.add('wrong');
      b.disabled=true;});
  }else{
    const inp=document.getElementById('ti'); inp.disabled=true;
  }
  vd.className='verdict '+(r.correct?'ok':'no');
  vd.innerHTML=r.correct?'✓ Correct':'✗ Answer: '+r.answer;
  if(cur.type==='mc'){
    let x='';
    if(r.explanation)x+=`<p class="expl">${r.explanation}</p>`;
    if(r.note)x+=`<p class="note">${r.note}</p>`;
    if(r.quote)x+=`<blockquote class="quote">“${r.quote}”${r.ref?`<cite>— ${r.ref}</cite>`:''}</blockquote>`;
    else if(r.ref)x+=`<p class="note">Ref: ${r.ref}</p>`;
    document.getElementById('explain').innerHTML=x;
  }
  // auto-grade default + override buttons
  const def=r.correct?'good':'again';
  document.getElementById('gradeRow').innerHTML=`
    <div class="grades">
      <button class="g-again" onclick="grade('again')">Again<small>→ Box 1</small></button>
      <button class="g-hard" onclick="grade('hard')">Hard<small>stay</small></button>
      <button class="g-good" onclick="grade('good')">Good<small>+1</small></button>
      <button class="g-easy" onclick="grade('easy')">Easy<small>+2</small></button>
    </div>
    <div style="font-family:'Spline Sans Mono',monospace;font-size:10px;color:var(--muted);margin-top:8px;text-align:center">
      auto: <b style="color:var(--ink)">${def}</b> — press Enter to accept</div>`;
  document.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();grade(def);}};
}

async function grade(g){ document.onkeydown=null; await api('/api/grade','POST',{id:cur.id,grade:g}); loadNext(); }
async function advance(){ await api('/api/session/advance','POST',{}); loadNext(); }

// ---------- progress & facts ----------
async function loadStats(){
  const d=await api('/api/stats'); const v=document.getElementById('v-progress');
  if(d.error){v.innerHTML=`<div class="done"><p>${esc(d.error)}</p></div>`;return;}
  const pct=d.total?Math.round(d.mastered/d.total*100):0;
  v.innerHTML=`<div class="boxes">${[1,2,3,4,5].map(b=>`<div class="boxcell">
    <div class="n bar${b}">${d.counts[b]}</div><div class="l">${d.box_label[b]}</div>
    <div class="cad">every ${d.cadence[b]} sess</div></div>`).join('')}</div>
    <div class="stats">
      <div class="stat"><div class="v">${d.mastered}/${d.total}</div><div class="k">Mastered</div></div>
      <div class="stat"><div class="v">${d.seen}/${d.total}</div><div class="k">Touched</div></div>
      <div class="stat"><div class="v">${d.accuracy}%</div><div class="k">Accuracy</div></div>
      <div class="stat"><div class="v">${d.session}</div><div class="k">Sessions</div></div>
    </div>
    <div class="progress-line"><div class="progress-fill" style="width:${pct}%"></div></div>
    <div class="meta"><span>Mastery — ${esc(USER.name)}</span><span>${pct}%</span></div>`;
}
async function loadCards(){
  const d=await api('/api/stats'); const v=document.getElementById('v-cards');
  if(d.error){v.innerHTML=`<div class="done"><p>${esc(d.error)}</p></div>`;return;}
  const rows=d.cards.map(c=>{const acc=c.seen?Math.round(c.correct/c.seen*100)+'%':'—';
    return `<tr><td><span class="pill" style="background:var(--b${c.box});color:#0b1014">B${c.box}</span></td>
    <td><span class="cat">${c.cat}</span>${c.sentence}</td><td style="color:var(--muted);font-family:'Spline Sans Mono',monospace;font-size:11px">${c.seen}× · ${acc}</td></tr>`;}).join('');
  v.innerHTML=`<table><thead><tr><th>Box</th><th>Fact</th><th>Reviews</th></tr></thead><tbody>${rows}</tbody></table>`;
}
async function resetAll(){
  if(!USER)return;
  if(confirm(`Reset your progress on this material?`)){await api('/api/reset','POST',{});show('study');}
}
boot();
</script></body></html>
"""

init_db()
auth.init_auth_db()
modules.init_modules_db()

if __name__=="__main__":
    print("StudyBuddy running at http://127.0.0.1:5000  (Ctrl-C to stop)")
    print("No account yet?  python manage.py invite <name> --admin")
    app.run(debug=False, port=5000)
