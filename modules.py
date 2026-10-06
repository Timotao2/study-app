"""
modules.py — training material as uploadable JSON modules.

An admin picks a module file on /admin, sees a validation report and a
preview, and publishes it. It then appears in everyone's material dropdown.
No code change is needed to add material.

Module format  "studybuddy-module/1":
    {"format": "studybuddy-module/1", "id": "sr20", "name": "Cirrus SR20 Reference",
     "version": "2026-09-24", "source": "where the facts came from", "cards": [...]}

Card types (every card has a stable string "id" — progress is keyed by it, so
re-uploading a module keeps everyone's progress on cards whose id is unchanged):
    cloze  {"id", "type": "cloze", "cat", "tmpl", "blanks"}
           tmpl has [[key]] markers; blanks = {key: {"a": answer, "kind": "num"|"word", "alts": [...]}}
           (identical to the original DECK entries in app.py)
    mc     {"id", "type": "mc", "cat", "ref", "q", "answer", "distractors": [1-5 strings],
            "explanation", optional "note", optional "quote"}
           Always multiple choice. Options = answer + distractors, shuffled per showing.

Storage: table `modules` in the same SQLite file as progress (one row per module,
the JSON stored verbatim). Every request loads the registry through all_modules(),
which re-reads the table only when the `modules_rev` settings row has changed —
every write bumps it — so several PythonAnywhere workers see a publish at once.

Text safety: nothing from a module is trusted. Everything that leaves the server
for the trainer page goes through esc() (HTML-escaped), because the page writes
API fields straight into innerHTML.
"""
import html, json, os, re, secrets, time
from flask import Blueprint, jsonify, g, has_request_context, current_app

import auth

bp = Blueprint("modules", __name__)

FORMAT = "studybuddy-module/1"
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
UPLOAD_MAX_BYTES = 1024 * 1024        # /admin/modules/* only; the app-wide limit stays 64 KB
MAX_CARDS = 5000
MAX_TEXT = 4000                       # per text field
MAX_DISTRACTORS = 5
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")       # module ids: used in URLs and as DB keys
CARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
TMPL_MARK = re.compile(r"\[\[(\w+)\]\]")

# db() is injected by app.py (same pattern as auth.db).
db = None

def esc(s):
    """HTML-escape any module text before it reaches the browser."""
    return html.escape("" if s is None else str(s), quote=True)

def norm(s):
    return " ".join(str(s).lower().split())

# ---------------------------------------------------------------------------
# schema + cache
def init_modules_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS modules(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT, source TEXT,
        data TEXT NOT NULL, card_count INTEGER, hidden INTEGER DEFAULT 0,
        created REAL, updated REAL, uploaded_by TEXT);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    """)
    con.commit(); con.close()
    seed_bundled()

def seed_bundled():
    """First run only: load the SR20 deck that ships in modules/ so existing progress has a module."""
    if auth.get_setting("sr20_seeded"): return
    path = os.path.join(BUNDLED_DIR, "sr20.studybuddy.json")
    if os.path.exists(path) and get_module("sr20", allow_hidden=True) is None:
        with open(path, encoding="utf-8") as f: obj = json.load(f)
        publish_module(obj, uploaded_by="bundled")
    auth.set_setting("sr20_seeded", "1")

_cache = {"rev": None, "mods": {}}

def _rev():
    con = db(); r = con.execute("SELECT v FROM settings WHERE k='modules_rev'").fetchone(); con.close()
    return r["v"] if r else "0"

def _bump():
    """After any write: new revision so every worker reloads, and drop this
    process's (and this request's) cached copy right away."""
    auth.set_setting("modules_rev", f"{time.time():.6f}-{secrets.token_hex(3)}")
    _cache["rev"] = None
    if has_request_context(): g.pop("_modules", None)

def _load_row(r):
    obj = json.loads(r["data"])
    cards = obj["cards"]
    by_id = {c["id"]: c for c in cards}
    num_pool = sorted({b["a"] for c in cards if c["type"] == "cloze"
                       for b in c["blanks"].values() if b["kind"] == "num"})
    types = {}
    for c in cards: types[c["type"]] = types.get(c["type"], 0) + 1
    return {"id": r["id"], "name": r["name"], "version": r["version"] or "", "source": r["source"] or "",
            "hidden": bool(r["hidden"]), "created": r["created"], "updated": r["updated"],
            "uploaded_by": r["uploaded_by"] or "", "cards": cards, "by_id": by_id,
            "num_pool": num_pool, "count": len(cards), "types": types}

def all_modules():
    """id -> loaded module (hidden ones included). Cached per process, keyed by modules_rev."""
    if has_request_context() and "_modules" in g: return g._modules
    rev = _rev()
    if rev != _cache["rev"]:
        con = db(); rows = con.execute("SELECT * FROM modules ORDER BY name COLLATE NOCASE").fetchall(); con.close()
        mods = {}
        for r in rows:
            try: mods[r["id"]] = _load_row(r)
            except Exception as e:       # a corrupt row must not take the whole app down
                print(f"module {r['id']} failed to load: {e}")
        _cache["rev"], _cache["mods"] = rev, mods
    mods = _cache["mods"]
    if has_request_context(): g._modules = mods
    return mods

def visible_modules(is_admin=False):
    return {k: m for k, m in all_modules().items() if is_admin or not m["hidden"]}

def get_module(mid, allow_hidden=False):
    m = all_modules().get(str(mid))
    if m is None or (m["hidden"] and not allow_hidden): return None
    return m

def types_text(types):
    return ", ".join(f"{n} {t}" for t, n in sorted(types.items()))

def summary(m):
    return {"id": m["id"], "name": esc(m["name"]), "version": esc(m["version"]), "source": esc(m["source"]),
            "count": m["count"], "types": m["types"], "types_text": types_text(m["types"]),
            "hidden": m["hidden"], "updated": m["updated"], "uploaded_by": esc(m["uploaded_by"])}

# ---------------------------------------------------------------------------
# validation
def _is_str(v, allow_empty=False):
    return isinstance(v, str) and (allow_empty or v.strip() != "") and len(v) <= MAX_TEXT

def _check_str(errors, where, card, key, allow_empty=False):
    """Append an error unless card[key] is an acceptable string. Returns True if OK."""
    if key not in card: errors.append({"card": where, "msg": f"missing field \"{key}\""}); return False
    v = card[key]
    if not isinstance(v, str): errors.append({"card": where, "msg": f"\"{key}\" must be a string"}); return False
    if not allow_empty and not v.strip(): errors.append({"card": where, "msg": f"\"{key}\" is empty"}); return False
    if len(v) > MAX_TEXT: errors.append({"card": where, "msg": f"\"{key}\" is longer than {MAX_TEXT} characters"}); return False
    return True

CLOZE_KEYS = {"id", "type", "cat", "tmpl", "blanks"}
MC_KEYS = {"id", "type", "cat", "ref", "q", "answer", "distractors", "explanation", "note", "quote"}
MODULE_KEYS = {"format", "id", "name", "version", "source", "cards"}

def _validate_cloze(card, where, errors, warnings):
    ok = True
    for k in ("cat", "tmpl"): ok &= _check_str(errors, where, card, k)
    blanks = card.get("blanks")
    if "blanks" not in card: errors.append({"card": where, "msg": "missing field \"blanks\""}); return False
    if not isinstance(blanks, dict) or not blanks:
        errors.append({"card": where, "msg": "\"blanks\" must be a non-empty object {key: {a, kind}}"}); return False
    for k, b in blanks.items():
        if not re.fullmatch(r"\w+", str(k)):
            errors.append({"card": where, "msg": f"blank key \"{k}\" must be letters/digits"}); ok = False; continue
        if not isinstance(b, dict):
            errors.append({"card": where, "msg": f"blank \"{k}\" must be an object"}); ok = False; continue
        if not _is_str(b.get("a")): errors.append({"card": where, "msg": f"blank \"{k}\": \"a\" (answer) missing or empty"}); ok = False
        if b.get("kind") not in ("num", "word"): errors.append({"card": where, "msg": f"blank \"{k}\": \"kind\" must be \"num\" or \"word\""}); ok = False
        alts = b.get("alts", [])
        if not isinstance(alts, list) or not all(_is_str(a) for a in alts):
            errors.append({"card": where, "msg": f"blank \"{k}\": \"alts\" must be a list of strings"}); ok = False
        for extra in set(b) - {"a", "kind", "alts"}: warnings.append({"card": where, "msg": f"blank \"{k}\": unknown field \"{extra}\" ignored"})
    if isinstance(card.get("tmpl"), str):
        marks = set(TMPL_MARK.findall(card["tmpl"]))
        for k in set(map(str, blanks)) - marks: errors.append({"card": where, "msg": f"blank \"{k}\" never appears as [[{k}]] in tmpl"}); ok = False
        for k in marks - set(map(str, blanks)): errors.append({"card": where, "msg": f"tmpl marker [[{k}]] has no entry in blanks"}); ok = False
    return ok

def _validate_mc(card, where, errors, warnings):
    ok = True
    for k in ("cat", "q", "answer"): ok &= _check_str(errors, where, card, k)
    for k in ("ref", "explanation"): ok &= _check_str(errors, where, card, k, allow_empty=True)
    for k in ("note", "quote"):
        if k in card and not _is_str(card[k], allow_empty=True):
            errors.append({"card": where, "msg": f"\"{k}\" must be a string"}); ok = False
    if "distractors" not in card: errors.append({"card": where, "msg": "missing field \"distractors\""}); return False
    ds = card["distractors"]
    if not isinstance(ds, list) or not (1 <= len(ds) <= MAX_DISTRACTORS):
        errors.append({"card": where, "msg": f"\"distractors\" must be a list of 1–{MAX_DISTRACTORS} strings"}); return False
    if not all(_is_str(d) for d in ds):
        errors.append({"card": where, "msg": "every distractor must be a non-empty string"}); return False
    if isinstance(card.get("answer"), str):
        for d in ds:
            if norm(d) == norm(card["answer"]):
                errors.append({"card": where, "msg": f"answer repeated in distractors: \"{d}\""}); ok = False
    seen = set()
    for d in ds:
        if norm(d) in seen: errors.append({"card": where, "msg": f"duplicate distractor: \"{d}\""}); ok = False
        seen.add(norm(d))
    return ok

def validate_module(obj, existing=None):
    """Strict check. Returns a report; nothing is published while report['errors'] is non-empty.
    All strings in the report are HTML-escaped (the admin page puts them into innerHTML)."""
    errors, warnings, good = [], [], []
    if not isinstance(obj, dict):
        return _report(errors=[{"card": "module", "msg": "top level must be a JSON object"}], warnings=warnings)
    if obj.get("format") != FORMAT:
        errors.append({"card": "module", "msg": f"\"format\" must be \"{FORMAT}\" (got {obj.get('format')!r})"})
    mid = obj.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        errors.append({"card": "module", "msg": "\"id\" must be 1–64 letters, digits, dot, dash or underscore, starting with a letter or digit"})
    if not _is_str(obj.get("name")) or len(obj.get("name", "")) > 120:
        errors.append({"card": "module", "msg": "\"name\" must be a non-empty string (max 120 characters)"})
    for k in ("version", "source"):
        if k in obj and not isinstance(obj[k], (str, int, float)):
            errors.append({"card": "module", "msg": f"\"{k}\" must be a string"})
        elif k not in obj: warnings.append({"card": "module", "msg": f"no \"{k}\" given"})
    for extra in set(obj) - MODULE_KEYS: warnings.append({"card": "module", "msg": f"unknown module field \"{extra}\" ignored"})
    cards = obj.get("cards")
    if not isinstance(cards, list) or not cards:
        errors.append({"card": "module", "msg": "\"cards\" must be a non-empty list"})
        return _report(errors=errors, warnings=warnings)
    if len(cards) > MAX_CARDS:
        errors.append({"card": "module", "msg": f"too many cards ({len(cards)}; max {MAX_CARDS})"})
        return _report(errors=errors, warnings=warnings)
    ids, dup_reported = set(), set()
    for i, c in enumerate(cards):
        where = f"card #{i + 1}"
        if not isinstance(c, dict): errors.append({"card": where, "msg": "card must be a JSON object"}); continue
        cid = c.get("id")
        if isinstance(cid, str) and cid.strip(): where = cid
        ok = True
        if "id" not in c: errors.append({"card": where, "msg": "missing field \"id\""}); ok = False
        elif not isinstance(cid, str) or not CARD_ID_RE.match(cid):
            errors.append({"card": where, "msg": "\"id\" must be 1–80 letters, digits, dot, dash, colon or underscore"}); ok = False
        elif cid in ids:
            if cid not in dup_reported: errors.append({"card": where, "msg": f"duplicate id \"{cid}\""}); dup_reported.add(cid)
            ok = False
        else: ids.add(cid)
        t = c.get("type")
        if "type" not in c: errors.append({"card": where, "msg": "missing field \"type\""}); continue
        if t == "cloze":
            ok &= _validate_cloze(c, where, errors, warnings)
            for extra in set(c) - CLOZE_KEYS: warnings.append({"card": where, "msg": f"unknown field \"{extra}\" ignored"})
        elif t == "mc":
            ok &= _validate_mc(c, where, errors, warnings)
            for extra in set(c) - MC_KEYS: warnings.append({"card": where, "msg": f"unknown field \"{extra}\" ignored"})
        else:
            errors.append({"card": where, "msg": f"unknown type {t!r} (expected \"cloze\" or \"mc\")"}); ok = False
        if ok: good.append(c)
    summ = replace = None
    if isinstance(mid, str) and ID_RE.match(mid) and _is_str(obj.get("name")):
        types, cats = {}, {}
        for c in good:
            types[c["type"]] = types.get(c["type"], 0) + 1
            cats[c["cat"]] = cats.get(c["cat"], 0) + 1
        summ = {"id": mid, "name": esc(obj["name"]), "version": esc(obj.get("version", "")),
                "source": esc(obj.get("source", "")), "count": len(cards), "types": types,
                "types_text": types_text(types), "cats": [esc(k) for k in cats]}
        if existing is None: existing = all_modules().get(mid) if db else None
        if existing is not None:
            old, new = set(existing["by_id"]), ids
            con = db(); users = con.execute("SELECT COUNT(DISTINCT user_id) FROM progress WHERE deck=? AND seen>0", (mid,)).fetchone()[0]; con.close()
            replace = {"name": esc(existing["name"]), "version": esc(existing["version"]), "count": existing["count"],
                       "hidden": existing["hidden"], "kept": len(old & new), "added": len(new - old),
                       "removed": len(old - new), "users": users}
    return _report(errors=errors, warnings=warnings, summary=summ, replace=replace,
                   preview=[preview_card(c) for c in good[:6]])

def _report(errors, warnings, summary=None, replace=None, preview=None):
    e = lambda lst: [{"card": esc(x["card"]), "msg": esc(x["msg"])} for x in lst]
    return {"ok": not errors, "errors": e(errors), "warnings": e(warnings),
            "summary": summary, "replace": replace, "preview": preview or []}

def preview_card(c):
    out = {"id": esc(c["id"]), "cat": esc(c["cat"]), "type": c["type"]}
    if c["type"] == "cloze":
        text = esc(c["tmpl"])
        for k, b in c["blanks"].items(): text = text.replace(f"[[{k}]]", f"<b>[{esc(b['a'])}]</b>")
        out["text"] = text
    else:
        out.update(text=esc(c["q"]), answer=esc(c["answer"]), distractors=[esc(d) for d in c["distractors"]],
                   explanation=esc(c.get("explanation", "")))
    return out

# ---------------------------------------------------------------------------
# writes
def publish_module(obj, uploaded_by=""):
    """Validate and store (insert or replace by id). Raises ValueError(report) on errors."""
    rep = validate_module(obj)
    if not rep["ok"]: raise ValueError(rep)
    mid = obj["id"]
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    con = db(); t = time.time()
    row = con.execute("SELECT id FROM modules WHERE id=?", (mid,)).fetchone()
    args = (obj["name"].strip(), str(obj.get("version", "")), str(obj.get("source", "")), data, len(obj["cards"]), t, uploaded_by)
    if row is None:
        con.execute("""INSERT INTO modules(name,version,source,data,card_count,updated,uploaded_by,created,id)
                       VALUES(?,?,?,?,?,?,?,?,?)""", args + (t, mid))
    else:
        con.execute("""UPDATE modules SET name=?,version=?,source=?,data=?,card_count=?,updated=?,uploaded_by=?
                       WHERE id=?""", args + (mid,))
    con.commit(); con.close()
    _bump()
    return rep

def set_hidden(mid, hidden):
    con = db(); n = con.execute("UPDATE modules SET hidden=? WHERE id=?", (1 if hidden else 0, mid)).rowcount
    con.commit(); con.close(); _bump()
    return n > 0

def delete_module(mid, wipe_progress=True):
    """Remove a module. By default everyone's progress on it goes too (hide it instead to keep progress)."""
    con = db(); n = con.execute("DELETE FROM modules WHERE id=?", (mid,)).rowcount
    if wipe_progress:
        con.execute("DELETE FROM progress WHERE deck=?", (mid,))
        con.execute("DELETE FROM sessions WHERE deck=?", (mid,))
    con.commit(); con.close(); _bump()
    return n > 0

def module_json(mid):
    con = db(); r = con.execute("SELECT data FROM modules WHERE id=?", (mid,)).fetchone(); con.close()
    return r["data"] if r else None

# ---------------------------------------------------------------------------
# admin API  (JSON in, JSON out — same post() pattern as the rest of /admin)
def _body_module():
    d = auth.json_body()
    return d.get("module")

@bp.get("/admin/modules")
@auth.admin_required
def admin_modules_list():
    return jsonify({"modules": [summary(m) for m in all_modules().values()]})

@bp.post("/admin/modules/validate")
@auth.admin_required
def admin_modules_validate():
    return jsonify(validate_module(_body_module()))

@bp.post("/admin/modules/publish")
@auth.admin_required
def admin_modules_publish():
    obj = _body_module()
    try:
        rep = publish_module(obj, uploaded_by=g.account["username"])
    except ValueError as e:
        rep = e.args[0]; rep["error"] = f"{len(rep['errors'])} error(s) — nothing published."
        return jsonify(rep), 400
    rep["ok"] = True; rep["published"] = summary(get_module(obj["id"], allow_hidden=True))
    return jsonify(rep)

@bp.post("/admin/modules/hide")
@auth.admin_required
def admin_modules_hide():
    d = auth.json_body(); mid = str(d.get("id", ""))
    if not set_hidden(mid, bool(d.get("hidden"))): return jsonify({"error": "no such module"}), 404
    return jsonify({"ok": True, "id": mid, "hidden": bool(d.get("hidden"))})

@bp.post("/admin/modules/delete")
@auth.admin_required
def admin_modules_delete():
    mid = str(auth.json_body().get("id", ""))
    if not delete_module(mid): return jsonify({"error": "no such module"}), 404
    return jsonify({"ok": True, "id": mid})

@bp.get("/admin/modules/<mid>.json")
@auth.admin_required
def admin_modules_download(mid):
    if not ID_RE.match(mid): return jsonify({"error": "bad id"}), 400
    data = module_json(mid)
    if data is None: return jsonify({"error": "no such module"}), 404
    return current_app.response_class(data, mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{mid}.studybuddy.json"'})

# ---------------------------------------------------------------------------
# admin page fragment (rendered by auth.admin_page through the auth.admin_extra hook)
def admin_fragment():
    from flask import render_template_string
    return render_template_string(ADMIN_MODULES_BODY, modules=list(all_modules().values()),
                                  fmt=auth.fmt_ts, upload_kb=UPLOAD_MAX_BYTES // 1024)

ADMIN_MODULES_BODY = r"""
<style>
.mrow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.mrow:last-child{border:0}
.mrow small{color:var(--muted);font-family:ui-monospace,monospace;font-size:11px}
.mbtn{width:auto;margin:0;padding:8px 12px;font-size:12px;white-space:nowrap}
.rep{margin:8px 0 0 18px;font-size:13px;line-height:1.6}.rep code{color:var(--accent)}
.prev{margin-top:12px}.pc{background:#0c1318;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:8px;font-size:13px;line-height:1.5}
.pc small{color:var(--muted);font-family:ui-monospace,monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px}
.pc ol{margin:6px 0 0 20px;color:var(--muted)}.pc ol li.ans{color:var(--good)}
input[type=file]{padding:10px;font-size:13px;color:var(--muted)}
</style>
<div class="card">
  <h2>Modules</h2>
  <p>Each module is one entry in the material dropdown. Pick a <code>studybuddy-module/1</code> JSON file (max {{ upload_kb }} KB), read the report, then publish. Uploading an id that already exists <b>replaces</b> that module; progress is kept on every card whose id is unchanged. Hidden modules are visible to admins only.</p>
  <input type="file" id="modFile" accept=".json,application/json" onchange="modCheck()">
  <div class="msg" id="modMsg"></div>
  <div id="modReport"></div>
  <hr class="hr">
  {% for m in modules %}
  <div class="mrow"><div>{{ m.name }} {% if m.hidden %}<span class="pill" style="color:var(--again)">hidden</span>{% endif %}
      <br><small>{{ m.id }}{% if m.version %} · v{{ m.version }}{% endif %} · {{ m.count }} cards{% if m.types|length > 1 %} ({% for t, n in m.types|dictsort %}{{ n }} {{ t }}{{ ', ' if not loop.last }}{% endfor %}){% elif m.types %} ({{ (m.types.keys()|list)[0] }}){% endif %} · updated {{ fmt(m.updated) }}{% if m.uploaded_by %} by {{ m.uploaded_by }}{% endif %}</small></div>
    <div style="display:flex;gap:6px">
      <button class="btn alt mbtn" data-id="{{ m.id }}" data-hidden="{{ 0 if m.hidden else 1 }}" onclick="modHide(this)">{{ 'show' if m.hidden else 'hide' }}</button>
      <a class="btn alt mbtn" href="/admin/modules/{{ m.id }}.json" style="text-decoration:none">download</a>
      <button class="btn warn mbtn" data-id="{{ m.id }}" data-name="{{ m.name }}" onclick="modDel(this)">delete</button>
    </div></div>
  {% else %}<p>No modules yet.</p>{% endfor %}
</div>
<script>
let MOD_OBJ=null;
function modCheck(){
  const f=document.getElementById('modFile').files[0]; MOD_OBJ=null;
  document.getElementById('modReport').innerHTML='';
  if(!f)return;
  if(f.size>{{ upload_kb }}*1024){say('modMsg','File is '+Math.round(f.size/1024)+' KB; the limit is {{ upload_kb }} KB.',false);return;}
  const rd=new FileReader();
  rd.onload=async()=>{
    let obj; try{obj=JSON.parse(rd.result);}catch(e){say('modMsg','Not valid JSON: '+e.message,false);return;}
    say('modMsg','Checking…',true);
    const rep=await post('/admin/modules/validate',{module:obj});
    if(rep._status===413){say('modMsg','Too large for the server (limit {{ upload_kb }} KB).',false);return;}
    if(rep._status!==200){say('modMsg',rep.error||('Server error '+rep._status),false);return;}
    MOD_OBJ=obj; say('modMsg',rep.ok?'✓ Valid — review the preview, then publish.':'✗ '+rep.errors.length+' error(s) — fix the file and pick it again.',rep.ok);
    modRender(rep);
  };
  rd.readAsText(f);
}
function modRender(rep){
  let h='';
  const s=rep.summary;
  if(s){
    h+=`<p style="margin-top:12px"><b>${s.name}</b> · id <code>${s.id}</code>${s.version?' · v'+s.version:''} · ${s.count} cards (${s.types_text}) · ${s.cats.length} categor${s.cats.length===1?'y':'ies'}</p>`;
    if(s.source) h+=`<p><small>Source: ${s.source}</small></p>`;
    if(rep.replace){const r=rep.replace;
      h+=`<p style="color:var(--ink)">Replaces <b>${r.name}</b>${r.version?' v'+r.version:''} (${r.count} cards): <b>${r.kept}</b> kept, <b>${r.added}</b> new, <b>${r.removed}</b> removed · ${r.users} user(s) have progress on it${r.hidden?' · currently hidden':''}.</p>`;}
  }
  if(rep.errors.length) h+=`<p style="color:var(--again)"><b>${rep.errors.length} error(s) — nothing will be published:</b></p><ul class="rep">${rep.errors.map(e=>`<li><code>${e.card}</code> ${e.msg}</li>`).join('')}</ul>`;
  if(rep.warnings.length) h+=`<p style="color:var(--muted)"><b>${rep.warnings.length} warning(s):</b></p><ul class="rep">${rep.warnings.map(e=>`<li><code>${e.card}</code> ${e.msg}</li>`).join('')}</ul>`;
  if(rep.preview.length){
    h+=`<div class="prev"><small class="sub">Preview — first ${rep.preview.length} cards</small>`+rep.preview.map(p=>{
      let c=`<div class="pc"><small>${p.cat} · ${p.id} · ${p.type}</small><div>${p.text}</div>`;
      if(p.type==='mc') c+=`<ol><li class="ans">${p.answer} ✓</li>${p.distractors.map(d=>`<li>${d}</li>`).join('')}</ol>`;
      return c+'</div>';}).join('')+'</div>';
  }
  if(rep.ok&&s) h+=`<button class="btn" onclick="modPublish()">${rep.replace?'Replace':'Publish'} ${s.name} (${s.count} cards)</button>`;
  document.getElementById('modReport').innerHTML=h;
}
async function modPublish(){
  if(!MOD_OBJ)return;
  const j=await post('/admin/modules/publish',{module:MOD_OBJ});
  if(j.ok){say('modMsg','✓ Published '+j.published.name+' — it is in the dropdown now.',true);setTimeout(()=>location.reload(),1200);}
  else{say('modMsg',j.error||('Failed ('+j._status+')'),false);if(j.errors)modRender(j);}
}
async function modHide(b){
  const j=await post('/admin/modules/hide',{id:b.dataset.id,hidden:b.dataset.hidden==='1'});
  if(j.ok)location.reload(); else say('modMsg',j.error||'Failed.',false);
}
async function modDel(b){
  if(!confirm('Delete "'+b.dataset.name+'"? This also deletes EVERYONE\'s progress on it. (Use hide to keep progress.)'))return;
  const j=await post('/admin/modules/delete',{id:b.dataset.id});
  if(j.ok)location.reload(); else say('modMsg',j.error||'Failed.',false);
}
</script>"""
