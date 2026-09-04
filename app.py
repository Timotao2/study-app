#!/usr/bin/env python3
"""
Trainer — persistent local study app (multi-user, multi-material).

Run:
    pip install flask
    python3 app.py
Then open http://127.0.0.1:5000

Persistence: SQLite file 'sr20_progress.db' created next to this script.
    users(id, name)                          -- who is studying
    progress(user_id, deck, card_id, ...)    -- Leitner state per user per deck
    sessions(user_id, deck, session)         -- session counter per user per deck
On first run after upgrading, progress from the original single-user schema
is migrated automatically to a user named "Tim".

Adding training material: define another card list like DECK below, then add
one entry to DECKS, e.g.  "sr22": {"name": "Cirrus SR22 Reference", "cards": SR22_DECK}
It will appear in the material dropdown automatically.

Adding users: just type a name in the "Who's studying?" screen in the app.

Learning model:
    * Leitner 5-box, session-based scheduling (cadence 1/2/4/8/16).
    * Cloze cards: one sentence, multiple blankable tokens; a different
      token is hidden each time the card appears (the "moving blank").
    * Number blanks -> multiple choice while the card is in Box 1-2;
      from Box 3 they graduate to type-in (bare numbers accepted).
      Word blanks -> always type-in, checked with fuzzy match.
    * Grading maps to Leitner: Again->box1, Hard->stay, Good->+1, Easy->+2.
"""
import json, os, re, sqlite3, random
from flask import Flask, request, jsonify, Response

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sr20_progress.db")
CADENCE = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
BOX_LABEL = {1: "Learning", 2: "Shaky", 3: "Familiar", 4: "Solid", 5: "Mastered"}

# ---------------------------------------------------------------------------
# DECK.  Each card is a sentence with [[id]] blank markers defined in "blanks".
#   kind="num"  -> token is a number/value, drilled via multiple choice
#   kind="word" -> token is a word/phrase, drilled via type-in
# A blank: {"a": shown answer, "kind": "num"|"word", "alts": [accepted typed variants]}
# ---------------------------------------------------------------------------
DECK = [
    # ---- V-speeds (number-discrimination heavy) ----
    {"cat":"V-speeds","tmpl":"Vr, normal rotation at 50% flaps, is [[0]].",
     "blanks":{"0":{"a":"67 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Vx (best angle of climb) is [[0]] at sea level and [[1]] at 10K.",
     "blanks":{"0":{"a":"81 KIAS","kind":"num"},"1":{"a":"85 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Vy (best rate of climb) is [[0]] at sea level and [[1]] at 10K.",
     "blanks":{"0":{"a":"96 KIAS","kind":"num"},"1":{"a":"91 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Best glide is [[0]] at 3000 lb and [[1]] at 2500 lb.",
     "blanks":{"0":{"a":"96 KIAS","kind":"num"},"1":{"a":"87 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Vpd, the max CAPS deployment speed, is [[0]].",
     "blanks":{"0":{"a":"135 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Va (design maneuvering) is [[0]] at 3000 lb, [[1]] at 2600 lb, and [[2]] at 2200 lb.",
     "blanks":{"0":{"a":"131 KIAS","kind":"num"},"1":{"a":"122 KIAS","kind":"num"},"2":{"a":"111 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Vfe is [[0]] with flaps 50% and [[1]] with flaps 100%.",
     "blanks":{"0":{"a":"120 KIAS","kind":"num"},"1":{"a":"100 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Vno (max structural cruise) is [[0]] and Vne (never exceed) is [[1]].",
     "blanks":{"0":{"a":"165 KIAS","kind":"num"},"1":{"a":"200 KIAS","kind":"num"}}},
    {"cat":"V-speeds","tmpl":"Stall speed is [[0]] clean (Vs) and [[1]] with flaps 100% (Vso).",
     "blanks":{"0":{"a":"65 KIAS","kind":"num"},"1":{"a":"56 KIAS","kind":"num"}}},
    # ---- Limits / capacities ----
    {"cat":"Limits","tmpl":"The engine makes [[0]] at [[1]].",
     "blanks":{"0":{"a":"200 HP","kind":"num"},"1":{"a":"2700 RPM","kind":"num"}}},
    {"cat":"Limits","tmpl":"Load factor limits are [[0]] positive and [[1]] negative.",
     "blanks":{"0":{"a":"+3.8 G","kind":"num"},"1":{"a":"-1.9 G","kind":"num"}}},
    {"cat":"Limits","tmpl":"Maximum glide ratio is [[0]].",
     "blanks":{"0":{"a":"10.9:1","kind":"num","alts":["10.9 to 1","10.9"]}}},
    {"cat":"Limits","tmpl":"Mag check limits: [[0]] max drop and [[1]] max differential.",
     "blanks":{"0":{"a":"150 RPM","kind":"num"},"1":{"a":"75 RPM","kind":"num"}}},
    {"cat":"Limits","tmpl":"Usable fuel is [[0]]; oil is [[1]] min and [[2]] max.",
     "blanks":{"0":{"a":"56 gal","kind":"num"},"1":{"a":"6 qt","kind":"num"},"2":{"a":"8 qt","kind":"num"}}},
    {"cat":"Limits","tmpl":"Max takeoff weight is [[0]] and max landing weight is [[1]].",
     "blanks":{"0":{"a":"3000 lb","kind":"num"},"1":{"a":"2900 lb","kind":"num"}}},
    {"cat":"Limits","tmpl":"Max useful load is [[0]], max full-fuel payload [[1]], max cargo area [[2]].",
     "blanks":{"0":{"a":"950 lb","kind":"num"},"1":{"a":"622 lb","kind":"num"},"2":{"a":"130 lb","kind":"num"}}},
    {"cat":"Limits","tmpl":"Max operating altitude is [[0]]; min autopilot engagement is [[1]].",
     "blanks":{"0":{"a":"17500 ft MSL","kind":"num","alts":["17,500 ft msl","17500 msl","17500"]},
               "1":{"a":"400 ft AGL","kind":"num","alts":["400 agl","400"]}}},
    {"cat":"Limits","tmpl":"Max demonstrated crosswind component is [[0]].",
     "blanks":{"0":{"a":"21 knots","kind":"num","alts":["21 kt","21"]}}},
    # ---- Takeoff procedures (word/phrase recall) ----
    {"cat":"Takeoff","tmpl":"Normal takeoff: flaps [[0]], rotate at [[1]], flaps up at [[2]], climb at [[3]].",
     "blanks":{"0":{"a":"50%","kind":"num"},"1":{"a":"67 KIAS","kind":"num"},"2":{"a":"85 KIAS","kind":"num"},"3":{"a":"96 KIAS","kind":"num"}}},
    {"cat":"Takeoff","tmpl":"Short field: rotate at [[0]], pull to [[1]] attitude, climb [[2]] until obstacle cleared.",
     "blanks":{"0":{"a":"65 KIAS","kind":"num"},"1":{"a":"Vx","kind":"word","alts":["vx"]},"2":{"a":"81 KIAS","kind":"num"}}},
    {"cat":"Takeoff","tmpl":"Soft field: full [[0]] yoke, at liftoff stay in ground effect to [[1]] before Vy attitude.",
     "blanks":{"0":{"a":"aft","kind":"word","alts":["back","rear"]},"1":{"a":"81 KIAS","kind":"num"}}},
    {"cat":"Cruise","tmpl":"Cruise power is [[0]]; enroute climb is [[1]] knots above Vy for cooling.",
     "blanks":{"0":{"a":"70-75%","kind":"num","alts":["70 to 75%","70-75"]},"1":{"a":"5 to 10","kind":"num","alts":["5-10","5 10"]}}},
    # ---- Landing procedures ----
    {"cat":"Landing","tmpl":"Normal landing: abeam touchdown [[0]] and [[1]]; base flaps 50% at [[2]]; final flaps 100% at [[3]].",
     "blanks":{"0":{"a":"1500 RPM","kind":"num"},"1":{"a":"100 KIAS","kind":"num"},"2":{"a":"90 KIAS","kind":"num"},"3":{"a":"75 KIAS","kind":"num"}}},
    {"cat":"Landing","tmpl":"Balked landing: full power, flaps [[0]], climb [[1]] until obstacles cleared.",
     "blanks":{"0":{"a":"50%","kind":"num"},"1":{"a":"81 KIAS","kind":"num"}}},
    {"cat":"Landing","tmpl":"Short field landing: fly final at [[0]], land within [[1]] of the spot.",
     "blanks":{"0":{"a":"75 KIAS","kind":"num"},"1":{"a":"0-200 ft","kind":"num","alts":["0 to 200 ft","0-200"]}}},
    {"cat":"Landing","tmpl":"Soft field landing: leave power at [[0]] through touchdown, touch down at [[1]] attitude.",
     "blanks":{"0":{"a":"1100-1200 RPM","kind":"num","alts":["1100 to 1200 rpm","1100-1200"]},"1":{"a":"Vy","kind":"word","alts":["vy"]}}},
    # ---- Maneuvers ----
    {"cat":"Maneuvers","tmpl":"Steep turns: slow to [[0]], roll into [[1]] of bank, lead rollout by [[2]].",
     "blanks":{"0":{"a":"Va","kind":"word","alts":["va"]},"1":{"a":"45/50 degrees","kind":"num","alts":["45-50 degrees","45/50","45 50"]},"2":{"a":"20 degrees","kind":"num","alts":["20"]}}},
    {"cat":"Maneuvers","tmpl":"Slow flight & stalls set up at power [[0]]; power-on stall uses full power at [[1]].",
     "blanks":{"0":{"a":"1500 RPM","kind":"num"},"1":{"a":"75 KIAS","kind":"num"}}},
    {"cat":"Maneuvers","tmpl":"Chandelle: roll into [[0]] of bank, full power; at 90 degrees about [[1]] nose high and [[2]].",
     "blanks":{"0":{"a":"30 degrees","kind":"num","alts":["30"]},"1":{"a":"18 degrees","kind":"num","alts":["18"]},"2":{"a":"75 KIAS","kind":"num"}}},
    {"cat":"Instrument","tmpl":"Holding airspeed is [[0]]; cross the FAF at [[1]] with final approach at [[2]].",
     "blanks":{"0":{"a":"110 KIAS","kind":"num"},"1":{"a":"100 KIAS","kind":"num"},"2":{"a":"100 KIAS","kind":"num"}}},
    {"cat":"Emergency","tmpl":"Emergency landing: glide at Vglide, and if no prepared surface pull CAPS no lower than [[0]].",
     "blanks":{"0":{"a":"2000 ft AGL","kind":"num","alts":["2000 agl","2,000 ft agl","2000"]}}},
    # ---- Cautions (mostly word recall) ----
    {"cat":"Cautions","tmpl":"Leave [[0]] off during engine start to avoid high electrical loads.",
     "blanks":{"0":{"a":"alternators","kind":"word","alts":["alternator"]}}},
    {"cat":"Cautions","tmpl":"Weight and balance is critical because the design is [[0]].",
     "blanks":{"0":{"a":"nose-heavy","kind":"word","alts":["nose heavy","noseheavy"]}}},
    {"cat":"Cautions","tmpl":"[[0]] and spins are prohibited, and so is flight into known [[1]].",
     "blanks":{"0":{"a":"aerobatics","kind":"word","alts":["acro","aerobatic"]},"1":{"a":"icing","kind":"word","alts":["ice"]}}},
    {"cat":"Cautions","tmpl":"Call '[[0]]' passing 500 ft AGL after takeoff and 'Negative CAPS' on approach.",
     "blanks":{"0":{"a":"CAPS available","kind":"word","alts":["caps available"]}}},
    {"cat":"Cautions","tmpl":"Spin recovery can only be accomplished by [[0]].",
     "blanks":{"0":{"a":"CAPS deployment","kind":"word","alts":["caps","caps deploy","deploying caps"]}}},
]

# Registry of training material.  Add new decks here.
DECKS = {
    "sr20": {"name": "Cirrus SR20 Reference", "cards": DECK},
}

# Pool of all real numeric answers per deck, for multiple-choice distractors.
NUM_POOLS = {d: sorted({b["a"] for c in info["cards"] for b in c["blanks"].values() if b["kind"]=="num"})
             for d, info in DECKS.items()}

# ---------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
    con.execute("""CREATE TABLE IF NOT EXISTS progress(
        user_id INTEGER, deck TEXT, card_id INTEGER,
        box INTEGER DEFAULT 1, last_session INTEGER DEFAULT 0,
        seen INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, deck, card_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        user_id INTEGER, deck TEXT, session INTEGER DEFAULT 1,
        PRIMARY KEY(user_id, deck))""")
    # One-time migration from the original single-user schema.
    legacy = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cards'").fetchone()
    if legacy:
        con.execute("INSERT OR IGNORE INTO users(name) VALUES('Tim')")
        uid = con.execute("SELECT id FROM users WHERE name='Tim'").fetchone()["id"]
        con.execute("""INSERT OR IGNORE INTO progress(user_id,deck,card_id,box,last_session,seen,correct)
                       SELECT ?, 'sr20', id, box, last_session, seen, correct FROM cards""", (uid,))
        s = con.execute("SELECT v FROM meta WHERE k='session'").fetchone()
        con.execute("INSERT OR IGNORE INTO sessions(user_id,deck,session) VALUES(?,'sr20',?)",
                    (uid, int(s["v"]) if s else 1))
        con.execute("ALTER TABLE cards RENAME TO cards_legacy")
        con.execute("ALTER TABLE meta RENAME TO meta_legacy")
    con.commit(); con.close()

def ensure_rows(uid, deck):
    """Make sure this user has progress rows + a session counter for this deck."""
    con = db()
    for i in range(len(DECKS[deck]["cards"])):
        con.execute("INSERT OR IGNORE INTO progress(user_id,deck,card_id) VALUES(?,?,?)", (uid, deck, i))
    con.execute("INSERT OR IGNORE INTO sessions(user_id,deck,session) VALUES(?,?,1)", (uid, deck))
    con.commit(); con.close()

def get_session(uid, deck):
    con=db(); r=con.execute("SELECT session FROM sessions WHERE user_id=? AND deck=?", (uid,deck)).fetchone(); con.close()
    return r["session"] if r else 1

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

def make_choices(answer, deck):
    pool_all = NUM_POOLS[deck]
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

# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/users")
def api_users():
    con=db(); rows=con.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall(); con.close()
    return jsonify({"users":[{"id":r["id"],"name":r["name"]} for r in rows]})

@app.route("/api/users", methods=["POST"])
def api_add_user():
    name=(request.get_json().get("name") or "").strip()
    if not name: return jsonify({"error":"name required"}), 400
    con=db()
    try:
        cur=con.execute("INSERT INTO users(name) VALUES(?)", (name,)); con.commit()
        uid=cur.lastrowid
    except sqlite3.IntegrityError:          # name exists -> just select that user
        uid=con.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()["id"]
    con.close()
    return jsonify({"id":uid,"name":name})

@app.route("/api/decks")
def api_decks():
    return jsonify({"decks":[{"id":k,"name":v["name"],"count":len(v["cards"])} for k,v in DECKS.items()]})

@app.route("/api/next")
def api_next():
    uid=int(request.args["user"]); deck=request.args.get("deck","sr20")
    if deck not in DECKS: return jsonify({"error":"unknown deck"}), 400
    ensure_rows(uid, deck)
    cards_def=DECKS[deck]["cards"]
    session=get_session(uid, deck)
    con=db(); rows=con.execute("SELECT * FROM progress WHERE user_id=? AND deck=?", (uid,deck)).fetchall(); con.close()
    due=[r for r in rows if (session - r["last_session"]) >= CADENCE[r["box"]]]
    if not due:
        return jsonify({"done":True,"session":session,
                        "next_due_in":min(CADENCE[r["box"]]-(session-r["last_session"]) for r in rows)})
    r=random.choice(due)
    card=cards_def[r["card_id"]]
    bid=random.choice(list(card["blanks"].keys()))   # MOVING BLANK
    blank=card["blanks"][bid]
    # render sentence with chosen blank hidden, others shown
    def render(txt):
        for k,b in card["blanks"].items():
            token = "_____" if k==bid else b["a"]
            txt = txt.replace(f"[[{k}]]", f"<b>{token}</b>")
        return txt
    # Numbers start as multiple choice; once the card reaches Box 3+
    # ("Familiar") they graduate to type-in.  Words are always type-in.
    mode = "mc" if (blank["kind"]=="num" and r["box"] < 3) else "type"
    payload={"done":False,"id":r["card_id"],"blank_id":bid,"cat":card["cat"],
             "box":r["box"],"box_label":BOX_LABEL[r["box"]],
             "sentence":render(card["tmpl"]),"kind":blank["kind"],"mode":mode,
             "session":session,"due_count":len(due)}
    if mode=="mc":
        payload["choices"]=make_choices(blank["a"], deck)
    return jsonify(payload)

@app.route("/api/answer", methods=["POST"])
def api_answer():
    d=request.get_json()
    deck=d.get("deck","sr20")
    cid=int(d["id"]); bid=d["blank_id"]; given=(d.get("answer") or "").strip()
    blank=DECKS[deck]["cards"][cid]["blanks"][bid]
    accepted=[blank["a"].lower()]+[a.lower() for a in blank.get("alts",[])]
    norm=lambda s: re.sub(r"\s+"," ",s.lower().strip())
    correct = norm(given) in [norm(a) for a in accepted]
    if not correct and blank["kind"]=="num":   # lenient: bare number, units optional
        g=num_core(given)
        correct = g is not None and g in {num_core(a) for a in accepted}
    return jsonify({"correct":correct,"answer":blank["a"]})

@app.route("/api/grade", methods=["POST"])
def api_grade():
    d=request.get_json()
    uid=int(d["user"]); deck=d.get("deck","sr20"); cid=int(d["id"]); g=d["grade"]
    session=get_session(uid, deck)
    con=db(); r=con.execute("SELECT * FROM progress WHERE user_id=? AND deck=? AND card_id=?",
                            (uid,deck,cid)).fetchone()
    box=r["box"]; correct=r["correct"]
    if g=="again": box=1
    elif g=="hard": pass
    elif g=="good": box=min(5,box+1); correct+=1
    elif g=="easy": box=min(5,box+2); correct+=1
    con.execute("""UPDATE progress SET box=?, seen=seen+1, correct=?, last_session=?
                   WHERE user_id=? AND deck=? AND card_id=?""",
                (box,correct,session,uid,deck,cid)); con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/session/advance", methods=["POST"])
def api_advance():
    d=request.get_json(); uid=int(d["user"]); deck=d.get("deck","sr20")
    con=db(); con.execute("UPDATE sessions SET session=session+1 WHERE user_id=? AND deck=?", (uid,deck))
    con.commit(); con.close()
    return jsonify({"session":get_session(uid,deck)})

@app.route("/api/stats")
def api_stats():
    uid=int(request.args["user"]); deck=request.args.get("deck","sr20")
    if deck not in DECKS: return jsonify({"error":"unknown deck"}), 400
    ensure_rows(uid, deck)
    cards_def=DECKS[deck]["cards"]
    con=db(); rows=con.execute("SELECT * FROM progress WHERE user_id=? AND deck=? ORDER BY card_id",
                               (uid,deck)).fetchall(); con.close()
    counts={b:0 for b in range(1,6)}
    for r in rows: counts[r["box"]]+=1
    seen=sum(1 for r in rows if r["seen"]>0)
    ts=sum(r["seen"] for r in rows); tc=sum(r["correct"] for r in rows)
    cards=[{"id":r["card_id"],"cat":cards_def[r["card_id"]]["cat"],
            "sentence":re.sub(r"\[\[(\w+)\]\]", lambda m:f'[{cards_def[r["card_id"]]["blanks"][m.group(1)]["a"]}]', cards_def[r["card_id"]]["tmpl"]),
            "box":r["box"],"seen":r["seen"],"correct":r["correct"]} for r in rows]
    return jsonify({"counts":counts,"box_label":BOX_LABEL,"cadence":CADENCE,
                    "seen":seen,"total":len(rows),"mastered":counts[5],
                    "accuracy":round(tc/ts*100) if ts else 0,"session":get_session(uid,deck),
                    "cards":cards})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    d=request.get_json(); uid=int(d["user"]); deck=d.get("deck","sr20")
    con=db()
    con.execute("UPDATE progress SET box=1,last_session=0,seen=0,correct=0 WHERE user_id=? AND deck=?", (uid,deck))
    con.execute("UPDATE sessions SET session=1 WHERE user_id=? AND deck=?", (uid,deck))
    con.commit(); con.close()
    return jsonify({"ok":True})

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trainer</title>
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
select.mini{appearance:none}
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
.sentence b{color:var(--ink);font-weight:800}
.sentence b._blank{color:var(--accent);letter-spacing:2px}
.choices{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:26px}
.choices button{font-family:'Spline Sans Mono',monospace;font-size:16px;padding:16px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);cursor:pointer;transition:.12s}
.choices button:hover{border-color:var(--accent2)}
.choices button.right{background:var(--good);color:#06150c;border-color:var(--good)}
.choices button.wrong{background:var(--again);color:#1a0606;border-color:var(--again)}
.typein{display:flex;gap:8px;margin-top:24px}
.typein input{flex:1;font-family:'Spline Sans Mono',monospace;font-size:17px;padding:15px;border-radius:12px;border:1px solid var(--line);background:#0c1318;color:var(--ink)}
.typein input:focus{outline:none;border-color:var(--accent2)}
.typein button{padding:0 22px;border-radius:12px;border:none;background:var(--accent2);color:#04201f;font-weight:700;cursor:pointer}
.verdict{margin-top:18px;font-family:'Spline Sans Mono',monospace;font-size:13px}
.verdict.ok{color:var(--good)}.verdict.no{color:var(--again)}
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
.pill{display:inline-block;font-family:'Spline Sans Mono',monospace;font-size:10px;padding:2px 8px;border-radius:99px;font-weight:600}
.userlist{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin-top:8px}
.userbtn{font-family:'Spline Sans Mono',monospace;font-size:16px;padding:16px 26px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);cursor:pointer}
.userbtn:hover{border-color:var(--accent)}
.hidden{display:none!important}
footer{margin-top:30px;font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--muted);text-align:center;line-height:1.7}
</style></head>
<body><div class="wrap">
<header><div><h1>SR20 <span>Trainer</span></h1><div class="sub">cloze · moving blank · Leitner</div></div>
<div class="ctrls">
<select id="deckSel" class="mini" onchange="setDeck(this.value)"></select>
<button class="mini" id="whoBtn" onclick="showUserPicker()">&#128100; —</button>
<button class="mini" onclick="resetAll()">↺ Reset</button>
</div></header>
<nav><button id="t-study" class="active" onclick="show('study')">Drill</button>
<button id="t-progress" onclick="show('progress')">Progress</button>
<button id="t-cards" onclick="show('cards')">All Facts</button></nav>
<section id="v-user" class="hidden"></section>
<section id="v-study"></section>
<section id="v-progress" class="hidden"></section>
<section id="v-cards" class="hidden"></section>
<footer>Each user's progress saves automatically to sr20_progress.db on disk. Number blanks → multiple choice until a card reaches Box 3, then type-in (bare numbers OK); word blanks → always type-in.<br>Each correct answer auto-grades Good — override with the buttons if it felt harder or easier.</footer>
</div>
<script>
let cur=null, answered=false, USER=null, DECK='sr20', ALL_DECKS=[], currentTab='study';
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const api=(u,m,b)=>{
  let url=u;
  if(USER) url+=(u.includes('?')?'&':'?')+'user='+USER.id+'&deck='+encodeURIComponent(DECK);
  const body=b?JSON.stringify(Object.assign({user:USER?USER.id:null,deck:DECK},b)):undefined;
  return fetch(url,{method:m||'GET',headers:{'Content-Type':'application/json'},body}).then(r=>r.json());
};

// ---------- users & decks ----------
async function boot(){
  ALL_DECKS=(await api('/api/decks')).decks;
  const savedDeck=localStorage.getItem('trainer_deck');
  if(savedDeck && ALL_DECKS.some(d=>d.id===savedDeck)) DECK=savedDeck;
  const sel=document.getElementById('deckSel');
  sel.innerHTML=ALL_DECKS.map(d=>`<option value="${esc(d.id)}">${esc(d.name)} (${d.count})</option>`).join('');
  sel.value=DECK;
  const users=(await api('/api/users')).users;
  const saved=localStorage.getItem('trainer_user');
  if(saved){
    const p=JSON.parse(saved);
    const hit=users.find(x=>x.id===p.id);
    if(hit){USER=hit; updateHeader(); show('study'); return;}
  }
  showUserPicker(users);
}
async function showUserPicker(users){
  if(!users) users=(await api('/api/users')).users;
  ['study','progress','cards'].forEach(x=>document.getElementById('v-'+x).classList.add('hidden'));
  const v=document.getElementById('v-user'); v.classList.remove('hidden');
  v.innerHTML=`<div class="card" style="text-align:center">
    <div class="face-label">Who's studying?</div>
    <div class="userlist">${users.map(u=>`<button class="userbtn" data-id="${u.id}" data-name="${esc(u.name)}"
      onclick="pickUser(+this.dataset.id,this.dataset.name)">${esc(u.name)}</button>`).join('')
      ||'<p class="sub">No users yet — add one below.</p>'}</div>
    <div class="typein" style="max-width:420px;margin:26px auto 0">
      <input id="nu" placeholder="new user name…" autocomplete="off" onkeydown="if(event.key==='Enter')addUser()">
      <button onclick="addUser()">Add</button></div></div>`;
  document.getElementById('nu').focus();
}
function pickUser(id,name){
  USER={id,name}; localStorage.setItem('trainer_user',JSON.stringify(USER));
  document.getElementById('v-user').classList.add('hidden');
  updateHeader(); show('study');
}
async function addUser(){
  const n=document.getElementById('nu').value.trim(); if(!n)return;
  const r=await api('/api/users','POST',{name:n});
  pickUser(r.id,r.name);
}
function updateHeader(){
  document.getElementById('whoBtn').innerHTML='&#128100; '+esc(USER.name);
  document.getElementById('deckSel').value=DECK;
}
function setDeck(d){
  DECK=d; localStorage.setItem('trainer_deck',d);
  if(USER) show(currentTab);
}

// ---------- tabs ----------
function show(t){
  if(!USER){showUserPicker();return;}
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
  if(d.done){
    v.innerHTML=`<div class="done"><h2>All caught up ✈</h2>
      <p>Nothing due for ${esc(USER.name)} in session #${d.session}.</p>
      <p style="margin-top:12px">Next cards unlock in ${d.next_due_in} session(s).</p>
      <div style="margin-top:22px"><button class="mini" onclick="advance()">Advance session →</button></div></div>`;
    return;}
  const sent=d.sentence.replace(/<b>_____<\/b>/,'<b class="_blank">_____</b>');
  let answerUI;
  if(d.mode==='mc'){
    answerUI=`<div class="choices">${d.choices.map(c=>`<button onclick="pick(this,'${c.replace(/'/g,"\\'")}')">${c}</button>`).join('')}</div>`;
  }else{
    answerUI=`<div class="typein"><input id="ti" placeholder="${d.kind==='num'?'type the missing value…':'type the missing word…'}" autocomplete="off"
      onkeydown="if(event.key==='Enter')submitType()"><button onclick="submitType()">Check</button></div>`;
  }
  const modeLabel=d.mode==='mc'?'multiple choice':(d.kind==='num'?'type-in · graduated':'type-in');
  v.innerHTML=`<div class="meta"><span>${d.due_count} due · session #${d.session}</span><span>${modeLabel}</span></div>
    <div class="card"><div class="cat-tag">${d.cat}</div><div class="box-pip">Box ${d.box} · ${d.box_label}</div>
    <div class="face-label">Fill the blank</div><div class="sentence">${sent}</div>${answerUI}
    <div class="verdict" id="verdict"></div><div id="gradeRow"></div></div>`;
  if(d.mode==='type')setTimeout(()=>document.getElementById('ti').focus(),50);
}

async function pick(btn,val){ if(answered)return; await resolve(val,btn); }
async function submitType(){ if(answered)return; const val=document.getElementById('ti').value; await resolve(val,null); }

async function resolve(val,btn){
  answered=true;
  const r=await api('/api/answer','POST',{id:cur.id,blank_id:cur.blank_id,answer:val});
  const vd=document.getElementById('verdict');
  if(cur.mode==='mc'){
    document.querySelectorAll('.choices button').forEach(b=>{
      if(b.textContent===r.answer)b.classList.add('right');
      else if(b===btn&&!r.correct)b.classList.add('wrong');
      b.disabled=true;});
  }else{
    const inp=document.getElementById('ti'); inp.disabled=true;
  }
  vd.className='verdict '+(r.correct?'ok':'no');
  vd.textContent=r.correct?'✓ Correct':'✗ Answer: '+r.answer;
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
  v.innerHTML=`<div class="boxes">${[1,2,3,4,5].map(b=>`<div class="boxcell">
    <div class="n bar${b}">${d.counts[b]}</div><div class="l">${d.box_label[b]}</div>
    <div class="cad">every ${d.cadence[b]} sess</div></div>`).join('')}</div>
    <div class="stats">
      <div class="stat"><div class="v">${d.mastered}/${d.total}</div><div class="k">Mastered</div></div>
      <div class="stat"><div class="v">${d.seen}/${d.total}</div><div class="k">Touched</div></div>
      <div class="stat"><div class="v">${d.accuracy}%</div><div class="k">Accuracy</div></div>
      <div class="stat"><div class="v">${d.session}</div><div class="k">Sessions</div></div>
    </div>
    <div class="progress-line"><div class="progress-fill" style="width:${Math.round(d.mastered/d.total*100)}%"></div></div>
    <div class="meta"><span>Mastery — ${esc(USER.name)}</span><span>${Math.round(d.mastered/d.total*100)}%</span></div>`;
}
async function loadCards(){
  const d=await api('/api/stats'); const v=document.getElementById('v-cards');
  const rows=d.cards.map(c=>{const acc=c.seen?Math.round(c.correct/c.seen*100)+'%':'—';
    return `<tr><td><span class="pill" style="background:var(--b${c.box});color:#0b1014">B${c.box}</span></td>
    <td>${c.sentence}</td><td style="color:var(--muted);font-family:'Spline Sans Mono',monospace;font-size:11px">${c.seen}× · ${acc}</td></tr>`;}).join('');
  v.innerHTML=`<table><thead><tr><th>Box</th><th>Fact</th><th>Reviews</th></tr></thead><tbody>${rows}</tbody></table>`;
}
async function resetAll(){
  if(!USER)return;
  if(confirm(`Reset ${USER.name}'s progress on this material?`)){await api('/api/reset','POST',{});show('study');}
}
boot();
</script></body></html>
"""

init_db()

if __name__=="__main__":
    init_db()
    print("Trainer running at http://127.0.0.1:5000  (Ctrl-C to stop)")
    app.run(debug=False, port=5000)
