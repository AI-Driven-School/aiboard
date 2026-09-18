#!/usr/bin/env python3
"""overview.py — 「AI作業の全体像」のデータ層。snapshot() が JSON 化できる dict を返す。

判定は1か所:
  - タブの状態(作業中/返答待ち/確認待ち/codex…)は cs.classify()(~/.claude/tools/cs.py)
  - 顧客(利用者が ~/.aiboard/clients.json に書く)は clients.py の classify()
  - 「いま何をしているか」は ~/.claude/tabstate/<session_id>.json(hooks/tab-status.py が書く)
  ここでは判定を書き直さない。集めて並べるだけ。

snapshot() の中身:
  sessions   このMacの全AIセッション(cs と同じ件数)
  attention  利用者が対応すべきもの(⚠確認待ち → 返答済みで長く放置 → 確認画面で停止)
  clients    顧客プロダクトごとのまとめ(動いている数・状態の内訳・最新の依頼・今日の依頼件数)
  projects   自社プロジェクト(フォルダ)ごとのまとめ
  machine    メモリ(使用率・圧縮・スワップ・内訳)と JetsamEvent(強制終了)の回数
  macmini    無人AIジョブ(YouTube工場 launchd / cron の claude -p / PM2 / cron-wrap 61本)。60秒キャッシュ

失敗・未測定は ok=False と reason を持たせ、正常と混ぜない。
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
import cs  # noqa: E402  判定ロジックの本体

HOME = os.path.expanduser("~")
import aiboard_paths  # noqa: E402
MACMINI_HOSTS = aiboard_paths.config().get("remote_hosts", [])   # ssh で無人ジョブを読む先。~/.aiboard/config.json の remote_hosts(無ければ読まない)
MACMINI_CACHE = aiboard_paths.data("remote_cache.json")
MACMINI_TTL = 60                                    # 秒。ssh は遅いので使い回す
SSH_OPTS = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
IDLE_LONG = 15 * 60                                 # 返答済みのまま「長く放置」とみなす秒数

# ---------------------------------------------------------------- 伏せ字 ----
SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{12,}"),          # OpenAI / Stripe 風
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bfhm_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),            # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),                  # Google API key
    re.compile(r"\bey[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{16,}"),
    re.compile(r"(?i)((?:api[_\-]?key|secret|token|password|passwd)\s*[=:]\s*[\"']?)[^\s\"',;]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),                    # 40桁以上の英数字(ハッシュ・トークン)
]


def redact(text):
    """APIキー風の文字列を伏せ字にする(表示前に必ず通す)。"""
    if not text:
        return text
    for pat in SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "●●●(伏せ字)", text)
        else:
            text = pat.sub("●●●(伏せ字)", text)
    return text


# ---------------------------------------------------------------- 共通 ----
def local_today():
    return dt.date.today()


def iso_to_local_date(ts):
    """トランスクリプトの timestamp(ISO, UTC 'Z' 付き)をローカル日付に。"""
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.astimezone().date()
    except (ValueError, AttributeError):
        return None


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分"
    if sec < 86400:
        return f"{sec / 3600:.1f}時間"
    return f"{sec / 86400:.1f}日"


def project_name(cwd):
    if not cwd:
        return ""
    base = os.path.basename(cwd.rstrip("/"))
    return base if base != os.path.basename(HOME) else "~(ホーム)"


# ---------------------------------------------------------------- 今日の依頼件数 ----
def today_requests(transcript, ai):
    """今日(ローカル日付)そのセッションで人が出した依頼の件数。

    数え方:
      Claude: トランスクリプト末尾 4MB の中の type=user レコードのうち、cs.prompt_text() が
              依頼と認めるもの(ツール結果・isMeta・isSidechain・'<' で始まる system 注入を除く)で、
              timestamp のローカル日付が今日のもの。
      Codex : rollout の response_item/message で role=user かつ input_text が '<' で始まらないもので、
              timestamp のローカル日付が今日のもの。
    末尾 4MB より前は読まないので、その範囲を超える長いセッションでは partial=True(下限値)になる。
    終了したセッション(タブに無いもの)は数えない。
    """
    if not transcript:
        return 0, False
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0, False
    today = local_today()
    codex = ai.startswith("Codex")
    st = _TODAY_CACHE.get(transcript)
    if not st or st["day"] != today or size < st["pos"]:
        # 初回・日付が変わった・ファイルが縮んだ → 末尾から数え直す。以後は増えた分だけ読む
        # (毎回 4MB×タブ数を読み直すと常駐のメモリが戻らない。2026-09-17 実測)
        start = 0 if codex else max(0, size - 4_000_000)
        st = _TODAY_CACHE[transcript] = {"day": today, "pos": start, "n": 0, "partial": start > 0, "first": None}
    if size > st["pos"]:
        try:
            with open(transcript, "rb") as f:
                f.seek(st["pos"])
                raw = f.read()
        except OSError:
            return st["n"], st["partial"] and st["first"] == today
        cut = raw.rfind(b"\n") + 1            # 書き込み途中の行は次回に回す
        chunk = raw[:cut].decode("utf-8", errors="replace")
        st["pos"] += cut
        for ts in (_codex_prompt_times(chunk) if codex else (t for t, _ in cs.user_prompts_in(chunk))):
            d = iso_to_local_date(ts)
            if st["first"] is None:
                st["first"] = d
            if d == today:
                st["n"] += 1
    # 末尾 4MB の最初の依頼が既に今日なら、それより前にも今日の依頼があるかもしれない
    return st["n"], st["partial"] and st["first"] == today


_TODAY_CACHE = {}


def _codex_prompt_times(chunk):
    for line in chunk.splitlines():
        if '"role":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        pl = d.get("payload") or {}
        if pl.get("type") != "message" or pl.get("role") != "user":
            continue
        txt = "".join(x.get("text", "") for x in (pl.get("content") or []) if isinstance(x, dict))
        if txt.lstrip().startswith("<") or not txt.strip():
            continue
        yield d.get("timestamp", "")


# ---------------------------------------------------------------- 上限 ----
LIMIT_RE = re.compile(r"hit your (session|weekly|opus|usage)[^·\n]*limit(?:\s*·\s*resets?\s+([^\"\n]+?))?\s*(?:$|\")", re.I)
CODEX_LIMIT_RE = re.compile(r"(usage limit|hit your limit)[^\n]*?try again at ([A-Za-z]{3} \d{1,2}(?:st|nd|rd|th)?, \d{4} \d{1,2}:\d{2} ?[AP]M)", re.I)


@cs.memo_by_file
def claude_limit(path):
    """記録の最後が「上限に当たった」なら {kind, resets, at, text}。その後に依頼や返答が続いていれば解けている(None)。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 400_000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in chunk.splitlines():
        if '"type":"assistant"' in line or '"type":"user"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "assistant" and d.get("isApiErrorMessage") and d.get("error") == "rate_limit":
                txt = "".join(b.get("text", "") for b in (d.get("message", {}).get("content") or []) if isinstance(b, dict))
                m = LIMIT_RE.search(txt + '"')
                last = {"kind": (m.group(1).lower() if m else "usage"), "resets": (m.group(2) or "").strip() if m else "", "at": d.get("timestamp", ""), "text": txt[:140]}
            elif d.get("type") == "assistant" and (d.get("message", {}).get("model") or "") != "<synthetic>":
                last = None
            elif d.get("type") == "user" and cs.prompt_text(d):
                last = None if last is None else last   # 依頼しただけでは解けない(また当たる)。返答が来たら解ける
    return last


def resets_epoch(resets, at_iso):
    """「4:10am (Asia/Tokyo)」「Sep 14 at 6am (Asia/Tokyo)」「Sep 22nd, 2026 4:49 PM」を、当たった時刻より後の実時刻(epoch)に。分からなければ None。"""
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    if not resets:
        return None
    tzm = re.search(r"\(([A-Za-z_]+/[A-Za-z_]+)\)", resets)
    tz = ZoneInfo(tzm.group(1)) if tzm else _dt.datetime.now().astimezone().tzinfo
    base = None
    if at_iso:
        try:
            base = _dt.datetime.fromisoformat(at_iso.replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            base = None
    base = base or _dt.datetime.now(tz)
    txt = re.sub(r"\(.*?\)", "", resets).strip()
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2})(?:st|nd|rd|th)?,? (?:(\d{4}) )?(?:at )?(\d{1,2})(?::(\d{2}))? ?([ap]m)", txt, re.I)
    if m:
        mon = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"].index(m.group(1).lower()) + 1
        hour = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "pm" else 0)
        dt = _dt.datetime(int(m.group(3) or base.year), mon, int(m.group(2)), hour, int(m.group(5) or 0), tzinfo=tz)
        return dt.timestamp()
    m = re.search(r"(\d{1,2})(?::(\d{2}))? ?([ap]m)", txt, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        dt = base.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if dt <= base:
            dt += _dt.timedelta(days=1)
        return dt.timestamp()
    return None


def with_active(lim):
    """上限の情報に、解除時刻(epoch)と「いまも上限中か」を足す。"""
    if not lim:
        return None
    ts = resets_epoch(lim.get("resets"), lim.get("at"))
    if ts is None:   # 解除時刻が書かれていない上限は、Claude の窓(5 時間)で解けたとみなす
        try:
            import datetime as _dt
            age = time.time() - _dt.datetime.fromisoformat(lim["at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            age = 0
        return dict(lim, resets_at=None, active=age < 5 * 3600)
    return dict(lim, resets_at=ts, active=time.time() < ts)


def codex_limit(doing):
    m = CODEX_LIMIT_RE.search(doing or "")
    return {"kind": "usage", "resets": m.group(2), "at": "", "text": (doing or "")[:140]} if m else None


# ---------------------------------------------------------------- loop / skill / MCP ----
def _tool_uses(chunk):
    """会話ログの断片から tool_use を (時刻, 名前, 入力) で順に返す。"""
    for line in chunk.splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for b in (d.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                yield d.get("timestamp", ""), b.get("name") or "", b.get("input") or {}


def _iso_epoch(ts):
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


_TOOLS = {}   # path -> {"off": 読んだ位置, "ino": inode, "val": 集計}


def session_tools(path):
    """1 セッションで使った skill / MCP サーバの回数と、/loop(ScheduleWakeup)・予約(CronCreate)の最後の指示。
    会話ログは追記しかされないので、前回の続きから 1 行ずつ読む(全体を毎回読み直さない・メモリに載せない)。"""
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return None
    slot = _TOOLS.get(path)
    if not slot or slot["ino"] != st.st_ino or st.st_size < slot["off"]:
        slot = _TOOLS[path] = {"off": 0, "ino": st.st_ino, "val": {"skills": {}, "mcp": {}, "wake": None, "crons": [], "dirs": {}}}
    if st.st_size == slot["off"]:
        return slot["val"]
    v = slot["val"]
    v.setdefault("dirs", {})
    with open(path, "rb") as f:
        f.seek(slot["off"])
        while True:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):   # 書きかけの最終行は次回に回す
                break
            slot["off"] += len(raw)
            if b'"tool_use"' not in raw:
                continue
            for ts, name, inp in _tool_uses(raw.decode("utf-8", errors="replace")):
                if name == "Skill":
                    k = str(inp.get("skill") or "?"); v["skills"][k] = v["skills"].get(k, 0) + 1
                elif name.startswith("mcp__"):
                    k = name.split("__")[1] if name.count("__") >= 2 else name[5:]; v["mcp"][k] = v["mcp"].get(k, 0) + 1
                elif name == "ScheduleWakeup":
                    v["wake"] = None if inp.get("stop") else {"at": _iso_epoch(ts), "delay": inp.get("delaySeconds") or 0, "reason": str(inp.get("reason") or "")[:140]}
                elif name == "CronCreate":
                    v["crons"].append({"cron": str(inp.get("cron") or ""), "recurring": bool(inp.get("recurring", True)), "prompt": str(inp.get("prompt") or "")[:140], "at": _iso_epoch(ts)})
                for k in ("file_path", "notebook_path", "path"):   # 触ったファイルの置き場(ホームで動く会話の実質の持ち場)
                    d = work_root(inp.get(k))
                    if d:
                        v["dirs"][d] = v["dirs"].get(d, 0) + 1
    return v


SKIP_ROOTS = {".claude", ".codex", ".aiboard", "Library", "Applications", "Downloads", "tmp", ".Trash"}


def work_root(path):
    """触ったファイルから「実質の持ち場」を 1 つ決める。~/Desktop/X のように 1 段では足りない所は 2 段見る。

    利用者の 8 割超がホーム直下で Claude を動かしており(30 日で 263/321)、cwd だけでは仕事が分けられない。
    触っているファイルの置き場なら、そのうち 54% に本当の持ち場が付く(2026-09-18 実測)。
    """
    if not isinstance(path, str) or not path.startswith(HOME + "/"):
        return ""
    parts = [x for x in path[len(HOME) + 1:].split("/") if x]
    if not parts or len(parts) < 2:   # ホーム直下のファイルそのものは持ち場にしない
        return ""
    top = parts[0]
    if top in SKIP_ROOTS:
        return ""
    if top in ("Desktop", "Documents", "src", "work", "repos"):
        return top + "/" + parts[1] if len(parts) > 2 else ""   # 直下に置いただけのファイルは持ち場でない
    return top


_GROUPS = {"t": 0, "val": {}}


def groups_file():
    import aiboard_paths as ap
    return ap.data("groups.json")


def groups(force=False):
    """束ね方の上書き(~/.aiboard/groups.json)。{持ち場やプロジェクトの名前: {label, rgb, client}}。

    自動判定(顧客の規則・cwd・触ったファイル)はそのまま使い、表示名・色・どの顧客かだけを人が上書きできる。
    """
    p = groups_file()
    try:
        st = os.stat(p)
    except OSError:
        _GROUPS.update(t=0, val={})
        return {}
    if force or _GROUPS["t"] != st.st_mtime_ns:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            _GROUPS.update(t=st.st_mtime_ns, val=d.get("groups") or {})
        except (OSError, ValueError):
            _GROUPS.update(t=st.st_mtime_ns, val={})
    return _GROUPS["val"]


def save_groups(groups_in, clients_known=None):
    """束ね方の上書きを保存する。形が違うものは弾く(盤から来た値をそのまま書かない)。"""
    if not isinstance(groups_in, dict) or len(groups_in) > 200:
        raise ValueError("形が違う(dict・200 件まで)")
    clean = {}
    for k, v in groups_in.items():
        if not isinstance(k, str) or not (1 <= len(k) <= 80) or not isinstance(v, dict):
            raise ValueError(f"名前が不正: {str(k)[:20]}")
        row = {}
        label = v.get("label")
        if label:
            if not isinstance(label, str) or len(label) > 40:
                raise ValueError(f"表示名が長すぎる: {str(label)[:20]}")
            row["label"] = label
        rgb = v.get("rgb")
        if rgb:
            if (not isinstance(rgb, (list, tuple)) or len(rgb) != 3
                    or not all(isinstance(x, int) and 0 <= x <= 255 for x in rgb)):
                raise ValueError(f"色が不正: {rgb}")
            row["rgb"] = list(rgb)
        ins = v.get("instructions")
        if ins:
            if not isinstance(ins, str) or len(ins) > 4000:
                raise ValueError("指示が長すぎる(4000 字まで)")
            row["instructions"] = ins
        cid = v.get("client")
        if cid:
            if not isinstance(cid, str) or (clients_known is not None and cid not in clients_known):
                raise ValueError(f"知らない顧客: {str(cid)[:20]}")
            row["client"] = cid
        if row:
            clean[k] = row
    p = groups_file()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"_doc": "束ね方の上書き。盤の設定から編集する。キーは持ち場/プロジェクトの名前", "groups": clean}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    groups(force=True)
    return clean


def safe_key(key):
    """案件の名前をファイル名に使える形に(アプリ側の writeInstructions と同じ規則)。"""
    ok = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    return "".join(c if c in ok else "_" for c in str(key))[:64]


def notes_path(key):
    import aiboard_paths as ap
    d = ap.data("projects")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, safe_key(key) + ".notes.md")


def read_notes(key):
    """案件の申し送り(次に入る人・AI への引き継ぎ)。無ければ空。"""
    try:
        with open(notes_path(key), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def save_notes(key, text):
    if not isinstance(key, str) or not (1 <= len(key) <= 80):
        raise ValueError("案件の名前が不正")
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("申し送りが長すぎる(20000 字まで)")
    p = notes_path(key)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
    return len(text)


def client_defs():
    """顧客の定義(id/label/emoji/rgb)。判定規則そのものは clients.py が持つ。"""
    import aiboard_paths as ap
    try:
        with open(ap.clients_file(), encoding="utf-8") as f:
            return [{k: c.get(k) for k in ("id", "label", "emoji", "rgb")} for c in json.load(f).get("clients", [])]
    except (OSError, ValueError):
        return []


def apply_group(sess):
    """束ね方の上書きを 1 セッションに当てる(顧客が未判定なら顧客を付け、表示名と色を差し替える)。"""
    g = groups()
    if not g:
        return sess
    key = sess.get("project_hint") or (sess.get("project") or "")
    row = g.get(key)
    if not row:
        return sess
    if row.get("client") and not sess.get("client"):
        c = next((x for x in client_defs() if x.get("id") == row["client"]), None)
        if c:
            sess["client"] = dict(c, by="group")
    if row.get("label") or row.get("rgb"):
        sess["group_label"] = row.get("label") or key
        sess["group_rgb"] = row.get("rgb")
    return sess


def project_hint(tools, cwd, min_hits=3):
    """ホームで動いている会話に、触ったファイルから持ち場の名前を付ける(足りなければ空)。"""
    if not tools or (cwd or "").rstrip("/") != HOME:
        return ""
    dirs = tools.get("dirs") or {}
    if not dirs:
        return ""
    name, n = max(dirs.items(), key=lambda kv: kv[1])
    return name if n >= min_hits else ""


def cron_next(expr, after, horizon_days=8):
    """5 欄の cron(数値・*・*/n・a-b・a,b)の次の発火(ローカル時刻の epoch)。範囲内に無ければ None。"""
    import datetime as _dt
    parts = expr.split()
    if len(parts) != 5:
        return None
    def field(txt, lo, hi):
        vals = set()
        for piece in txt.split(","):
            step = 1
            if "/" in piece:
                piece, st = piece.split("/", 1); step = int(st)
            if piece == "*":
                a, b = lo, hi
            elif "-" in piece:
                a, b = map(int, piece.split("-", 1))
            else:
                a = b = int(piece)
            vals.update(range(a, b + 1, step))
        return vals
    try:
        mi, ho, dom, mon, dow = (field(parts[0], 0, 59), field(parts[1], 0, 23), field(parts[2], 1, 31), field(parts[3], 1, 12), field(parts[4], 0, 7))
    except ValueError:
        return None
    t = _dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    for _ in range(horizon_days * 24 * 60):
        if t.minute in mi and t.hour in ho and t.month in mon and t.day in dom and ((t.isoweekday() % 7) in dow or (7 in dow and t.isoweekday() == 7)):
            return t.timestamp()
        t += _dt.timedelta(minutes=1)
    return None


def loop_state(tools, live=True):
    """いま効いている /loop と予約。ループの次の起床が 15 分以上過ぎても次の指示が無ければ、止まったとみなす。"""
    if not tools or not live:
        return None
    now = time.time()
    out = {}
    w = tools.get("wake")
    if w and w.get("at"):
        nxt = w["at"] + w["delay"]
        if now < nxt + 900:
            out["wake"] = {"next_at": nxt, "reason": redact(w["reason"])}
    crons = []
    for c in tools.get("crons") or []:
        nxt = cron_next(c["cron"], max(now - 60, c["at"] or 0))
        if nxt is None:
            continue
        crons.append({"cron": c["cron"], "recurring": c["recurring"], "next_at": nxt, "prompt": redact(c["prompt"])})
    if crons:
        out["crons"] = sorted(crons, key=lambda c: c["next_at"])[:5]
    return out or None


_EXT = {"t": 0, "health": None}


def extensions_info(refresh=False):
    """設定パネル用: 入っている skill・plugin・MCP サーバと、MCP の接続状態(claude mcp list, 10 分キャッシュ)。"""
    skills = []
    for base, src in ((os.path.join(HOME, ".claude", "skills"), "user"),):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            desc = ""
            try:
                with open(os.path.join(base, n, "SKILL.md"), encoding="utf-8", errors="replace") as f:
                    head = f.read(2000)
                m = re.search(r"^description:\s*(.+)$", head, re.M)
                desc = m.group(1).strip().strip('"')[:160] if m else ""
            except OSError:
                continue
            skills.append({"name": n, "source": src, "description": desc})
    plugins = []
    try:
        d = json.load(open(os.path.join(HOME, ".claude", "plugins", "installed_plugins.json"), encoding="utf-8"))
        plugins = sorted((d.get("plugins") or d).keys())
    except (OSError, ValueError, AttributeError):
        pass
    servers = []
    try:
        cj = json.load(open(os.path.join(HOME, ".claude.json"), encoding="utf-8"))
        for k, v in (cj.get("mcpServers") or {}).items():   # 設定の中身(env, url)は秘密を含み得るので名前と種類だけ
            servers.append({"name": k, "scope": "user", "type": v.get("type") or ("stdio" if v.get("command") else "")})
        for proj, pv in (cj.get("projects") or {}).items():
            for k, v in (pv.get("mcpServers") or {}).items():
                servers.append({"name": k, "scope": "project", "project": project_name(proj), "type": v.get("type") or ("stdio" if v.get("command") else "")})
    except (OSError, ValueError):
        pass
    if refresh or _EXT["health"] is None or time.time() - _EXT["t"] > 600:
        health = {}
        try:
            r = subprocess.run(["zsh", "-l", "-c", "command claude mcp list"], capture_output=True, text=True, timeout=60, cwd=HOME)
            for line in r.stdout.splitlines():
                m = re.match(r"^(.+?): .*? - ([✔✘!⊘]) ?(.*)$", line)
                if m:
                    st = {"✔": "ok", "✘": "failed", "!": "auth", "⊘": "disabled"}[m.group(2)]
                    health[m.group(1).strip()] = {"status": st, "note": re.sub(r"https?://\S+", "", m.group(3))[:80]}
        except (subprocess.TimeoutExpired, OSError) as e:
            health = {"_error": {"status": "failed", "note": type(e).__name__}}
        _EXT.update(t=time.time(), health=health)
    return {"skills": skills, "plugins": plugins, "servers": servers, "health": _EXT["health"], "checked_at": _EXT["t"]}


_AGENTS = {"t": 0, "val": []}


def official_agents(max_age=5):
    """`claude agents --json` の一覧(公式の状態源)。hook が無くても状態が分かる。

    返す項目: pid(対話セッション)・id(背景セッション)・status(busy/waiting/idle)・waitingFor・state・cwd・name。
    0.5 秒ほどかかるので 5 秒使い回す。CLI が無い/失敗しても盤は止めない(空で返す)。
    """
    now = time.time()
    if now - _AGENTS["t"] < max_age:
        return _AGENTS["val"]
    out = []
    try:
        r = subprocess.run(["zsh", "-l", "-c", "command claude agents --json"], capture_output=True, text=True, timeout=15, cwd=HOME)
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            out = [x for x in json.loads(r.stdout) if isinstance(x, dict)]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        out = _AGENTS["val"]   # 取れなかった時は前の値(古いと分かるように t は進めない)
        _AGENTS["val"] = out
        return out
    _AGENTS.update(t=now, val=out)
    return out


OFFICIAL_STATE = {   # 公式の言い方 → 盤の言い方
    ("waiting", "permission prompt"): "確認待ち", ("waiting", "sandbox request"): "確認待ち",
    ("waiting", "worker request"): "確認待ち", ("waiting", "dialog open"): "確認待ち",
    ("waiting", "input needed"): "返答待ち", ("waiting", None): "返答待ち",
    ("busy", None): "作業中", ("idle", None): "返答待ち",
}


def official_for_pid(pid, agents=None):
    """その pid の公式の状態。見つからなければ None。"""
    if not pid:
        return None
    for a in (agents if agents is not None else official_agents()):
        if a.get("pid") == pid:
            st = OFFICIAL_STATE.get((a.get("status"), a.get("waitingFor"))) or OFFICIAL_STATE.get((a.get("status"), None))
            return {"status": a.get("status"), "waiting_for": a.get("waitingFor"), "name": a.get("name") or "", "state": st}
    return None


BACKGROUND_STATE = {"blocked": ("確認待ち", "🔴"), "running": ("作業中", "🟢"), "queued": ("起動中?", "🔵"),
                    "done": ("終了", "⚪"), "failed": ("⛔ エラーで停止", "🔴"), "stopped": ("終了", "⚪")}


def background_sessions(agents=None):
    """agent view(claude agents)で動いている背景セッション。端末を持たないので tty では見つからない。"""
    out = []
    for a in (agents if agents is not None else official_agents()):
        if a.get("kind") != "background" or not a.get("id"):
            continue
        state, mark = BACKGROUND_STATE.get(a.get("state") or "", ("起動中?", "🔵"))
        if state == "終了":
            continue   # 終わったものは「いま」には出さない(過去は索引から出る)
        started = (a.get("startedAt") or 0) / 1000 or None
        out.append({
            "tab": "a-" + str(a["id"])[:8], "tty": "", "sid": a.get("sessionId") or ("agent:" + str(a["id"])),
            "state": state, "mark": mark, "ai": "Claude", "model": "", "account": "", "model_id": "",
            "model_style": cs.model_style(""), "cwd": a.get("cwd") or "", "project": project_name(a.get("cwd") or ""),
            "doing": redact(a.get("name") or ""), "task": redact(a.get("name") or ""), "topic": "",
            "client": (cs._clients.classify(cwd=a.get("cwd") or "", texts=(a.get("name") or "",)) if cs._clients else None),
            "state_for": None, "ago": (time.time() - started) if started else None, "started": started,
            "mem_mb": 0, "pid": None, "transcript": "", "today_requests": 0, "today_requests_partial": False,
            "subagents": {}, "tools": None, "loop": None, "limit": None, "group_label": "", "group_rgb": None,
            "background": True, "project_hint": "",
        })
    return out


def pick_ai(prefer="", accounts_now=None):
    """まとめ役が使う「いま空いている AI」の決め方。上限に当たっていない方を選ぶ。

    返り値: {"ai": "Claude"|"Codex", "profile": <Claude のアカウント名 or "">, "why": 理由}
    どちらも上限なら ai="" と理由を返す(勝手に投げない)。
    """
    acc = accounts_now if accounts_now is not None else accounts()
    # ログイン済みの目印: 設定にメールがある(logged_in は accounts() では引かない。CLI を呼ぶと遅いため)
    claude = [a for a in acc if a.get("ai") == "Claude" and a.get("logged_in") is not False and (a.get("email") or a.get("profile") == "default")]
    codex = next((a for a in acc if a.get("ai") == "Codex"), None)
    free_claude = [a for a in claude if not ((a.get("limit") or {}).get("active"))]
    cx_usage = (codex or {}).get("usage") or {}
    codex_free = (codex is not None and not ((codex.get("limit") or {}).get("active"))
                  and codex.get("logged_in") is not False and (cx_usage.get("used_percent") or 0) < 100)
    order = [("Codex", None), ("Claude", None)] if prefer == "Codex" else [("Claude", None), ("Codex", None)]
    for ai, _ in order:
        if ai == "Claude" and free_claude:
            a = free_claude[0]
            return {"ai": "Claude", "profile": "" if a.get("profile") == "default" else (a.get("profile") or ""),
                    "why": "Claude が空いている" + (f"(アカウント {a.get('profile')})" if a.get("profile") not in ("default", None) else "")}
        if ai == "Codex" and codex_free:
            return {"ai": "Codex", "profile": "", "why": "Claude が上限なので Codex に回す" if prefer != "Codex" else "Codex が空いている"}
    busy = []
    for a in claude:
        lim = a.get("limit") or {}
        if lim.get("active"):
            busy.append(f"Claude({a.get('profile')}) は {lim.get('resets') or '時刻不明'} まで上限")
    if codex and (((codex.get("limit") or {}).get("active")) or (cx_usage.get("used_percent") or 0) >= 100):
        when = (codex.get("limit") or {}).get("resets")
        if not when and cx_usage.get("resets_at"):
            when = time.strftime("%m/%d %H:%M", time.localtime(cx_usage["resets_at"]))
        busy.append(f"Codex は {when or '時刻不明'} まで上限")
    return {"ai": "", "profile": "", "why": "・".join(busy) or "使えるアカウントが無い"}


# ---------------------------------------------------------------- セッション ----
def _tools_of(t):
    return session_tools(t["transcript"]) if t.get("transcript") and not (t.get("ai") or "").startswith("Codex") else None


def _tools_brief(t):
    tl = _tools_of(t)
    if not tl:
        return None
    top = lambda d: sorted(d.items(), key=lambda kv: -kv[1])[:6]
    return {"skills": top(tl["skills"]), "mcp": top(tl["mcp"])} if (tl["skills"] or tl["mcp"]) else None


def sessions(procs=None, with_official=True):
    """cs.classify() の結果を JSON 化できる形に整える(件数は cs と同じ)。"""
    now = time.time()
    tabs = cs.classify(cs.iterm_sessions(), procs or cs.processes())
    out = []
    for t in tabs:
        n_today, partial = today_requests(t.get("transcript", ""), t.get("ai", ""))
        out.append({
            "tab": f"{t['win']}-{t['tab']}",
            "tty": t.get("tty", ""),
            "sid": t.get("sid", ""),
            "state": t["state"], "mark": t["mark"],
            "ai": t.get("ai", ""), "model": t.get("model", ""), "account": t.get("account", ""),
            "model_id": t.get("model_id", ""),
            "model_style": t.get("model_style") or (cs.model_style(t.get("model_id", "")) if t.get("ai") else None),
            "cwd": t.get("cwd", ""), "project": project_name(t.get("cwd", "")),
            "project_hint": project_hint(_tools_of(t), t.get("cwd", "")),
            "doing": redact(t.get("doing", "")),
            "task": redact(t.get("task", "")),
            "topic": redact(t.get("title_topic", "")),
            "client": t.get("client"),
            "state_for": (now - t["state_since"]) if t.get("state_since") else None,
            "ago": t.get("ago"),
            "started": t.get("started"),
            "mem_mb": round(t.get("mem", 0) / 1024),
            "pid": t.get("pid"),
            "transcript": t.get("transcript", ""),
            "today_requests": n_today, "today_requests_partial": partial,
            "subagents": t.get("subagents") or {},
            "tools": _tools_brief(t),
            "loop": loop_state(session_tools(t["transcript"]) if t.get("transcript") and not (t.get("ai") or "").startswith("Codex") else None),
            "group_label": "", "group_rgb": None,
            "limit": with_active(codex_limit(t.get("doing")) if (t.get("ai") or "").startswith("Codex")
                                 else claude_limit(t["transcript"]) if t.get("transcript") else None),
        })
    if with_official:
        agents = official_agents()
        for x in out:   # hook が無い/まだ書かれていないセッションは、公式の状態で補う
            off = official_for_pid(x.get("pid"), agents)
            if off:
                x["official"] = off
                if off["state"] and x["state"] in ("起動中?", "終了(古い題名)", ""):
                    x["state"] = off["state"]
                    x["mark"] = {"確認待ち": "🔴", "返答待ち": "🟡", "作業中": "🟢"}.get(off["state"], x["mark"])
                    if not x.get("task") and off["name"]:
                        x["task"] = redact(off["name"])
        seen_tty = {x.get("tty") for x in out}
        for b in background_sessions(agents):
            if b["sid"] not in {x.get("sid") for x in out}:
                out.append(b)
    return [apply_group(x) for x in out]


def attention(sess):
    """利用者が対応すべきもの。上から順に: ⚠確認待ち → 返答済みで長く放置 → 確認画面で停止。"""
    items = []
    for s in sess:
        why = None
        rank = None
        if s["state"] == "確認待ち":
            why, rank = "⚠ 確認待ち(承認か返事が要る)", 0
        elif s["state"] in ("返答待ち", "codex 返答待ち") and (s.get("loop") or {}).get("wake"):
            continue   # /loop が次に自分で起きる。人の番ではない
        elif s["state"] in ("返答待ち", "codex 返答待ち") and (s.get("state_for") or 0) >= IDLE_LONG:
            why, rank = f"返答済みのまま {fmt_dur(s['state_for'])} 放置", 1
        elif s["state"] == "codex 停止":
            why, rank = "Codex が止まっている: " + (s.get("doing") or "")[:80], 0
        elif s["state"] == "確認画面で停止":
            why, rank = "フォルダ信頼の確認画面で止まっている(未起動)", 2
        elif s["state"] == "起動中?":
            why, rank = "セッション記録が無い(起動途中か固まっている)", 2
        if why:
            items.append({**s, "why": why, "rank": rank})
    items.sort(key=lambda x: (x["rank"], -(x.get("state_for") or 0)))
    return items


def _group_summary(rows):
    counts = {}
    for s in rows:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    latest = max(rows, key=lambda s: -(s.get("ago") if s.get("ago") is not None else 1e12))
    return {
        "sessions": len(rows),
        "active": sum(1 for s in rows if s["mark"] in ("🟢", "🟩")),
        "states": counts,
        "latest_task": latest.get("task", ""),
        "latest_tab": latest["tab"],
        "last_update_ago": min((s["ago"] for s in rows if s.get("ago") is not None), default=None),
        "today_requests": sum(s["today_requests"] for s in rows),
        "today_requests_partial": any(s["today_requests_partial"] for s in rows),
        "tabs": [s["tab"] for s in rows],
    }


def grouped(sess):
    """顧客プロダクト(顧客ごと) → 自社プロジェクト(フォルダごと) の順にまとめる。"""
    clients, projects = {}, {}
    for s in sess:
        if s["mark"] == "⚪":
            continue
        c = s.get("client")
        if c:
            g = clients.setdefault(c["id"], {"client": c, "rows": []})
        else:
            g = projects.setdefault(s["cwd"] or "?", {"cwd": s["cwd"], "name": s["project"] or "?", "rows": []})
        g["rows"].append(s)
    cl = [{"id": k, **v["client"], **_group_summary(v["rows"])} for k, v in clients.items()]
    pr = [{"cwd": v["cwd"], "name": v["name"], **_group_summary(v["rows"])} for v in projects.values()]
    cl.sort(key=lambda g: -g["active"])
    pr.sort(key=lambda g: (-g["active"], g["last_update_ago"] if g["last_update_ago"] is not None else 1e12))
    return cl, pr


# ---------------------------------------------------------------- このMac ----
def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", str(e), -1


def machine(procs=None):
    """メモリの使用率・圧縮・スワップ・内訳と、JetsamEvent(メモリ不足の強制終了)の回数。"""
    out = {"ok": True, "reason": ""}
    vm, err, rc = _run(["vm_stat"])
    if rc != 0:
        return {"ok": False, "reason": f"vm_stat 失敗: {err.strip()}"}
    page = int(re.search(r"page size of (\d+)", vm).group(1))
    v = {m.group(1): int(m.group(2)) for m in re.finditer(r"^(.+?):\s+(\d+)\.", vm, re.M)}
    total = int(_run(["sysctl", "-n", "hw.memsize"])[0].strip() or 0)
    wired = v.get("Pages wired down", 0) * page
    anon = v.get("Anonymous pages", 0) * page
    purgeable = v.get("Pages purgeable", 0) * page
    compressed = v.get("Pages occupied by compressor", 0) * page
    stored = v.get("Pages stored in compressor", 0) * page
    # Activity Monitor の「使用済み」= App(anonymous−purgeable) + 確保済み(wired) + 圧縮
    used = anon - purgeable + wired + compressed
    swap = _run(["sysctl", "-n", "vm.swapusage"])[0]
    m = re.search(r"used = ([\d.]+)M", swap)
    swap_used_mb = float(m.group(1)) if m else None
    mp, _, _ = _run(["memory_pressure"], timeout=15)
    m = re.search(r"free percentage:\s*(\d+)%", mp)
    free_pct = int(m.group(1)) if m else None
    out.update(total_gb=round(total / 2**30, 1), used_gb=round(used / 2**30, 2),
               used_pct=round(used / total * 100) if total else None,
               free_pct_kernel=free_pct,
               compressed_gb=round(compressed / 2**30, 2), compressed_saves_gb=round((stored - compressed) / 2**30, 2),
               wired_gb=round(wired / 2**30, 2), swap_used_mb=swap_used_mb)
    # 内訳(RSS 合計。共有メモリを重複して数えるので合計は used_gb より大きくなり得る)
    procs = procs or cs.processes()
    cat = {"Chrome": 0, "AI(claude+codex)": 0, "Docker": 0, "iTerm": 0, "その他": 0}
    kids = {}
    for p, x in procs.items():
        kids.setdefault(x["ppid"], []).append(p)
    ai_roots = [p for p, x in procs.items() if (cs.is_claude(x["cmd"]) or cs.is_codex(x["cmd"]))
                and not (cs.is_claude(procs.get(x["ppid"], {}).get("cmd", "")) or cs.is_codex(procs.get(x["ppid"], {}).get("cmd", "")))]
    ai_pids = set()
    stack = list(ai_roots)
    while stack:
        p = stack.pop()
        if p in ai_pids:
            continue
        ai_pids.add(p)
        stack.extend(kids.get(p, []))
    for p, x in procs.items():
        c = x["cmd"]
        if p in ai_pids:
            cat["AI(claude+codex)"] += x["rss"]
        elif "Google Chrome" in c or "/Chrome" in c:
            cat["Chrome"] += x["rss"]
        elif "ocker" in c and ("com.docker" in c or "Docker" in c):
            cat["Docker"] += x["rss"]
        elif "iTerm" in c:
            cat["iTerm"] += x["rss"]
        else:
            cat["その他"] += x["rss"]
    out["breakdown_gb"] = {k: round(v / 2**20, 2) for k, v in cat.items()}
    out["breakdown_note"] = "RSS合計(共有分を重複して数える)。AI は claude/codex とその子プロセス"
    out["jetsam"] = jetsam()
    return out


def jetsam():
    """/Library/Logs/DiagnosticReports/JetsamEvent-*.ips の件数(今日・直近24h)と最後の1件。
    1行目がヘッダJSON(timestamp)、2行目以降が本体JSON(processes[].reason が殺された理由)。"""
    files = sorted(glob.glob("/Library/Logs/DiagnosticReports/JetsamEvent-*.ips"))
    res = {"ok": True, "today": 0, "last24h": 0, "last": None, "last_killed": None, "files": len(files)}
    if not files:
        return res
    now = time.time()
    today = local_today()
    last_ts = None
    unreadable = 0
    for f in files:
        try:
            with open(f, errors="replace") as fh:
                hdr = json.loads(fh.readline())
            ts = dt.datetime.strptime(hdr["timestamp"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (OSError, ValueError, KeyError):
            unreadable += 1
            continue
        if ts > now - 86400:
            res["last24h"] += 1
        if dt.date.fromtimestamp(ts) == today:
            res["today"] += 1
        if last_ts is None or ts > last_ts:
            last_ts, last_file = ts, f
    if unreadable:
        res["ok"] = False
        res["reason"] = f"{unreadable} 件が読めない(権限)"
    if last_ts:
        res["last"] = last_ts
        res["last_ago"] = now - last_ts
        try:
            with open(last_file, errors="replace") as fh:
                fh.readline()
                body = json.loads(fh.read())
            killed = [p for p in body.get("processes", []) if p.get("reason")]
            res["last_killed"] = [{"name": p.get("name"), "reason": p.get("reason"),
                                   "mb": round(p.get("rpages", 0) * body.get("memoryStatus", {}).get("pageSize", 16384) / 2**20)}
                                  for p in killed][:5]
            res["largest_process"] = body.get("largestProcess")
        except (OSError, ValueError):
            res["last_killed"] = None
    return res


# ---------------------------------------------------------------- macmini ----
# ---------------------------------------------------------------- 遠隔の無人ジョブ ----
# 既定は汎用の探り(PM2 と cron の件数)。利用者固有の探り方は ~/.aiboard/config.json の
# "remote_module"(REMOTE_SCRIPT と parse(text) を持つ Python ファイル)で差し替える。
REMOTE_SCRIPT = r'''
echo "@@meta"; date +%s; hostname; uptime
echo "@@pm2"; NB=$(ls -d ~/.nvm/versions/node/*/bin 2>&1 | tail -1); PATH="$NB:/opt/homebrew/bin:/usr/local/bin:$PATH"
pm2 jlist 2>&1 | /usr/bin/python3 -c '
import json,sys
raw=sys.stdin.read()
try: L=json.loads(raw)
except Exception as e: print("ERR "+str(e)+" "+raw[:200]); sys.exit(0)
for p in L:
    e=p.get("pm2_env",{}); print(json.dumps({"name":p.get("name"),"status":e.get("status"),"restarts":e.get("restart_time")}))
'
echo "@@cron"; crontab -l 2>&1 | grep -v "^#" | grep -c .
echo "@@end"
'''


def _parse_remote(text):
    sec, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip(); sec[cur] = []
        elif cur:
            sec[cur].append(line)
    meta = sec.get("meta", [])
    data = {"host": meta[1] if len(meta) > 1 else "", "load1": None, "remote_now": int(meta[0]) if meta and meta[0].isdigit() else None}
    m = re.search(r"load averages?: ([\d.]+)", " ".join(meta))
    if m:
        data["load1"] = float(m.group(1))
    pm2 = []
    for l in sec.get("pm2", []):
        try:
            pm2.append(json.loads(l))
        except ValueError:
            continue
    data["pm2"] = {"total": len(pm2), "online": sum(1 for p in pm2 if p.get("status") == "online"),
                   "bad": [p["name"] for p in pm2 if p.get("status") != "online"]}
    cron = sec.get("cron", [])
    data["cron_wrap"] = {"today": {"ok": None, "FAIL": None}, "jobs": int(cron[0]) if cron and cron[0].strip().isdigit() else 0}
    data["ytfactory"] = None
    data["cronai"] = []
    return data


def _remote_impl():
    """(REMOTE_SCRIPT, parse) を返す。config の remote_module があればそれを読む(失敗は理由つきで汎用に落とす)。"""
    path = aiboard_paths.config().get("remote_module")
    if path:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("aiboard_remote_probe", os.path.expanduser(path))
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            return mod.REMOTE_SCRIPT, mod.parse, ""
        except Exception as e:  # 私物モジュールの失敗で盤全体を止めない
            return REMOTE_SCRIPT, _parse_remote, f"remote_module を読めない: {e}"
    return REMOTE_SCRIPT, _parse_remote, ""


def macmini(force=False):
    """macmini の無人AIジョブの状態。60秒キャッシュ(ステージング内 macmini_cache.json)。
    ssh 失敗時は ok=False と reason を返し、前回成功分があれば stale として添える。"""
    now = time.time()
    cache = {}
    try:
        cache = json.load(open(MACMINI_CACHE))
    except (OSError, ValueError):
        pass
    if not force and cache.get("fetched") and now - cache["fetched"] < MACMINI_TTL:
        return cache["data"]
    errors = []
    if not MACMINI_HOSTS:
        return {"ok": False, "reason": "未設定(~/.aiboard/config.json の remote_hosts)", "unconfigured": True}
    for host in MACMINI_HOSTS:
        t0 = time.time()
        try:
            script, parse, impl_err = _remote_impl()
            r = subprocess.run(["ssh"] + SSH_OPTS + [host, script], capture_output=True, text=True, timeout=40)
        except subprocess.TimeoutExpired:
            errors.append(f"{host}: 40秒で応答なし")
            continue
        except OSError as e:
            errors.append(f"{host}: {e}")
            continue
        if r.returncode != 0 or "@@end" not in r.stdout:
            errors.append(f"{host}: rc={r.returncode} {r.stderr.strip()[:200]}")
            continue
        try:
            data = parse(r.stdout)
            if impl_err:
                data["impl_error"] = impl_err
        except ValueError as e:
            errors.append(f"{host}: {e}")
            continue
        data.update(host=host, fetched=now, took=round(time.time() - t0, 1), ok=True, reason="")
        json.dump({"fetched": now, "data": data}, open(MACMINI_CACHE + ".tmp", "w"), ensure_ascii=False)
        os.replace(MACMINI_CACHE + ".tmp", MACMINI_CACHE)
        return data
    stale = cache.get("data") if cache.get("data", {}).get("ok") else None
    data = {"ok": False, "reason": "取得失敗: " + " / ".join(errors), "fetched": now,
            "stale": stale, "stale_age": (now - stale["fetched"]) if stale else None}
    json.dump({"fetched": now, "data": data}, open(MACMINI_CACHE + ".tmp", "w"), ensure_ascii=False)
    os.replace(MACMINI_CACHE + ".tmp", MACMINI_CACHE)
    return data


# ---------------------------------------------------------------- 全体 ----
def accounts():
    """持っている AI アカウントの一覧と、それぞれの上限の状態。読むだけ。

    Claude: 既定の ~/.claude と ~/.claude-profiles/<名前>。各々の .claude.json のメールと、直近 48 時間の記録で最後に当たった上限。
    Codex: 新しい rollout の rate_limits(使用率・窓・リセット時刻)と、最後の「try again at」。
    """
    out = []
    homes = [("default", HOME + "/.claude", HOME + "/.claude.json")] + [
        (os.path.basename(d), d, os.path.join(d, ".claude.json")) for d in sorted(glob.glob(HOME + "/.claude-profiles/*")) if os.path.isdir(d)]
    now = time.time()
    for name, base, cfg in homes:
        acct = {}
        try:
            with open(cfg, encoding="utf-8") as f:
                acct = (json.load(f).get("oauthAccount") or {})
        except (OSError, ValueError):
            pass
        last = None
        for fp in glob.glob(os.path.join(base, "projects", "*", "*.jsonl")):
            try:
                if now - os.path.getmtime(fp) > 48 * 3600:
                    continue
            except OSError:
                continue
            lim = claude_limit(fp)
            if lim and (not last or lim["at"] > last["at"]):
                last = dict(lim, session=os.path.basename(fp)[:-6])
        out.append({"ai": "Claude", "profile": name, "config_dir": base, "email": acct.get("emailAddress") or "",
                    "org": acct.get("organizationName") or "", "plan": acct.get("billingType") or "", "limit": with_active(last)})
    # Codex
    cx = {"ai": "Codex", "profile": "codex", "config_dir": HOME + "/.codex", "email": "", "org": "", "plan": "", "limit": None, "usage": None}
    files = sorted(glob.glob(HOME + "/.codex/sessions/*/*/*/rollout-*.jsonl"), key=os.path.getmtime, reverse=True)[:40]
    for fp in files:
        try:
            with open(fp, "rb") as f:
                f.seek(max(0, os.path.getsize(fp) - 300_000)); tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if cx["usage"] is None:
            for m in re.finditer(r'"primary":\{"used_percent":([\d.]+),"window_minutes":(\d+),"resets_at":(\d+)\}', tail):
                cx["usage"] = {"used_percent": float(m.group(1)), "window_minutes": int(m.group(2)), "resets_at": int(m.group(3))}
        if cx["limit"] is None:
            ms = list(CODEX_LIMIT_RE.finditer(tail))
            if ms:
                cx["limit"] = {"kind": "usage", "resets": ms[-1].group(2), "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(fp))), "text": ms[-1].group(0)[:140]}
        if cx["usage"] is not None and cx["limit"] is not None:
            break
    cx["limit"] = with_active(cx["limit"])
    if cx["usage"] and cx["limit"] and cx["limit"]["resets_at"] is None:
        cx["limit"]["resets_at"] = cx["usage"]["resets_at"]; cx["limit"]["active"] = time.time() < cx["usage"]["resets_at"]
    out.append(cx)
    return out


AI_CLI_DEFS = [
    {"id": "claude", "label": "Claude Code", "cmd": "claude"},
    {"id": "codex", "label": "Codex", "cmd": "codex"},
    {"id": "gemini", "label": "Gemini CLI", "cmd": "gemini"},
    {"id": "grok", "label": "Grok CLI", "cmd": "grok"},
    {"id": "cursor", "label": "Cursor Agent", "cmd": "cursor-agent"},
]
_CLIS = {"t": 0, "val": []}


def _which_all(cmds, timeout=15):
    """入っている CLI の場所を 1 回のシェルで調べる(1 つずつ呼ぶと遅い)。"""
    lines = []
    for c in cmds:
        lines.append('echo "{0}\t$(command -v {0} 2>&1 | head -1)"'.format(c))
    try:
        r = subprocess.run(["/bin/zsh", "-l", "-c", "; ".join(lines)], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            v = v.strip()
            out[k.strip()] = v if v.startswith("/") else ""
    return out


def _gemini_auth():
    """Gemini CLI は Google アカウントで入る。~/.gemini/google_accounts.json の active を見る。"""
    try:
        with open(os.path.join(HOME, ".gemini", "google_accounts.json"), encoding="utf-8") as f:
            d = json.load(f)
        who = d.get("active") or ""
        return (bool(who), who, "Google アカウント")
    except (OSError, ValueError):
        return (False, "", "Google アカウント")


def _grok_auth():
    """Grok CLI は API 鍵(GROK_API_KEY か設定ファイル)。鍵そのものは読まない・出さない。"""
    if os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY"):
        return (True, "", "API 鍵(環境変数)")
    for name in ("user-settings.json", "settings.json"):
        try:
            with open(os.path.join(HOME, ".grok", name), encoding="utf-8") as f:
                d = json.load(f)
            if any("key" in k.lower() or "token" in k.lower() for k in (d or {})):
                return (True, "", "API 鍵(設定ファイル)")
        except (OSError, ValueError):
            pass
    return (False, "", "API 鍵(GROK_API_KEY)")


def _cursor_auth():
    try:
        r = subprocess.run(["/bin/zsh", "-l", "-c", "command cursor-agent status"], capture_output=True, text=True, timeout=25)
        txt = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", r.stdout + r.stderr)
        if re.search(r"Not logged in", txt, re.I):
            return (False, "", "Cursor")
        m = re.search(r"(?:Logged in as|Email)[:\s]+(\S+@\S+)", txt)
        return (bool(re.search(r"Logged in", txt, re.I)), m.group(1) if m else "", "Cursor")
    except (subprocess.TimeoutExpired, OSError):
        return (None, "", "Cursor")


def ai_clis(force=False, logins=None):
    """この Mac に入っている AI の CLI と、その認証の状態。鍵そのものは読まない。

    claude / codex は各 CLI に聞く(login_status)。gemini は Google アカウントの設定、
    grok は API 鍵の有無、cursor-agent は status の文言で判断する。
    """
    if not force and time.time() - _CLIS["t"] < 120 and _CLIS["val"]:
        return _CLIS["val"]
    where = _which_all([d["cmd"] for d in AI_CLI_DEFS])
    st = {(x["profile"] if x["ai"] == "Claude" else "codex"): x for x in (logins if logins is not None else login_status())}
    out = []
    for d in AI_CLI_DEFS:
        row = dict(d, installed=bool(where.get(d["cmd"])), path=where.get(d["cmd"], ""),
                   logged_in=None, who="", how="", login_cmd="", logout_cmd="", note="")
        if not row["installed"]:
            row["note"] = "入っていない"
            out.append(row)
            continue
        if d["id"] == "claude":
            base = st.get("default") or {}
            row.update(logged_in=base.get("logged_in"), who=base.get("email", ""), how=base.get("method", "claude.ai"),
                       login_cmd="env -u CLAUDE_CONFIG_DIR command claude auth login",
                       logout_cmd="env -u CLAUDE_CONFIG_DIR command claude auth logout",
                       note="アカウントの切替は上の一覧から")
        elif d["id"] == "codex":
            cx = st.get("codex") or {}
            row.update(logged_in=cx.get("logged_in"), who="", how=cx.get("method", "ChatGPT"),
                       login_cmd="command codex login", logout_cmd="command codex logout")
        elif d["id"] == "gemini":
            ok, who, how = _gemini_auth()
            row.update(logged_in=ok, who=who, how=how, login_cmd="command gemini", note="初回起動で Google のログインに進む")
        elif d["id"] == "grok":
            ok, who, how = _grok_auth()
            row.update(logged_in=ok, who=who, how=how, login_cmd="",
                       note="" if ok else "GROK_API_KEY を設定する(AIBoard は鍵を預かりません)")
        elif d["id"] == "cursor":
            ok, who, how = _cursor_auth()
            row.update(logged_in=ok, who=who, how=how, login_cmd="command cursor-agent login", logout_cmd="command cursor-agent logout")
        out.append(row)
    _CLIS.update(t=time.time(), val=out)
    return out


def accounts_full(sess=None, logins=None):
    """アカウント 1 か所ぶんの全部: 誰か(公式のログイン状態)・プラン・上限・いま何本動いているか。

    メールとプランは `claude auth status --json`(login_status)を正とする。設定ファイルは古いことがある
    (実際、ログイン済みのアカウントを「未ログイン」と出していた。2026-09-18 実測)。
    """
    acc = accounts()
    st = {(x.get("ai"), x.get("profile")): x for x in (logins if logins is not None else login_status()) if isinstance(x, dict)}
    live = sess if sess is not None else []
    for a in acc:
        if not isinstance(a, dict) or "ai" not in a:
            continue   # 形の違う行は触らない(ここで落ちると API ごと 500 になる)
        s0 = st.get((a.get("ai"), a.get("profile"))) or {}
        if s0.get("email"):
            a["email"] = s0["email"]
        if s0.get("plan"):
            a["plan"] = s0["plan"]
        if s0.get("org"):
            a["org"] = s0["org"]
        a["logged_in"] = s0.get("logged_in")
        a["method"] = s0.get("method", "")
        a["auth_error"] = s0.get("error", "")
        a["from_file"] = bool(s0.get("from_file"))
        name = "" if a.get("profile") == "default" else (a.get("profile") or "")
        if a.get("ai") == "Codex":
            a["running"] = sum(1 for x in live if (x.get("ai") or "").startswith("Codex"))
        else:
            a["running"] = sum(1 for x in live if (x.get("ai") or "").startswith("Claude") and (x.get("account") or "") == name)
    return acc


def login_status():
    """各アカウントのログイン状態を、それぞれの CLI 自身に聞く(推測しない)。数秒かかるので呼ぶ側で使い回す。"""
    out = []
    homes = [("default", HOME + "/.claude")] + [(os.path.basename(d), d) for d in sorted(glob.glob(HOME + "/.claude-profiles/*")) if os.path.isdir(d)]
    for name, base in homes:
        env = dict(os.environ)
        if name == "default":
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = base
        st = {"ai": "Claude", "profile": name, "config_dir": base, "logged_in": None, "method": "", "error": ""}
        j = {}
        try:
            r = subprocess.run(["/bin/zsh", "-l", "-c", "command claude auth status --json"], env=env, capture_output=True, text=True, timeout=20)
            j = json.loads(r.stdout[r.stdout.find("{"):]) if "{" in r.stdout else {}
            st["logged_in"] = bool(j.get("loggedIn")); st["method"] = j.get("authMethod") or ""
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            st["error"] = f"{type(e).__name__}"
        # 正はこの CLI の答え。設定ファイルは CLI が答えられなかった時だけ使う(古い値が残っていることがある)
        st.update(email=j.get("email") or "", org=j.get("orgName") or "", plan=j.get("subscriptionType") or "")
        if not st["email"]:
            try:
                with open(os.path.join(HOME, ".claude.json") if name == "default" else os.path.join(base, ".claude.json"), encoding="utf-8") as f:
                    a = json.load(f).get("oauthAccount") or {}
                st.update(email=a.get("emailAddress") or "", org=a.get("organizationName") or "", plan=a.get("billingType") or "", from_file=True)
            except (OSError, ValueError):
                pass
        out.append(st)
    cx = {"ai": "Codex", "profile": "codex", "config_dir": HOME + "/.codex", "logged_in": None, "method": "", "error": "", "email": "", "org": "", "plan": ""}
    try:
        r = subprocess.run(["/bin/zsh", "-l", "-c", "command codex login status"], capture_output=True, text=True, timeout=20)
        txt = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\\\)|\x1b\][^A-Za-z]*[A-Za-z=][^\n]*?(?=Logged|Not)", "", r.stdout + r.stderr)
        cx["logged_in"] = "Logged in" in txt
        m = re.search(r"Logged in using ([A-Za-z ]+)", txt); cx["method"] = m.group(1).strip() if m else ""
    except (subprocess.TimeoutExpired, OSError) as e:
        cx["error"] = type(e).__name__
    out.append(cx)
    return out


def settings_info():
    import aiboard_paths as ap
    cfg = ap.config()
    clients_path = ap.data("clients.json")
    try:
        n_clients = len(json.load(open(clients_path, encoding="utf-8")).get("clients", []))
    except (OSError, ValueError):
        n_clients = 0
    hook = subprocess.run([sys.executable, os.path.join(HERE, "install_hook.py"), "--check"], capture_output=True, text=True)
    return {"data_dir": ap.DATA, "clients_file": clients_path, "clients": n_clients, "config_file": ap.data("config.json"),
            "remote_hosts": cfg.get("remote_hosts", []), "hook_installed": hook.returncode == 0,
            "claude_settings": os.path.join(HOME, ".claude", "settings.json"), "codex_config": os.path.join(HOME, ".codex", "config.toml")}


def snapshot(with_macmini=True):
    t0 = time.time()
    procs = cs.processes()
    sess = sessions(procs)
    cl, pr = grouped(sess)
    snap = {
        "time": time.time(),
        "sessions": sess,
        "attention": attention(sess),
        "clients": cl,
        "projects": pr,
        "machine": machine(procs),
        "macmini": macmini() if with_macmini else {"ok": False, "reason": "未取得"},
        "iterm": {"ok": not cs.OSA_ERROR, "error": cs.OSA_ERROR,
                  "stale_for": (time.time() - cs._LAST_ITERM["fail_t"]) if cs._LAST_ITERM.get("fail_t") else 0},
        "counts": {
            "your_turn": sum(1 for s in sess if s["state"] == "確認待ち"),
            "working": sum(1 for s in sess if s["mark"] in ("🟢", "🟩")),
            "waiting": sum(1 for s in sess if s["state"] in ("返答待ち", "codex 返答待ち")),
            "tabs": len(sess),
        },
    }
    snap["took"] = round(time.time() - t0, 2)
    return snap


# ---------------------------------------------------------------- 詳細(1タブ) ----
def _load_describe_tool():
    """hooks/tab-status.py の describe_tool を借りる(ツール操作の言い方を1か所にする)。"""
    import importlib.util
    p = os.path.join(HOME, ".claude", "hooks", "tab-status.py")
    try:
        spec = importlib.util.spec_from_file_location("tab_status", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.describe_tool
    except Exception:  # フックが無くても詳細は出す
        return lambda name, inp: name


def timeline_claude(path, limit=20, tail_bytes=3_000_000, with_text=False, sidechain_ok=False):
    """トランスクリプト末尾から、最後の依頼とその後のツール操作(最大 limit 件)を時系列で。
    with_text=True なら AI の返答本文も入れる(過去の会話を読む用)。"""
    describe = _load_describe_tool()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in chunk.splitlines():
        if '"type":"user"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if sidechain_ok:
                d["isSidechain"] = False   # サブエージェントの記録は全行 isSidechain=true なので外す
            text = cs.prompt_text(d)
            if text:
                events.append({"t": d.get("timestamp", ""), "kind": "依頼", "text": text[:2000]})
        elif '"type":"assistant"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain") and not sidechain_ok:
                continue
            for b in d.get("message", {}).get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    events.append({"t": d.get("timestamp", ""), "kind": "操作",
                                   "text": describe(b.get("name", ""), b.get("input") or {})})
                elif b.get("type") == "text" and with_text and b.get("text", "").strip():
                    events.append({"t": d.get("timestamp", ""), "kind": "返答", "text": b["text"][:4000]})
    # 最後の依頼以降を優先し、足りなければその前も足す
    idx = max((i for i, e in enumerate(events) if e["kind"] == "依頼"), default=0)
    picked = events[idx:]
    if len(picked) > limit:
        picked = [picked[0]] + picked[-(limit - 1):]
    elif len(picked) < limit:
        picked = events[max(0, idx - (limit - len(picked))):idx] + picked
    for e in picked:
        e["text"] = redact(e["text"])
    return picked


def timeline_codex(path, limit=20, with_text=False):
    events = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"response_item"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pl = d.get("payload") or {}
                ts = d.get("timestamp", "")
                if pl.get("type") in ("function_call", "custom_tool_call"):
                    arg = str(pl.get("arguments") or pl.get("input") or "")
                    cmd = re.search(r'cmd["\']?\s*[:=]\s*["\']([^"\']+)', arg)
                    files = re.findall(r"\*\*\* (?:Update|Add|Delete) File: (\S+)", arg)
                    desc = ", ".join(os.path.basename(x) for x in files) if files else " ".join((cmd.group(1) if cmd else arg).split())[:120]
                    events.append({"t": ts, "kind": "操作", "text": f"{pl.get('name', '操作')}: {desc}"})
                elif pl.get("type") == "message":
                    txt = "".join(x.get("text", "") for x in (pl.get("content") or []) if isinstance(x, dict))
                    if pl.get("role") == "user" and txt.strip() and not txt.lstrip().startswith("<"):
                        events.append({"t": ts, "kind": "依頼", "text": " ".join(txt.split())[:2000]})
                    elif pl.get("role") == "assistant" and with_text and txt.strip():
                        events.append({"t": ts, "kind": "返答", "text": txt[:4000]})
    except OSError:
        return []
    idx = max((i for i, e in enumerate(events) if e["kind"] == "依頼"), default=0)
    picked = events[idx:]
    if len(picked) > limit:
        picked = [picked[0]] + picked[-(limit - 1):]
    for e in picked:
        e["text"] = redact(e["text"])
    return picked


def screen_tail(tab, lines=40, tty=None):
    """iTerm の画面(contents of session)の末尾。AppleScript は読むだけ。tty があればそれで指す(位置ずれに強い)。"""
    m = re.fullmatch(r"(\d+)-(\d+)", tab)
    if not m:
        return None
    txt = cs.screen_text(int(m.group(1)), int(m.group(2)), tty=tty)
    rows = [r.rstrip() for r in txt.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return redact("\n".join(rows[-lines:]))


def detail(tab, sess=None):
    """1タブ分の詳細(記録・直近の流れ・画面末尾)。秘密情報は redact() を通す。"""
    sess = sess or sessions()
    s = next((x for x in sess if x["tab"] == tab), None)
    if not s:
        return {"ok": False, "reason": f"タブ {tab} が無い"}
    tl = []
    if s["transcript"]:
        tl = timeline_codex(s["transcript"]) if s["ai"].startswith("Codex") else timeline_claude(s["transcript"])
    return {"ok": True, "session": s, "timeline": tl, "screen": screen_tail(tab, tty=s.get("tty"))}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "macmini":
        print(json.dumps(macmini(force="--force" in sys.argv), ensure_ascii=False, indent=1))
    elif len(sys.argv) > 1 and sys.argv[1] == "detail":
        print(json.dumps(detail(sys.argv[2]), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
