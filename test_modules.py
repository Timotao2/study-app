"""Module upload / validation / progress tests.  Run:  python test_modules.py

Self-contained: builds a scratch database in the OLD schema (progress keyed by
list index) to exercise the one-time migration, then drives the admin API and
the trainer API through Flask's test client. If 8210-1d.studybuddy.json sits
next to this file it is validated too (it is git-ignored)."""
import os, sys, json, copy, sqlite3, time
HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "_test_modules.db")
if os.path.exists(SCRATCH): os.remove(SCRATCH)

# --- legacy-schema fixture: what a pre-modules database looked like ---------------
con = sqlite3.connect(SCRATCH)
con.executescript("""
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE progress(user_id INTEGER, deck TEXT, card_id INTEGER, box INTEGER DEFAULT 1,
    last_session INTEGER DEFAULT 0, seen INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, deck, card_id));
CREATE TABLE sessions(user_id INTEGER, deck TEXT, session INTEGER DEFAULT 1, PRIMARY KEY(user_id, deck));
INSERT INTO users(id,name) VALUES(1,'Tim');
INSERT INTO sessions VALUES(1,'sr20',3);
""")
LEGACY = [(i, 1 + i % 5, 2 + i % 3, 2, 1 + i % 3) for i in range(36)]   # (idx, box, seen, last_session, correct)
con.executemany("INSERT INTO progress(user_id,deck,card_id,box,seen,last_session,correct) VALUES(1,'sr20',?,?,?,?,?)",
                [(i, b, s, ls, c) for i, b, s, ls, c in LEGACY])
con.commit(); con.close()

os.environ["SECRET_KEY"] = "test-secret"; os.environ["ORIGIN"] = "http://localhost"; os.environ["RP_ID"] = "localhost"
os.environ["BLOCK_COUNTRIES"] = "CN,RU,IN"
import app as trainer
trainer.DB = SCRATCH
trainer.init_db(); trainer.auth.init_auth_db(); trainer.modules.init_modules_db()
import auth, modules, pyotp
modules._cache["rev"] = None              # forget anything cached from the real DB during import
app = trainer.app; app.testing = True

fails = 0
def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg); fails += 0 if cond else 1

def code_for(username):
    c = auth.db(); s = c.execute("SELECT totp_secret FROM accounts WHERE username=?", (username,)).fetchone()["totp_secret"]; c.close()
    return pyotp.TOTP(s).now()

def login(client, name, admin=False):
    tok = auth.create_invite(name, is_admin=admin)
    r = client.post(f"/auth/enroll/{tok}/totp", json={"code": code_for(name)})
    assert r.status_code == 200, r.data
    return client

def mc(cid, q="Question?", answer="Right", distractors=("Wrong A", "Wrong B"), **extra):
    c = {"id": cid, "type": "mc", "cat": "Cat", "ref": "ref 1.2", "q": q, "answer": answer,
         "distractors": list(distractors), "explanation": "Because."}
    c.update(extra); return c

def module(mid="mctest", cards=None, **extra):
    m = {"format": modules.FORMAT, "id": mid, "name": "MC Test", "version": "1", "source": "unit test",
         "cards": cards if cards is not None else [mc("a"), mc("b"), mc("c", distractors=["x"])]}
    m.update(extra); return m

# ---------------------------------------------------------------------------------
print("legacy migration (progress keyed by list index -> card id)")
con = trainer.db()
check(trainer._card_id_type(con) == "TEXT", "progress.card_id is TEXT now")
check(con.execute("SELECT COUNT(*) FROM progress_legacy_index").fetchone()[0] == 36, "old table kept as backup")
rows = {r["card_id"]: r for r in con.execute("SELECT * FROM progress WHERE user_id=1 AND deck='sr20'")}
check(len(rows) == 36 and set(rows) == set(trainer.LEGACY_SR20_IDS), "36 rows, keyed by the SR20 card ids")
good = all(rows[trainer.LEGACY_SR20_IDS[i]]["box"] == b and rows[trainer.LEGACY_SR20_IDS[i]]["seen"] == s
           and rows[trainer.LEGACY_SR20_IDS[i]]["last_session"] == ls and rows[trainer.LEGACY_SR20_IDS[i]]["correct"] == c
           for i, b, s, ls, c in LEGACY)
check(good, "every row's box/seen/last_session/correct survived, mapped index -> id")
check(con.execute("SELECT session FROM sessions WHERE user_id=1 AND deck='sr20'").fetchone()[0] == 3, "session counter untouched")
check(con.execute("SELECT card_count, uploaded_by FROM modules WHERE id='sr20'").fetchone()[:] == (36, "bundled"), "SR20 seeded from modules/sr20.studybuddy.json")
con.close()
trainer.init_db(); modules.init_modules_db()
con = trainer.db(); check(con.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 36 and con.execute("SELECT COUNT(*) FROM modules").fetchone()[0] == 1, "second start-up is a no-op"); con.close()
sr20 = json.load(open(os.path.join(HERE, "modules", "sr20.studybuddy.json"), encoding="utf-8"))
check([c["id"] for c in sr20["cards"]] == trainer.LEGACY_SR20_IDS, "bundled file's card order matches LEGACY_SR20_IDS")

print("validator")
V = modules.validate_module
import html as _html
def errs(rep): return [(e["card"], _html.unescape(e["msg"])) for e in rep["errors"]]   # report text is escaped for the page
def has(rep, card, frag): return any(c == card and frag in m for c, m in errs(rep))
r = V(module()); check(r["ok"] and r["summary"]["count"] == 3 and r["summary"]["types"] == {"mc": 3} and len(r["preview"]) == 3, "valid mc module -> ok, summary, preview")
r = V(module(cards=[mc("a", type="essay")])); check(has(r, "a", "unknown type"), "unknown type reported by card id")
c = mc("a"); del c["q"]; r = V(module(cards=[c])); check(has(r, "a", 'missing field "q"'), "missing field")
c = mc("a"); del c["id"]; r = V(module(cards=[c])); check(has(r, "card #1", 'missing field "id"'), "missing id reported by position")
r = V(module(cards=[mc("a"), mc("a")])); check(has(r, "a", "duplicate id"), "duplicate id")
r = V(module(cards=[mc("a", answer="Right", distractors=["right ", "B"])])); check(has(r, "a", "answer repeated in distractors"), "answer repeated in distractors (case/space-insensitive)")
r = V(module(cards=[mc("a", distractors=["B", "b"])])); check(has(r, "a", "duplicate distractor"), "duplicate distractor")
r = V(module(cards=[mc("a", distractors=[])])); check(has(r, "a", "1–5"), "no distractors")
r = V(module(cards=[mc("a", distractors=list("abcdef"))])); check(has(r, "a", "1–5"), "six distractors")
r = V(module(cards=[mc("a", distractors=["x", 3])])); check(has(r, "a", "non-empty string"), "non-string distractor")
r = V(module(cards=[mc("a", note=5)])); check(has(r, "a", '"note" must be a string'), "note must be a string")
cl = {"id": "c1", "type": "cloze", "cat": "V", "tmpl": "Vr is [[0]].", "blanks": {"0": {"a": "67 KIAS", "kind": "num"}}}
r = V(module(cards=[cl])); check(r["ok"] and r["summary"]["types"] == {"cloze": 1} and "<b>[67 KIAS]</b>" in r["preview"][0]["text"], "valid cloze card")
bad = copy.deepcopy(cl); bad["blanks"]["1"] = {"a": "x", "kind": "word"}; r = V(module(cards=[bad])); check(has(r, "c1", "never appears as [[1]]"), "blank without marker")
bad = copy.deepcopy(cl); bad["tmpl"] = "Vr is [[0]] at [[9]]."; r = V(module(cards=[bad])); check(has(r, "c1", "[[9]] has no entry"), "marker without blank")
bad = copy.deepcopy(cl); bad["blanks"]["0"]["kind"] = "number"; r = V(module(cards=[bad])); check(has(r, "c1", '"kind" must be'), "bad kind")
bad = copy.deepcopy(cl); bad["blanks"] = {}; r = V(module(cards=[bad])); check(has(r, "c1", "non-empty object"), "empty blanks")
r = V(module(format="studybuddy-module/2")); check(has(r, "module", '"format" must be'), "wrong format")
r = V(module(mid="bad id!")); check(has(r, "module", '"id" must be'), "bad module id")
r = V(module(cards=[])); check(has(r, "module", "non-empty list"), "no cards")
r = V([1, 2]); check(has(r, "module", "JSON object"), "top level not an object")
r = V(module(cards=[mc("a", tags=["x"])], extra=1)); check(r["ok"] and len(r["warnings"]) == 2, "unknown fields -> warnings, still ok")
r = V(module(cards=[mc("<img>")])); check(any("&lt;img&gt;" in c for c, m in errs(r)) and not any("<img>" in c for c, m in errs(r)), "report text is HTML-escaped")
r = V(module(cards=[mc("a", q="1 < 2 & \"x\"")])); check("1 &lt; 2 &amp; &quot;x&quot;" == r["preview"][0]["text"], "preview text is HTML-escaped")

REAL = os.path.join(HERE, "8210-1d.studybuddy.json")
if os.path.exists(REAL):
    real = json.load(open(REAL, encoding="utf-8")); r = V(real)
    check(r["ok"] and r["summary"]["count"] == 73 and r["summary"]["types"] == {"mc": 73} and not r["errors"], f"8210-1d.studybuddy.json validates: {r['summary']['count']} cards, {len(r['warnings'])} warnings")
else:
    real = None; print("  (8210-1d.studybuddy.json not present — skipped)")

print("admin API access control")
anon = app.test_client()
check(anon.post("/admin/modules/validate", json={"module": module()}).status_code == 401, "anonymous -> 401")
tim = login(app.test_client(), "Tim", admin=True)
eva = login(app.test_client(), "Eva")
check(eva.post("/admin/modules/validate", json={"module": module()}).status_code == 403, "non-admin validate -> 403")
check(eva.post("/admin/modules/publish", json={"module": module()}).status_code == 403, "non-admin publish -> 403")
check(eva.get("/admin/modules").status_code == 403, "non-admin list -> 403")
check(eva.get("/admin/modules/sr20.json").status_code == 403, "non-admin download -> 403")
r = tim.get("/admin"); check(r.status_code == 200 and b"<h2>Modules</h2>" in r.data and b"sr20" in r.data and b"modFile" in r.data, "admin page shows the Modules section with sr20 listed")

print("publish, drill and grade an mc module (with hostile text)")
nasty = mc("n1", q="Is 1 < 2 & \"yes\"? <script>alert(1)</script>", answer="Yes <b>bold</b>", distractors=["No & never", "Maybe 'so'"],
           explanation="Expl <i>x</i>", note="Note & more", quote="Quoted \"text\" <u>u</u>", ref="Ref <3")
m1 = module(cards=[mc("a"), mc("b"), nasty])
r = tim.post("/admin/modules/validate", json={"module": m1}); j = r.get_json()
check(r.status_code == 200 and j["ok"] and j["replace"] is None, "validate endpoint: ok, not a replacement")
r = tim.post("/admin/modules/publish", json={"module": m1}); j = r.get_json()
check(r.status_code == 200 and j["ok"] and j["published"]["count"] == 3, "publish endpoint")
d = eva.get("/api/decks").get_json()["decks"]
check([x["id"] for x in d] == ["sr20", "mctest"] or sorted(x["id"] for x in d) == ["mctest", "sr20"], f"module in everyone's dropdown: {[x['id'] for x in d]}")
seen_types = set(); nasty_seen = False
for _ in range(40):
    n = eva.get("/api/next?deck=mctest").get_json()
    if n.get("done"): eva.post("/api/session/advance", json={"deck": "mctest"}); continue
    seen_types.add(n["type"])
    if n["id"] == "n1":
        nasty_seen = True
        check(n["mode"] == "mc" and len(n["choices"]) == 3 and "&lt;script&gt;" in n["question"] and "<script>" not in json.dumps(n), "hostile question is escaped in /api/next")
        check(all("<" not in c for c in n["choices"]) and "Yes &lt;b&gt;bold&lt;/b&gt;" in n["choices"], "choices escaped, answer among them")
        right = "Yes &lt;b&gt;bold&lt;/b&gt;"
        a = eva.post("/api/answer", json={"deck": "mctest", "id": "n1", "answer": right}).get_json()
        check(a["correct"] and a["answer"] == right and a["explanation"] == "Expl &lt;i&gt;x&lt;/i&gt;" and a["note"] == "Note &amp; more"
              and a["quote"].startswith("Quoted &quot;text&quot;") and a["ref"] == "Ref &lt;3", "answer accepted; explanation/note/quote/ref returned escaped")
        a2 = eva.post("/api/answer", json={"deck": "mctest", "id": "n1", "answer": "No &amp; never"}).get_json()
        check(a2["correct"] is False and a2["answer"] == right, "wrong choice -> correct=false, right answer shown")
        g = eva.post("/api/grade", json={"deck": "mctest", "id": "n1", "grade": "good"}).get_json(); check(g["ok"], "grade good")
        break
    eva.post("/api/grade", json={"deck": "mctest", "id": n["id"], "grade": "hard"})
check(nasty_seen and seen_types == {"mc"}, "mc cards served as multiple choice only")
st = eva.get("/api/stats?deck=mctest").get_json()
check(st["total"] == 3 and next(c for c in st["cards"] if c["id"] == "n1")["box"] == 2 and "<script>" not in json.dumps(st) and "&lt;script&gt;" in json.dumps(st), "stats: 3 cards, n1 in box 2, escaped")
check(eva.post("/api/grade", json={"deck": "mctest", "id": "zzz", "grade": "good"}).status_code == 400, "unknown card id -> 400")
check(eva.post("/api/answer", json={"deck": "mctest", "id": 0, "answer": "x"}).status_code == 400, "numeric index no longer addresses a card")

print("re-upload keeps progress on unchanged ids")
m2 = module(cards=[mc("a"), nasty, mc("d")], version="2")      # b removed, d added, a + n1 kept
r = tim.post("/admin/modules/validate", json={"module": m2}); j = r.get_json()
check(j["ok"] and j["replace"]["kept"] == 2 and j["replace"]["added"] == 1 and j["replace"]["removed"] == 1 and j["replace"]["users"] == 1, f"replace report: {j['replace']}")
r = tim.post("/admin/modules/publish", json={"module": m2}); check(r.status_code == 200, "replace published")
st = eva.get("/api/stats?deck=mctest").get_json(); by = {c["id"]: c for c in st["cards"]}
check(set(by) == {"a", "n1", "d"} and by["n1"]["box"] == 2 and by["n1"]["seen"] == 1 and by["d"]["box"] == 1, "n1 progress kept, b gone, d fresh")
con = trainer.db(); check(con.execute("SELECT COUNT(*) FROM progress WHERE deck='mctest' AND card_id='b'").fetchone()[0] == 1, "row for removed card kept in the table (ignored, not lost)"); con.close()
r = tim.get("/admin/modules/mctest.json")
check(r.status_code == 200 and r.get_json() == m2 and "attachment" in r.headers.get("Content-Disposition", ""), "download returns the stored module verbatim")
check(tim.get("/admin/modules/nope.json").status_code == 404 and tim.get("/admin/modules/../x.json").status_code in (400, 404), "download: unknown / bad id")

print("publish refuses invalid modules")
r = tim.post("/admin/modules/publish", json={"module": module(cards=[mc("a"), mc("a")])}); j = r.get_json()
check(r.status_code == 400 and not j["ok"] and "duplicate id" in json.dumps(j["errors"]) and "nothing published" in j["error"], "400 + report, nothing published")
check(tim.get("/admin/modules/mctest.json").get_json() == m2, "existing module untouched")
check(tim.post("/admin/modules/publish", json={"module": "junk"}).status_code == 400, "non-object module -> 400")
check(tim.post("/admin/modules/publish", data="not json", content_type="application/json").status_code == 400, "malformed JSON -> 400")

print("hide / show / delete")
r = tim.post("/admin/modules/hide", json={"id": "mctest", "hidden": True}); check(r.get_json()["hidden"] is True, "hide")
check([x["id"] for x in eva.get("/api/decks").get_json()["decks"]] == ["sr20"], "hidden module gone from a user's dropdown")
check(eva.get("/api/next?deck=mctest").status_code == 400 and eva.get("/api/stats?deck=mctest").status_code == 400, "hidden module not drillable by users")
adm = [x for x in tim.get("/api/decks").get_json()["decks"] if x["id"] == "mctest"]
check(adm and adm[0]["hidden"] and tim.get("/api/next?deck=mctest").status_code == 200, "admin still sees and can drill it")
tim.post("/admin/modules/hide", json={"id": "mctest", "hidden": False})
check(eva.get("/api/next?deck=mctest").status_code == 200, "shown again")
check(tim.post("/admin/modules/hide", json={"id": "ghost", "hidden": True}).status_code == 404, "hide unknown -> 404")
r = tim.post("/admin/modules/delete", json={"id": "mctest"}); check(r.status_code == 200, "delete")
check("mctest" not in [x["id"] for x in tim.get("/api/decks").get_json()["decks"]], "deleted module gone")
con = trainer.db(); check(con.execute("SELECT COUNT(*) FROM progress WHERE deck='mctest'").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM sessions WHERE deck='mctest'").fetchone()[0] == 0, "its progress + session rows wiped"); con.close()
check(tim.post("/admin/modules/delete", json={"id": "mctest"}).status_code == 404, "delete twice -> 404")

print("request size limits")
big = module(mid="big", cards=[mc(f"c{i}", q="Q " * 40, explanation="E " * 60) for i in range(200)])
size = len(json.dumps({"module": big}))
check(size > 64 * 1024, f"fixture is {size // 1024} KB (> 64 KB app-wide limit)")
r = tim.post("/admin/modules/validate", json={"module": big}); check(r.status_code == 200 and r.get_json()["ok"], "upload route accepts it (1 MB limit)")
r = tim.post("/api/answer", json={"deck": "sr20", "id": "v-speeds-01", "blank_id": "0", "answer": "x", "pad": "y" * 70000})
check(r.status_code == 413 and "too large" in r.get_json()["error"].lower(), "same size on /api/answer -> 413 JSON")
huge = module(mid="huge", cards=[mc(f"c{i}", q="Q " * 400, explanation="E " * 600) for i in range(600)])
r = tim.post("/admin/modules/validate", json={"module": huge}); check(r.status_code == 413, f"{len(json.dumps(huge)) // 1024} KB upload -> 413")
if real is not None:
    r = tim.post("/admin/modules/publish", json={"module": real}); j = r.get_json()
    check(r.status_code == 200 and j["published"]["count"] == 73, "8210-1d publishes through the API")
    n = eva.get("/api/next?deck=dcma-8210-1d").get_json()
    check(n["type"] == "mc" and 2 <= len(n["choices"]) <= 6 and "§" in n["cat"] or n["cat"].startswith("Memos"), f"8210 card served: {n['cat']} / {n['id']}")
    tim.post("/admin/modules/delete", json={"id": "dcma-8210-1d"})

print("cache invalidation across workers (revision key, not memory)")
tim.post("/admin/modules/publish", json={"module": module(mid="cache1")})
con = sqlite3.connect(SCRATCH)      # a "second worker": raw SQL, bypassing this process's cache
con.execute("UPDATE modules SET name='Renamed by worker 2' WHERE id='cache1'")
con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('modules_rev','worker2-bump')"); con.commit()
check(next(x["name"] for x in tim.get("/api/decks").get_json()["decks"] if x["id"] == "cache1") == "Renamed by worker 2", "rev bump -> reloaded on next request")
con.execute("UPDATE modules SET name='Silent change' WHERE id='cache1'"); con.commit(); con.close()
check(next(x["name"] for x in tim.get("/api/decks").get_json()["decks"] if x["id"] == "cache1") == "Renamed by worker 2", "no rev bump -> cache still used (one tiny query per request)")
tim.post("/admin/modules/delete", json={"id": "cache1"})

print("legacy SR20 progress via the API (user_id 1 = Tim)")
st = tim.get("/api/stats?deck=sr20").get_json()
check(st["total"] == 36 and st["session"] == 3 and st["seen"] == 36 and {c["id"] for c in st["cards"]} == set(trainer.LEGACY_SR20_IDS), "Tim's 36 cards, session 3, all seen")
check(st["cards"][0]["id"] == "v-speeds-01" and st["cards"][0]["box"] == 1 and "[67 KIAS]" in st["cards"][0]["sentence"], "All-Facts row for v-speeds-01 in module order")
n = None
for _ in range(30):
    n = tim.get("/api/next?deck=sr20").get_json()
    if not n.get("done"): break
    tim.post("/api/session/advance", json={"deck": "sr20"})
check(n and n["type"] == "cloze" and "<b>_____</b>" in n["sentence"] and n["id"] in trainer.LEGACY_SR20_IDS, "cloze card served with a blank")
card = next(c for c in sr20["cards"] if c["id"] == n["id"]); blank = card["blanks"][n["blank_id"]]
a = tim.post("/api/answer", json={"deck": "sr20", "id": n["id"], "blank_id": n["blank_id"], "answer": blank["a"]}).get_json()
check(a["correct"] and a["answer"] == modules.esc(blank["a"]), "typed exact answer accepted")
if n["mode"] == "mc":
    check(modules.esc(blank["a"]) in n["choices"] and 2 <= len(n["choices"]) <= 4, "cloze mc choices include the answer (escaped)")
a = tim.post("/api/answer", json={"deck": "sr20", "id": n["id"], "blank_id": n["blank_id"], "answer": "definitely wrong"}).get_json()
check(a["correct"] is False, "wrong typed answer rejected")
check(tim.post("/api/answer", json={"deck": "sr20", "id": n["id"], "blank_id": "99", "answer": "x"}).status_code == 400, "unknown blank -> 400")

print("edge policy (Cloudflare country header)")
check(anon.get("/", headers={"CF-IPCountry": "CN"}).status_code == 403, "CN -> 403")
check(anon.get("/login", headers={"CF-IPCountry": "ru"}).status_code == 403, "ru (any case) -> 403")
check(anon.get("/api/next", headers={"CF-IPCountry": "IN"}).status_code == 403, "IN -> 403 before auth even runs")
check(anon.get("/login", headers={"CF-IPCountry": "US"}).status_code == 200, "US -> normal")
check(anon.get("/login").status_code == 200, "no header (local dev) -> normal")
check(tim.get("/api/decks", headers={"CF-IPCountry": "CN"}).status_code == 403, "blocked even when logged in")

os.remove(SCRATCH)
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
