#!/usr/bin/env python3
"""overview_index.py — 過去の Claude / Codex セッションの索引と、セッション間の関係(エッジ)。

対象:
  Claude  ~/.claude/projects/*/<sid>.jsonl と ~/.claude-profiles/*/projects/*/<sid>.jsonl
          <sid>/subagents/**/*.jsonl は親 <sid> の子(サブエージェント)として扱う
  Codex   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

1セッション=1レコード。重いので増分索引: ファイルの (path, mtime, size) が前回と同じなら読み直さない。
索引は index.json(このフォルダ)に保存。行ごとにストリームで読み、必要な項目だけ拾う
(json.loads するのは 人の依頼・モデル/ツール行 だけ。ツール結果の行は先頭の "tool_use_id" で捨てる)。

無人実行(cron の claude -p・工場ジョブ・codex exec)は unattended=True と根拠 unattended_by を持たせ、
既定では隠す(表示側のトグル)。

エッジ(根拠のあるものだけ。推測で引かない):
  subagent   <sid>/subagents/ の親子
  continue   依頼文が「つづき/続き/Earlier messages are available/以下のcodex」等で、貼られた文章の
             特徴的な行(最大3行)が別セッションの記録に実在する → そのセッションへ。見つからなければ引かない
  review     Claude セッションの期間中に、同じ cwd で「あなたは査読者」で始まる Codex rollout → 査読
  samefile   7日以内に同じファイルを Edit/Write した別セッション(細い線・トグル)
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(os.path.expanduser("~"), ".claude", "tools"))
import cs  # noqa: E402
try:
    import clients as _clients
except ImportError:
    _clients = None

HOME = os.path.expanduser("~")
import aiboard_paths  # noqa: E402
INDEX_PATH = aiboard_paths.data("index.json")   # 旧形式(移行用)
CODEX_SESS = os.path.join(HOME, ".codex", "sessions")
GREP = "/usr/bin/grep"

CONT_WORDS = ("つづき", "続き", "Earlier messages are available", "以下のcodex", "以下のCodex", "続けて", "つづけて")
UNATTENDED_FIRST = re.compile(
    r"^(# .{1,40} — |あなたは.{0,30}(書記|編集長|論説委員|エディタ|放送作家|構成作家|査読者|リサーチャー)|"
    r"以下は会社のナレッジベース|以下のYouTube動画トランスクリプト|次のnote記事|エンジェルナンバー記事|"
    r"Reply with exactly)")
TS_RE = re.compile(rb'"timestamp":"([^"]+)"')
MODEL_RE = re.compile(rb'"model":"([^"]+)"')
CWD_RE = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
ENTRY_RE = re.compile(rb'"entrypoint":"([^"]+)"')
TOOL_RE = re.compile(rb'"type":"tool_use","id":"[^"]*","name":"([^"]+)"')
FILE_TOOLS = (b'"name":"Edit"', b'"name":"Write"', b'"name":"MultiEdit"', b'"name":"NotebookEdit"')
PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\\\"\n]+)")


def _ts(s):
    """ISO(UTC 'Z') → epoch。失敗は None。"""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _raw_prompt_text(d):
    """cs.prompt_text と同じ判定だが、改行を保った本文を返す(つづき検出用)。"""
    if not cs.prompt_text(d):
        return ""
    c = d.get("message", {}).get("content")
    return c if isinstance(c, str) else "\n".join(
        b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")


# ---------------------------------------------------------------- Claude ----
def parse_claude(path, sidechain_ok=False):
    rec = {"ai": "Claude", "model": "", "models": {}, "model_history": [], "cwd": "", "entrypoint": "",
           "start": None, "end": None, "prompts": 0, "first_prompt": "", "last_prompt": "",
           "tools": 0, "files": {}, "cont_prompts": [], "responses": 0, "title": ""}
    last_model = None
    try:
        with open(path, "rb") as f:
            for line in f:
                if not rec["cwd"]:
                    m = CWD_RE.search(line)
                    if m:
                        try:
                            rec["cwd"] = json.loads(b'"' + m.group(1) + b'"')
                        except ValueError:
                            rec["cwd"] = m.group(1).decode("utf-8", "replace")
                if not rec["entrypoint"]:
                    m = ENTRY_RE.search(line)
                    if m:
                        rec["entrypoint"] = m.group(1).decode()
                if b'"aiTitle"' in line[:60]:
                    m = re.search(rb'"aiTitle":"((?:[^"\\]|\\.)*)"', line)
                    if m:
                        try:
                            rec["title"] = json.loads(b'"' + m.group(1) + b'"')
                        except ValueError:
                            pass
                    continue
                if b'"type":"user"' in line:
                    if b'"isMeta":true' in line[:400] or b'"tool_use_id"' in line[:700]:
                        continue
                    if not sidechain_ok and b'"isSidechain":true' in line[:200]:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if sidechain_ok:
                        d["isSidechain"] = False    # サブエージェント側の依頼も依頼として数える
                    text = cs.prompt_text(d)
                    if not text:
                        continue
                    ts = _ts(d.get("timestamp", "")) or None
                    rec["prompts"] += 1
                    if rec["start"] is None and ts:
                        rec["start"] = ts
                    if ts:
                        rec["end"] = max(rec["end"] or 0, ts)
                    if not rec["first_prompt"]:
                        rec["first_prompt"] = text[:1500]
                    rec["last_prompt"] = text[:1500]
                    if any(w in text[:200] for w in CONT_WORDS) and len(text) > 200 and len(rec["cont_prompts"]) < 3:
                        rec["cont_prompts"].append(_raw_prompt_text(d)[:6000])
                elif b'"type":"assistant"' in line:
                    if not sidechain_ok and b'"isSidechain":true' in line[:200]:
                        continue
                    m = MODEL_RE.search(line)
                    tsm = TS_RE.search(line)
                    ts = _ts(tsm.group(1)) if tsm else None
                    if ts:
                        rec["end"] = max(rec["end"] or 0, ts)
                        if rec["start"] is None:
                            rec["start"] = ts
                    if m:
                        model = m.group(1).decode()
                        if model != "<synthetic>":
                            rec["models"][model] = rec["models"].get(model, 0) + 1
                            rec["model"] = model
                            if model != last_model:
                                rec["model_history"].append({"t": ts, "model": model})
                                last_model = model
                    rec["responses"] += 1
                    if b'"tool_use"' in line:
                        names = TOOL_RE.findall(line)
                        rec["tools"] += len(names)
                        if any(k in line for k in FILE_TOOLS):
                            try:
                                d = json.loads(line)
                            except ValueError:
                                continue
                            for b in d.get("message", {}).get("content", []) or []:
                                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                    fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                                    if fp:
                                        rec["files"][fp] = rec["files"].get(fp, 0) + 1
    except OSError as e:
        rec["error"] = str(e)
    return rec


# ---------------------------------------------------------------- Codex ----
def parse_codex(path):
    rec = {"ai": "Codex", "model": "", "models": {}, "model_history": [], "cwd": "", "entrypoint": "",
           "start": None, "end": None, "prompts": 0, "first_prompt": "", "last_prompt": "",
           "tools": 0, "files": {}, "cont_prompts": [], "responses": 0, "source": ""}
    last_model = None
    try:
        with open(path, "rb") as f:
            for line in f:
                if b'"session_meta"' in line[:80]:
                    try:
                        pl = json.loads(line).get("payload", {})
                    except ValueError:
                        continue
                    rec["cwd"] = pl.get("cwd", "")
                    rec["source"] = pl.get("source", "")
                    rec["entrypoint"] = pl.get("originator", "")
                    rec["start"] = _ts(pl.get("timestamp", "")) or rec["start"]
                    rec["session_id"] = pl.get("id", "")
                    continue
                tsm = TS_RE.search(line[:80])
                ts = _ts(tsm.group(1)) if tsm else None
                if b'"turn_context"' in line[:80]:
                    m = MODEL_RE.search(line)
                    if m:
                        model = m.group(1).decode()
                        rec["models"][model] = rec["models"].get(model, 0) + 1
                        rec["model"] = model
                        if model != last_model:
                            rec["model_history"].append({"t": ts, "model": model})
                            last_model = model
                    continue
                if b'"event_msg"' in line[:80] and (b'"task_complete"' in line or b'"turn_aborted"' in line or b'"task_started"' in line):
                    try:
                        pl = json.loads(line).get("payload", {})
                    except ValueError:
                        continue
                    rec["last_turn"] = pl.get("type", "")
                    rec["last_error"] = " ".join(str((pl.get("error") or {}).get("message") or "").split())[:200]
                    continue
                if b'"response_item"' not in line[:80]:
                    continue
                if b'"function_call"' in line or b'"custom_tool_call"' in line:
                    rec["tools"] += 1
                    for fp in PATCH_FILE_RE.findall(line.decode("utf-8", "replace")):
                        rec["files"][fp] = rec["files"].get(fp, 0) + 1
                    if ts:
                        rec["end"] = max(rec["end"] or 0, ts)
                elif b'"role":"user"' in line:
                    try:
                        pl = json.loads(line).get("payload", {})
                    except ValueError:
                        continue
                    txt = "".join(x.get("text", "") for x in (pl.get("content") or []) if isinstance(x, dict))
                    if not txt.strip() or txt.lstrip().startswith("<"):
                        continue
                    text = " ".join(txt.split())
                    rec["prompts"] += 1
                    if rec["start"] is None and ts:
                        rec["start"] = ts
                    if ts:
                        rec["end"] = max(rec["end"] or 0, ts)
                    if not rec["first_prompt"]:
                        rec["first_prompt"] = text[:1500]
                    rec["last_prompt"] = text[:1500]
                    if any(w in text[:200] for w in CONT_WORDS) and len(text) > 200 and len(rec["cont_prompts"]) < 3:
                        rec["cont_prompts"].append(txt[:6000])
                elif b'"role":"assistant"' in line:
                    rec["responses"] += 1
                    if ts:
                        rec["end"] = max(rec["end"] or 0, ts)
    except OSError as e:
        rec["error"] = str(e)
    return rec


# ---------------------------------------------------------------- 索引 ----
CODEX_DB = os.path.join(HOME, ".codex", "state_5.sqlite")


def _codex_db():
    import sqlite3
    if not os.path.exists(CODEX_DB):
        return None
    con = sqlite3.connect(CODEX_DB, timeout=2)   # WAL なので mode=ro は使わない(断続的に開けない)
    con.row_factory = sqlite3.Row
    return con


def enrich_codex_from_db(idx):
    """Codex 自身の状態DB(state_5.sqlite)から、記録(jsonl)に無い git・消費トークン・親スレッドを足す。

    DB にあるものを索引へ複写しない(7千件を JSON に持つと索引が太りメモリを食う。2026-09-17 に実測)。
    期間より前の Codex は query() が表示のたびに DB から引く。読み取りのみ。
    """
    import sqlite3
    for sid in [k for k, r in idx["records"].items() if r.get("light")]:
        idx["records"].pop(sid)
    try:
        con = _codex_db()
        if con is None:
            return {"skipped": "state_5.sqlite が無い"}
        rows = con.execute("select id, tokens_used, git_sha, git_branch, git_origin_url, agent_nickname from threads").fetchall()
        parents = dict(con.execute("select child_thread_id, parent_thread_id from thread_spawn_edges").fetchall())
        con.close()
    except sqlite3.Error as e:
        return {"skipped": f"state_5.sqlite を読めない: {e}"}
    enriched = 0
    for r in rows:
        rec = idx["records"].get(r["id"])
        if rec is None:
            continue
        rec["tokens"] = r["tokens_used"] or 0
        rec["git"] = {"branch": r["git_branch"] or "", "sha": (r["git_sha"] or "")[:10], "origin": r["git_origin_url"] or ""}
        if r["agent_nickname"]:
            rec["agent_nickname"] = r["agent_nickname"]
        if parents.get(r["id"]):
            rec["parent"] = parents[r["id"]]
        enriched += 1
    return {"enriched": enriched, "db_threads": len(rows), "spawn_edges": len(parents)}


def codex_history_from_db(cutoff, known):
    """索引に無い(期間より前の) Codex を DB の列だけで返す。(人の会話・サブの行, exec の件数)。

    codex exec(査読・工場ジョブ)は 7千件超あり大半が1問1答なので、行にせず件数だけ返す。
    """
    import sqlite3
    try:
        con = _codex_db()
        if con is None:
            return [], 0
        n_exec = con.execute("select count(*) from threads where source='exec' and updated_at>=?", (cutoff,)).fetchone()[0]
        rows = con.execute("select id, created_at, updated_at, source, cwd, title, tokens_used, git_sha, git_branch, "
                           "git_origin_url, first_user_message, model from threads "
                           "where source!='exec' and updated_at>=?", (cutoff,)).fetchall()
        con.close()
    except sqlite3.Error:
        return [], 0
    out = []
    for r in rows:
        if r["id"] in known:
            continue
        first = " ".join((r["first_user_message"] or "").split())[:300]
        cwd = r["cwd"] or ""
        out.append({"id": r["id"], "ai": "Codex", "model": r["model"] or "", "model_style": cs.model_style(r["model"] or ""),
                    "model_history": [], "account": "", "cwd": cwd, "project": os.path.basename(cwd.rstrip("/")),
                    "client": _clients.classify(cwd=cwd, texts=(first,)) if _clients else None,
                    "start": r["created_at"], "end": r["updated_at"], "prompts": 1 if first else 0, "tools": 0,
                    "responses": 0, "files_top": [], "kind": "codex", "parent": None, "parent_session": None,
                    "child_models": {}, "unattended": False, "unattended_by": "",
                    "role": "human" if r["source"] == "cli" else "subagent",
                    "title": " ".join((r["title"] or first).split())[:60], "first_prompt": first, "last_prompt": "",
                    "files": [], "children": 0, "live": False, "ghost": False, "light": True,
                    "tokens": r["tokens_used"] or 0,
                    "git": {"branch": r["git_branch"] or "", "sha": (r["git_sha"] or "")[:10], "origin": r["git_origin_url"] or ""}})
    return out, n_exec


def candidate_files(days):
    """(path, kind, sid, account, parent) を列挙。days=None なら全部。"""
    cutoff = time.time() - days * 86400 if days else 0
    out = []
    roots = [(os.path.join(HOME, ".claude", "projects"), "")] + [
        (p, re.search(r"\.claude-profiles/([^/]+)/", p + "/").group(1))
        for p in glob.glob(os.path.join(HOME, ".claude-profiles", "*", "projects"))]
    for root, account in roots:
        for proj in glob.glob(os.path.join(root, "*")):
            for f in glob.glob(os.path.join(proj, "*.jsonl")):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                sid = os.path.basename(f)[:-6]
                out.append((f, "claude", sid, account, None, st))
                for sf in glob.glob(os.path.join(proj, sid, "subagents", "**", "*.jsonl"), recursive=True):
                    try:
                        sst = os.stat(sf)
                    except OSError:
                        continue
                    aid = os.path.basename(sf)[:-6]
                    out.append((sf, "subagent", f"{sid}/{aid}", account, sid, sst))
    for f in glob.glob(os.path.join(CODEX_SESS, "*", "*", "*", "rollout-*.jsonl")):
        try:
            st = os.stat(f)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]+)\.jsonl$", f)
        out.append((f, "codex", m.group(1) if m else os.path.basename(f), "", None, st))
    return out


def role_of(rec, kind):
    """human=人の会話 / review=査読(crosscheck) / helper=他セッションから起動した1問1答の codex exec /
    unattended=無人実行(cron の claude -p・SDK・工場) / subagent=サブエージェント"""
    if kind == "subagent":
        return "subagent"
    if kind == "codex" and rec.get("source") == "exec":
        fp = rec.get("first_prompt", "")
        return "review" if (fp.startswith("あなたは査読者") or "反証" in fp[:80] or "adversarial reviewer" in fp[:80].lower()) else "helper"
    return "unattended" if rec.get("unattended") else "human"


def unattended_reason(rec, kind):
    if kind == "codex":
        return ""      # codex exec は role_of() で 査読/補助 に分ける(無人実行とは別扱い)
    if rec.get("entrypoint") and rec["entrypoint"] != "cli":
        return f"entrypoint={rec['entrypoint']}(claude -p / SDK)"
    cwd = rec.get("cwd", "")
    if cwd.startswith("/private/tmp/") or "/scratchpad" in cwd:
        return "cwd が一時フォルダ(scratchpad)"
    if rec.get("prompts", 0) == 1 and UNATTENDED_FIRST.match(rec.get("first_prompt", "")):
        return "人の発言が1回だけで定型の指示(" + rec["first_prompt"][:24] + "…)"
    return ""


DB_PATH = aiboard_paths.data("index.db")


def _db():
    """索引の置き場(SQLite)。以前は index.json を丸ごと読んでいて、常駐サーバが 319MB を抱えていた
    (2026-09-17 実測)。表示側は期間で絞って引き、全件を読むのはビルド(子プロセス)だけにする。"""
    import sqlite3
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("pragma journal_mode=wal")
    con.executescript("""
        create table if not exists records(id text primary key, end real, role text, kind text, json text not null);
        create index if not exists records_end on records(end);
        -- 絞り込みを索引だけで済ませる(本体は1行数KBの JSON。全行を読むと冷えた状態で3秒かかる。2026-09-17 実測)
        create index if not exists records_end_role on records(end, role);
        create index if not exists records_kind_id on records(kind, id);
        create table if not exists edges(kind text, src text, dst text, json text not null);
        create index if not exists edges_src on edges(src);
        create index if not exists edges_dst on edges(dst);
        create table if not exists files(path text primary key, mtime real, size integer, id text);
        create table if not exists cont_done(id text primary key, json text not null);
        create table if not exists meta(k text primary key, v text);""")
    return con


def _walk_dict(text, i, on_item):
    """text[i] が '{' の JSON オブジェクトを、全体を組み立てずに歩く。on_item(key, 値の位置) は値の次の位置を返す。"""
    dec = json.JSONDecoder()
    i += 1
    while True:
        while text[i] in " \t\r\n,":
            i += 1
        if text[i] == "}":
            return i + 1
        key, i = dec.raw_decode(text, i)
        while text[i] in " \t\r\n:":
            i += 1
        i = on_item(key, i)


def migrate_json_to_db():
    """index.json → index.db。1件ずつ読んで書く(全体を dict にすると 700MB 食うので組み立てない)。"""
    text = open(INDEX_PATH, encoding="utf-8").read()
    dec = json.JSONDecoder()
    con = _db()
    count = {"records": 0, "files": 0, "cont_done": 0, "edges": 0, "light_dropped": 0}

    def section(name):
        def item(k, pos):
            v, nxt = dec.raw_decode(text, pos)
            if name == "records":
                if v.get("light"):          # Codex の DB から複写した軽量レコードは持たない(表示時に DB から引く)
                    count["light_dropped"] += 1
                    return nxt
                con.execute("insert or replace into records values(?,?,?,?,?)",
                            (k, v.get("end") or v.get("mtime") or 0, v.get("role", ""), v.get("kind", ""),
                             json.dumps(v, ensure_ascii=False)))
            elif name == "files":
                con.execute("insert or replace into files values(?,?,?,?)", (k, v["mtime"], v["size"], v["id"]))
            else:
                con.execute("insert or replace into cont_done values(?,?)", (k, json.dumps(v, ensure_ascii=False)))
            count[name] += 1
            return nxt
        return item

    def top(key, pos):
        if key in ("records", "files", "cont_done"):
            return _walk_dict(text, pos, section(key))
        v, nxt = dec.raw_decode(text, pos)
        if key == "edges":
            for kind, es in v.items():
                con.executemany("insert into edges values(?,?,?,?)",
                                [(kind, e["from"], e["to"], json.dumps(e, ensure_ascii=False)) for e in es])
                count["edges"] += len(es)
        elif key == "built":
            con.execute("insert or replace into meta values('built',?)", (str(v),))
        return nxt

    with con:
        _walk_dict(text, text.index("{"), top)
    con.close()
    return count


def load_index():
    """ビルド用に全件を dict で返す(重い。表示側は query_db / get_record を使う)。"""
    if not os.path.exists(DB_PATH) and os.path.exists(INDEX_PATH):
        migrate_json_to_db()
    con = _db()
    idx = {"version": 2, "files": {}, "records": {}, "edges": {}, "cont_done": {}, "_h": {}}
    for rid, js in con.execute("select id, json from records"):
        idx["records"][rid] = json.loads(js)
        idx["_h"][rid] = hash(js)
    for path, mtime, size, rid in con.execute("select path, mtime, size, id from files"):
        idx["files"][path] = {"mtime": mtime, "size": size, "id": rid}
    for rid, js in con.execute("select id, json from cont_done"):
        idx["cont_done"][rid] = json.loads(js)
    for kind, js in con.execute("select kind, json from edges"):
        idx["edges"].setdefault(kind, []).append(json.loads(js))
    con.close()
    return idx


def save_index(idx):
    """変わったレコードだけ書く。files / cont_done / edges は小さいので入れ替える。1トランザクション。"""
    con = _db()
    seen_h = idx.setdefault("_h", {})
    with con:
        for rid, rec in idx["records"].items():
            js = json.dumps(rec, ensure_ascii=False)
            h = hash(js)
            if seen_h.get(rid) != h:
                con.execute("insert or replace into records values(?,?,?,?,?)",
                            (rid, rec.get("end") or rec.get("mtime") or 0, rec.get("role", ""), rec.get("kind", ""), js))
                seen_h[rid] = h
        gone = [rid for rid in seen_h if rid not in idx["records"]]
        con.executemany("delete from records where id=?", [(g,) for g in gone])
        for g in gone:
            seen_h.pop(g, None)
        con.execute("delete from files")
        con.executemany("insert into files values(?,?,?,?)",
                        [(p, f["mtime"], f["size"], f["id"]) for p, f in idx["files"].items()])
        con.execute("delete from cont_done")
        con.executemany("insert into cont_done values(?,?)",
                        [(k, json.dumps(v, ensure_ascii=False)) for k, v in idx.get("cont_done", {}).items()])
        con.execute("delete from edges")
        con.executemany("insert into edges values(?,?,?,?)",
                        [(kind, e["from"], e["to"], json.dumps(e, ensure_ascii=False))
                         for kind, es in idx.get("edges", {}).items() for e in es])
        if idx.get("built"):
            con.execute("insert or replace into meta values('built',?)", (str(idx["built"]),))
    con.close()


def get_record(sid):
    con = _db()
    row = con.execute("select json from records where id=?", (sid,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def edges_of(sid):
    con = _db()
    rows = con.execute("select json from edges where src=? or dst=?", (sid, sid)).fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


def query_db(days=30, include_unattended=False, live_ids=(), children=True):
    """query() と同じ形を、索引を全部読まずに返す(常駐サーバ・TUI 用)。"""
    cutoff = time.time() - days * 86400 if days else 0
    live = list(live_ids)
    con = _db()
    recs = {}
    sql = "select id, json from records where end>=?"
    if not include_unattended:
        sql += " and role!='unattended'"      # 無人実行は30日で6千件超。出さないなら読まない(件数だけ数える)
    if not children:
        # サブエージェント・査読・補助は盤では畳まれている(親の child_models に件数がある)。
        # 30日で6千行・応答の9割を占めるので、表示するときだけ読む
        sql += " and role not in ('subagent','review','helper')"
    for rid, js in con.execute(sql, (cutoff,)):
        recs[rid] = json.loads(js)
    edges = {}
    for kind, js in con.execute("select kind, json from edges" + ("" if children else " where kind in ('continue','samefile')")):
        edges.setdefault(kind, []).append(json.loads(js))
    by_role = dict(con.execute("select role, count(*) from records where end>=? group by role", (cutoff,)).fetchall())
    want = set(live) | {x for e in edges.get("continue", []) for x in (e["from"], e["to"])}
    for rid in want - set(recs):
        row = con.execute("select json from records where id=?", (rid,)).fetchone()
        if row:
            recs[rid] = json.loads(row[0])
    n_unatt = con.execute("select count(*) from records where end>=? and role='unattended'", (cutoff,)).fetchone()[0]
    known = {r[0] for r in con.execute("select id from records where kind='codex'")}
    n_index = con.execute("select count(*) from records").fetchone()[0]
    built = con.execute("select v from meta where k='built'").fetchone()
    con.close()
    res = query({"records": recs, "edges": edges, "built": float(built[0]) if built else None},
                days=days, include_unattended=include_unattended, live_ids=live, known_ids=known)
    # 件数は読まなかった行も含めて DB で数える
    res["counts"].update(subagent=by_role.get("subagent", 0), unattended=n_unatt,
                         review_helper=by_role.get("review", 0) + by_role.get("helper", 0))
    res["n_index"] = n_index
    return res


def build(days=30, force=False, progress=None):
    """増分で索引を作る。返り値: (index, stats)。"""
    t0 = time.time()
    idx = load_index()
    if force:   # 解析はやり直すが、つづき探索の結果(grep が重い)は残す
        idx = {"version": 2, "files": {}, "records": {}, "edges": {}, "cont_done": idx.get("cont_done", {}),
               "_h": idx.get("_h", {})}
    files = candidate_files(days)
    seen = set()
    parsed = 0
    for f, kind, sid, account, parent, st in files:
        seen.add(f)
        prev = idx["files"].get(f)
        if prev and prev["mtime"] == st.st_mtime and prev["size"] == st.st_size:
            continue
        rec = parse_codex(f) if kind == "codex" else parse_claude(f, sidechain_ok=(kind == "subagent"))
        rec.update(id=sid, kind=kind, path=f, account=account, parent=parent,
                   mtime=st.st_mtime, size=st.st_size,
                   project=os.path.basename(rec["cwd"].rstrip("/")) if rec.get("cwd") else "")
        rec["files_top"] = sorted(rec["files"].items(), key=lambda x: -x[1])[:8]
        rec["files"] = list(rec["files"].keys())[:60]
        rec["unattended_by"] = unattended_reason(rec, kind)
        rec["unattended"] = bool(rec["unattended_by"])
        rec["role"] = role_of(rec, kind)
        if kind == "codex" and not rec.get("title"):
            rec["title"] = rec["first_prompt"][:60]
        rec["client"] = _clients.classify(cwd=rec["cwd"], account=account,
                                          texts=(rec["first_prompt"], rec["last_prompt"])) if _clients else None
        rec["model_style"] = cs.model_style(rec["model"])
        if not rec["start"]:
            rec["start"] = st.st_mtime
        if not rec["end"]:
            rec["end"] = st.st_mtime
        idx["records"][sid] = rec
        idx["files"][f] = {"mtime": st.st_mtime, "size": st.st_size, "id": sid}
        # つづきの探索結果: 見つかっていればそのまま(動いているセッションを毎回 grep し直さない)。
        # 見つかっていない場合も、貼り付け依頼の数が変わった時だけ探し直す
        prev_c = idx["cont_done"].get(sid)
        if prev_c is None or (prev_c.get("none") and prev_c.get("n") != len(rec["cont_prompts"])):
            idx["cont_done"].pop(sid, None)
        parsed += 1
        if progress and parsed % 200 == 0:
            progress(f"{parsed} 件解析 {time.time() - t0:.0f}s")
    # 消えたファイル
    for f in list(idx["files"]):
        if f not in seen and not os.path.exists(f):
            sid = idx["files"].pop(f)["id"]
            idx["records"].pop(sid, None)
    codex_db = enrich_codex_from_db(idx)
    # 親子の要約(子のモデル)
    for rec in idx["records"].values():
        rec["children"] = []
        rec["child_models"] = {}
    for rec in idx["records"].values():
        p = rec.get("parent")
        if p and p in idx["records"]:
            idx["records"][p]["children"].append(rec["id"])
            cm = idx["records"][p]["child_models"]
            lab = rec["model_style"]["label"] if rec.get("model") else "モデル不明"
            cm[lab] = cm.get(lab, 0) + 1
    stats = {"files": len(files), "parsed": parsed, "records": len(idx["records"]), "took": round(time.time() - t0, 1),
             "codex_db": codex_db}
    save_index(idx)
    if progress:
        progress(f"解析 {parsed} 件 {stats['took']}s → エッジ計算")
    t1 = time.time()
    build_edges(idx, progress=progress)
    stats["edges_took"] = round(time.time() - t1, 1)
    stats["edges"] = {k: len(v) for k, v in idx["edges"].items()}
    idx["built"] = time.time()
    save_index(idx)
    stats["total_took"] = round(time.time() - t0, 1)
    return idx, stats


# ---------------------------------------------------------------- エッジ ----
def _pick_lines(text):
    """貼られた文章から、検索に使う特徴的な行を最大3つ選ぶ(短すぎ・記号だけ・定型を除く)。"""
    cands = []
    for ln in text.splitlines():
        s = ln.strip()
        if not (24 <= len(s) <= 140):
            continue
        if s.startswith(("Earlier messages", "•", "└", "├", "│", "⎿", ">", "#")):
            s2 = s.lstrip("•└├│⎿># ").strip()
            if len(s2) < 24:
                continue
            s = s2
        if any(w in s for w in CONT_WORDS):
            continue
        if sum(c.isalnum() for c in s) < 12:
            continue
        cands.append(s)
    # 中ほどの行ほど固有なので、前・中・後から1つずつ
    if not cands:
        return []
    picks = [cands[len(cands) // 2]]
    if len(cands) > 2:
        picks += [cands[len(cands) // 4], cands[(3 * len(cands)) // 4]]
    elif len(cands) == 2:
        picks.append(cands[0] if cands[0] != picks[0] else cands[1])
    out = []
    for p in picks:
        if p not in out:
            out.append(p)
    return out[:3]


def _find_continuation(idx, rec):
    """rec の「つづき」の貼り付け元を探す。見つかった (相手id, 根拠) か None。"""
    lines = []
    for t in rec.get("cont_prompts", []):
        lines += _pick_lines(t)
    lines = lines[:3]
    if not lines:
        return None
    start = rec.get("start") or rec["mtime"]
    recs = idx["records"]
    # 候補: 自分より前に始まり、30日以内、同じ cwd を優先。「codex」と書いてあれば Codex も
    wants_codex = any("codex" in t[:120].lower() for t in rec.get("cont_prompts", []))
    cands = []
    for r in recs.values():
        if r["id"] == rec["id"] or r["kind"] == "subagent":
            continue
        if (r.get("start") or 0) > start + 60 or (r.get("end") or 0) < start - 3 * 86400:
            continue
        same = r.get("cwd") == rec.get("cwd")
        if r["kind"] == "codex" and not wants_codex:
            continue
        if r["kind"] == "claude" and not same and not wants_codex:
            continue
        cands.append((0 if same else 1, abs(start - (r.get("end") or 0)), r["id"]))
    cands.sort()
    paths = [recs[c[2]]["path"] for c in cands[:40]]
    # サブエージェント側にも本文があるので親のフォルダも見る
    if not paths:
        return None
    # 1回目: 3行をまとめて -e で渡し、当たったファイルだけ絞る(grep の回数を 3→1+α に)
    try:
        r = subprocess.run([GREP, "-lF"] + [x for ln in lines for x in ("-e", ln)] + ["--"] + paths,
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    hit_files = r.stdout.splitlines()
    hits = {}
    for p in hit_files:
        for ln in lines:
            try:
                r2 = subprocess.run([GREP, "-qF", "--", ln, p], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r2.returncode == 0:
                hits.setdefault(p, []).append(ln)
    if not hits:
        return None
    need = 2 if len(lines) >= 2 else 1
    best = None
    for p, matched in hits.items():
        if len(matched) < need:
            continue
        rid = idx["files"].get(p, {}).get("id")
        if not rid or rid == rec["id"]:
            continue
        key = (-len(matched), abs(start - (recs[rid].get("end") or 0)))
        if best is None or key < best[0]:
            best = (key, rid, matched)
    if not best:
        return None
    return best[1], {"matched_lines": best[2], "searched_lines": lines, "searched_files": len(paths)}


def build_edges(idx, progress=None):
    recs = idx["records"]
    edges = {"subagent": [], "continue": [], "review": [], "helper": [], "samefile": []}
    # 親子
    for r in recs.values():
        if r.get("parent") and r["parent"] in recs:
            edges["subagent"].append({"from": r["parent"], "to": r["id"], "kind": "subagent", "label": "サブエージェント",
                                      "evidence": f"{os.path.relpath(r['path'], os.path.dirname(recs[r['parent']]['path']))}"})
    # つづき(結果はキャッシュ。none も記録して二度探さない)
    done = idx.setdefault("cont_done", {})
    todo = [r for r in recs.values() if r.get("cont_prompts") and r["id"] not in done]
    if progress:
        progress(f"つづき候補 {len(todo)} 件を検索")
    n = 0
    for r in recs.values():
        if not r.get("cont_prompts"):
            continue
        if r["id"] in done:
            res = done[r["id"]]
        else:
            found = _find_continuation(idx, r)
            res = {"to": found[0], "evidence": found[1]} if found else None
            done[r["id"]] = res or {"none": True, "n": len(r["cont_prompts"])}
            n += 1
            if progress and n % 20 == 0:
                progress(f"つづき検索 {n}/{len(todo)}")
                save_index(idx)
        if res and not res.get("none"):
            edges["continue"].append({"from": res["to"], "to": r["id"], "kind": "continue", "label": "つづき",
                                      "evidence": res["evidence"]})
    # 査読・補助: codex exec の rollout を、同じ cwd で期間が重なる Claude セッションの子にする
    edges["helper"] = []
    claude_by_cwd = {}
    for r in recs.values():
        r["parent_session"] = None
        if r["kind"] == "claude":
            claude_by_cwd.setdefault(r.get("cwd", ""), []).append(r)
    for rv in recs.values():
        if rv.get("role") not in ("review", "helper"):
            continue
        t = rv.get("start") or rv["mtime"]
        best = None
        for c in claude_by_cwd.get(rv.get("cwd", ""), []):
            if (c.get("start") or 0) - 60 <= t <= (c.get("end") or 0) + 300:
                gap = t - (c.get("start") or 0)
                if best is None or gap < best[0]:
                    best = (gap, c)
        if best:
            rv["parent_session"] = best[1]["id"]
            lab = "査読" if rv["role"] == "review" else "補助"
            edges[rv["role"]].append({"from": best[1]["id"], "to": rv["id"], "kind": rv["role"], "label": lab,
                                      "evidence": f"同じ cwd {rv.get('cwd')} で、Claude の期間内({time.strftime('%m-%d %H:%M', time.localtime(t))})に codex exec が開始" + ("(「あなたは査読者」で始まる)" if rv["role"] == "review" else f"(最初の依頼: {rv.get('first_prompt', '')[:40]})")})
    # 同じファイル(7日以内)
    by_file = {}
    for r in recs.values():
        for fp in r.get("files", []):
            by_file.setdefault(fp, []).append(r)
    seen = set()
    for fp, rs in by_file.items():
        if len(rs) < 2 or len(rs) > 8:
            continue
        rs = sorted(rs, key=lambda r: r.get("end") or 0)
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if abs((a.get("end") or 0) - (b.get("end") or 0)) > 7 * 86400:
                    continue
                if a.get("parent") == b["id"] or b.get("parent") == a["id"]:
                    continue
                key = (a["id"], b["id"])
                if key in seen:
                    continue
                seen.add(key)
                edges["samefile"].append({"from": a["id"], "to": b["id"], "kind": "samefile", "label": "同じファイル",
                                          "evidence": fp})
                if len(edges["samefile"]) >= 3000:
                    break
    idx["edges"] = edges


# ---------------------------------------------------------------- 取り出し ----
def query(idx, days=30, include_unattended=False, live_ids=(), known_ids=None):
    """表示用に絞ったレコードとエッジ。live_ids のセッションは state=live。
    人の会話(role=human)は全件、サブエージェント/査読/補助は親の子として(短い形で)返し、表示側が畳む。
    無人実行(role=unattended)は include_unattended のときだけ。"""
    cutoff = time.time() - days * 86400 if days else 0
    live = set(live_ids)
    out = {}
    counts = {"human": 0, "review_helper": 0, "unattended": 0, "subagent": 0}
    for r in idx["records"].values():
        if (r.get("end") or r["mtime"]) < cutoff and r["id"] not in live:
            continue
        role = r.get("role", "human")
        counts["review_helper" if role in ("review", "helper") else role] += 1
        if role == "unattended" and not include_unattended and r["id"] not in live:
            continue
        out[r["id"]] = r
    # つづきの相手だけは、期間外でも小さく(ghost)出す
    extra = {}
    for e in idx["edges"].get("continue", []):
        for x in (e["from"], e["to"]):
            if x not in out and x in idx["records"]:
                extra[x] = idx["records"][x]
    out.update(extra)
    edges = [e for es in idx["edges"].values() for e in es if e["from"] in out and e["to"] in out]
    keep = ("id", "ai", "model", "model_style", "model_history", "account", "client", "cwd", "project", "start", "end",
            "prompts", "tools", "responses", "files_top", "kind", "parent", "parent_session", "child_models",
            "unattended", "unattended_by", "role", "title", "tokens", "git", "last_turn", "last_error", "agent_nickname", "path")
    rows = []
    for r in out.values():
        row = {k: r.get(k) for k in keep}
        short = r.get("role") in ("subagent", "review", "helper")
        row["first_prompt"] = (r.get("first_prompt") or "")[:120 if short else 300]
        row["last_prompt"] = (r.get("last_prompt") or "")[:120 if short else 300]
        row["files"] = [] if short else [os.path.basename(f) for f in r.get("files", [])[:30]]   # 検索用にファイル名だけ
        row["children"] = len(r.get("children") or [])
        row["live"] = r["id"] in live
        row["ghost"] = r["id"] in extra
        rows.append(row)
    # 索引の期間より前の Codex は、Codex 自身の DB から引く(索引には複写しない)
    older, n_exec = codex_history_from_db(cutoff, known_ids if known_ids is not None else idx["records"])
    rows.extend(older)
    counts["human"] += sum(1 for r in older if r["role"] == "human")
    counts["codex_exec_in_db"] = n_exec
    rows.sort(key=lambda r: -(r.get("end") or 0))
    return {"records": rows, "edges": edges, "built": idx.get("built"), "counts": counts}


def query_cli(argv):
    """`--query <JSON>`: 表示用の結果を JSON で標準出力へ。常駐サーバが子プロセスとして呼ぶ。
    13MB の結果を常駐側で組み立てると、Python はそのメモリを OS に返さない(3回で 344MB。2026-09-17 実測)。"""
    import overview
    a = json.loads(argv)
    res = query_db(days=a.get("days"), include_unattended=bool(a.get("unattended")), live_ids=a.get("live_ids") or (),
                   children=bool(a.get("children")))
    for r in res["records"]:
        r["first_prompt"] = overview.redact(r.get("first_prompt", ""))
        r["last_prompt"] = overview.redact(r.get("last_prompt", ""))
    res.update(a.get("extra") or {})
    sys.stdout.write(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--query":
        query_cli(sys.argv[2])
        sys.exit(0)
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    idx, stats = build(days=days, force="--force" in sys.argv, progress=lambda m: print(m, file=sys.stderr))
    print(json.dumps(stats, ensure_ascii=False))
