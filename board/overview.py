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
import functools
import glob
import json
import os
import re
import shlex
import shutil
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


# 止まり方の見分け(2026-09-19 実測・母集団=30 日に更新された会話ログ 6,726 本):
#   認証 1,012(未ログイン 631・鍵が無効 199・OAuth 失効 145・期限切れ 37) / 上限 509 / クレジット切れ 11
#   一時的な失敗 57 ＋ スリープ 24 ＋ 応答が止まる 13 ＋ 到達不能 8 ＝ 102(これは自動で戻るので騒がない)
STOP_KINDS = [
    ("credits", re.compile(r"out of usage credits", re.I), "クレジットが尽きています"),
    ("login", re.compile(r"Not logged in", re.I), "ログインしていません"),
    ("apikey", re.compile(r"Invalid API key", re.I), "API キーが無効です"),
    ("oauth", re.compile(r"OAuth session expired", re.I), "OAuth の期限が切れました"),
    ("expired", re.compile(r"Login expired", re.I), "ログインの期限が切れました"),
]
TRANSIENT_RE = re.compile(r"went to sleep|response stopped arriving|Can't reach the API server|overloaded|"
                          r"\b5\d\d\b|timeout|ECONN|network error|fetch failed", re.I)
AUTH_RE = re.compile(r"(Not logged in|Invalid API key|OAuth session expired|Login expired|Please run /login)", re.I)


@cs.memo_by_file
def claude_auth_error(path):
    """記録の最後が「ログインが切れている」なら {text, at}。その後に本物の返答が来ていれば解けている(None)。

    直近 30 日で 1,012 回起きていて(未ログイン 631・鍵が無効 199・OAuth 失効 145・期限切れ 37)、
    盤には何も出ていなかった＝利用者には「なぜか進まない」としか見えなかった(2026-09-19 実測)。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 200_000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in chunk.splitlines():
        if '"assistant"' not in line:      # すきまの入った JSON も拾う(前に同じ取り落としをした)
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        txt = "".join(b.get("text", "") for b in (d.get("message", {}).get("content") or []) if isinstance(b, dict))
        if d.get("isApiErrorMessage"):
            kind = next((k for k, rx, _ in STOP_KINDS if rx.search(txt)), None)
            if kind:
                label = next(l for k, _, l in STOP_KINDS if k == kind)
                # 直し方が違うので分けて出す: 鍵が無効は「ログインし直す」ではなく「鍵の設定を直す」
                fix = "key" if kind == "apikey" else "billing" if kind == "credits" else "login"
                last = {"kind": kind, "fix": fix, "label": label, "text": txt[:140], "at": d.get("timestamp", "")}
            elif TRANSIENT_RE.search(txt):
                last = {"kind": "transient", "fix": "wait", "label": "一時的に失敗しました(自動で戻ります)",
                        "text": txt[:140], "at": d.get("timestamp", "")}
        elif (d.get("message", {}).get("model") or "") != "<synthetic>":
            last = None      # 本物の返答が来た = 止まりは解けている
    return last


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


# ---------------------------------------------------------------- 遠隔 ----
# 既定は切ってある。入れると「同じ LAN の中から、合言葉つきで」だけ届く。
# 読み取りと、判断待ちへの返事だけを通す(終了・起動・設定の変更は通さない)。
REMOTE_PATHS_READ = ("/m", "/m.js", "/m.webmanifest", "/m-icon.png", "/api/snapshot", "/api/conv", "/api/schedule", "/api/version")
REMOTE_PATHS_WRITE = ("/api/send",)


def sound_on():
    """判断待ちの音を鳴らすか(config.json の sound。既定は鳴らす)。"""
    import aiboard_paths as ap
    c = ap.config() or {}
    return bool(c.get("sound", True))


def sound_set(on):
    import aiboard_paths as ap
    p = ap.data("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["sound"] = bool(on)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    ap._cfg = None
    return bool(on)


def remote_config():
    import aiboard_paths as ap
    c = (ap.config() or {}).get("remote") or {}
    return {"enabled": bool(c.get("enabled")), "token": str(c.get("token") or "")}


def remote_set(enabled):
    """遠隔の入切。入れる時に合言葉を作る(呼ぶ側が持っていなければ)。設定ファイルは本人だけが読める形にする。"""
    import aiboard_paths as ap
    import secrets
    p = ap.data("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if enabled:
        # 入れるたびに合言葉を作り直す(切って入れ直せば、前の合言葉を知る端末は締め出される)
        cfg["remote"] = {"enabled": True, "token": secrets.token_urlsafe(24)}   # 外から渡せない(古い合言葉を戻せない)
    else:
        cfg["remote"] = {"enabled": False, "token": ""}
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    ap._cfg = None    # 読み直させる
    return cfg["remote"]


def remote_addrs():
    """この機械の、外から届く IPv4 の住所と、その種類。
    Wi-Fi/LAN(192.168・10・172.16-31)は同じ Wi-Fi から、Tailscale(100.64-127)は外出先から
    (自分で張った VPN。AIBoard は中継しない)。"""
    import ipaddress
    out = []
    try:
        r = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return out
    iface = ""
    for line in r.stdout.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":")[0]
        m = re.search(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        ip = m.group(1)
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback or a.is_link_local:
            continue
        if a in ipaddress.ip_network("100.64.0.0/10"):
            kind = "tailscale"
        elif a.is_private:
            kind = "lan"
        else:
            kind = iface
        out.append({"ip": ip, "iface": iface, "kind": kind})
    return out


def remote_urls(port):
    """同じ LAN(や自分の VPN)から開く URL。合言葉は付けない(画面で別に見せる)。"""
    return [f"http://{a['ip']}:{port}/m" for a in remote_addrs()]


def is_loopback(addr):
    a = str(addr or "")
    return a == "::1" or a.startswith("127.") or a.startswith("::ffff:127.")


def remote_allowed(path, write, addr, key, cfg=None):
    """この要求を通してよいか。返り値 (可否, 理由)。

    自分の機械からはこれまでどおり全部通す。外(同じ LAN)からは
    「遠隔が入っている」「合言葉が合う」「決まった道だけ」の 3 つが揃った時だけ。
    """
    if is_loopback(addr):
        return True, ""
    c = cfg if cfg is not None else remote_config()
    if not c.get("enabled"):
        return False, "遠隔は切ってあります"
    tok = str(c.get("token") or "")
    import hmac
    if not tok or not hmac.compare_digest(tok, str(key or "")):
        return False, "合言葉が違います"
    allowed = REMOTE_PATHS_WRITE if write else (REMOTE_PATHS_READ + REMOTE_PATHS_WRITE)
    if path not in allowed:
        return False, f"遠隔からは {path} を使えません"
    return True, ""


# ------------------------------------------------------------ まとめ役 ----
PLAN_MAX = 5
PLAN_PROMPT = """あなたは仕事を分解する係です。次の依頼を、**並行して別々のセッションで進められる**小さな仕事に分けてください。

規則:
- 1〜{n} 件。分ける必要が無ければ 1 件でよい
- 互いに依存しない(順番に実行しないと成り立たないものは 1 件にまとめる)
- 各件は「その 1 件だけ読めば作業を始められる」文にする
- 出力は **JSON 配列だけ**。説明や ``` は書かない
- 形: [{{"title": "20 字以内の見出し", "prompt": "その仕事への指示"}}]

依頼:
{text}
"""


def plan_tasks(text, ai=None, profile="", timeout=150):
    """依頼を並行できる小さな仕事に分ける。返り値 (件のリスト, 理由)。

    分解そのものを AI に頼むので、**実行はしない**。出た案は盤で人が選んでから動かす。
    """
    text = str(text or "").strip()
    if not (1 <= len(text) <= 4000):
        return [], "依頼文は 1〜4000 字"
    cmd = os.environ.get("AIBOARD_PLAN_CMD")   # 試験用: モデルを呼ばずに決まった答えを返す
    prompt = PLAN_PROMPT.format(n=PLAN_MAX, text=text)
    env = dict(os.environ)
    if cmd:
        argv = ["/bin/zsh", "-lc", cmd]
    else:
        if profile:
            env["CLAUDE_CONFIG_DIR"] = os.path.join(HOME, ".claude-profiles", profile)
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        argv = ["/bin/zsh", "-lc", "command claude --model claude-haiku-4-5-20251001 -p " + shlex.quote(prompt)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)   # stdin を閉じないと警告が本文に混ざる
    except subprocess.TimeoutExpired:
        return [], f"分解が {timeout} 秒で返らなかった"
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        return [], ((r.stderr or "").strip().splitlines() or ["失敗"])[-1][:160]
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return [], "分解の答えが JSON 配列でない: " + out[:120]
    try:
        rows = json.loads(m.group(0))
    except ValueError as e:
        return [], f"分解の答えを読めない: {e}"
    if not isinstance(rows, list) or not rows:
        return [], "分解の答えが空"
    tasks = []
    for i, x in enumerate(rows[:PLAN_MAX]):
        if not isinstance(x, dict):
            continue
        p = str(x.get("prompt") or "").strip()
        if not p:
            continue
        tasks.append({"title": (str(x.get("title") or "").strip() or p)[:40], "prompt": p[:4000]})
    if not tasks:
        return [], "分解の答えに仕事が無い"
    return tasks, ""


# ---------------------------------------------------------------- 予約 ----
# 「毎朝 6 時にこれを」を盤から作る。crontab や launchd は触らない(再起動で消える・TCC で読めない場所がある)。
# 代わりにアプリが開いている間に見張って走らせる。走った仕事は普通のセッションとして盤に出る。
SCHEDULE_MIN_EVERY = 15


def schedule_path():
    import aiboard_paths as ap
    return ap.data("schedule.json")


def read_schedule():
    try:
        with open(schedule_path(), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []


def _write_schedule(rows):
    p = schedule_path()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, p)


def save_job(job):
    """予約を 1 件足す/書き換える。形が違えば ValueError(黙って壊れたものを置かない)。"""
    if not isinstance(job, dict):
        raise ValueError("予約の形が不正")
    prompt = str(job.get("prompt", "")).strip()
    if not (1 <= len(prompt) <= 4000):
        raise ValueError("依頼文は 1〜4000 字")
    cwd = str(job.get("cwd", "") or HOME)
    if not cwd.startswith("/") or ".." in cwd:
        raise ValueError("場所が不正")
    ai = "Codex" if str(job.get("ai", "")) == "Codex" else "Claude"
    at, every = str(job.get("at", "") or ""), job.get("every")
    once_at = job.get("once_at")     # 1 回だけ: この時刻(epoch)を過ぎたら 1 度走って自分を止める
    if once_at not in (None, ""):
        try:
            once_at = float(once_at)
        except (TypeError, ValueError):
            raise ValueError("once_at は時刻(epoch)")
        if once_at < time.time() - 86400 or once_at > time.time() + 30 * 86400:
            raise ValueError("once_at が現実的でない(過去 1 日〜先 30 日)")
    else:
        once_at = None
    resume_sid = str(job.get("resume") or "")
    if resume_sid and not re.fullmatch(r"[0-9a-fA-F-]{16,}", resume_sid):
        raise ValueError("resume は会話の id")
    if at and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        raise ValueError("時刻は HH:MM")
    if every is not None and every != "":
        try:
            every = int(every)
        except (TypeError, ValueError):
            raise ValueError("間隔は分(数)")
        if every < SCHEDULE_MIN_EVERY:
            raise ValueError(f"間隔は {SCHEDULE_MIN_EVERY} 分以上")
    else:
        every = None
    if not at and not every and not once_at:
        raise ValueError("時刻・間隔・1 回だけ のどれかが要る")
    jid = str(job.get("id") or f"j{int(time.time() * 1000)}")
    rows = [r for r in read_schedule() if r.get("id") != jid]
    rec = {"id": jid, "key": str(job.get("key", ""))[:80], "prompt": prompt, "cwd": cwd, "ai": ai,
           "at": at, "every": every, "once_at": once_at, "resume": resume_sid,
           "enabled": bool(job.get("enabled", True)),
           "last_run": float(job.get("last_run") or 0), "created": time.time()}
    rows.append(rec)
    _write_schedule(rows[-100:])
    return rec


def delete_job(jid):
    rows = read_schedule()
    left = [r for r in rows if r.get("id") != jid]
    _write_schedule(left)
    return len(rows) - len(left)


def mark_ran(jid, when=None):
    rows = read_schedule()
    hit = 0
    for r in rows:
        if r.get("id") == jid:
            r["last_run"] = float(when or time.time())
            if r.get("once_at"):
                r["enabled"] = False      # 1 回だけの予約は走ったら自分を止める
            hit += 1
    _write_schedule(rows)
    return hit


def job_due(job, now=None, grace=3600):
    """いま走らせるべきか。

    - 間隔(every 分): 前回から every 分経っていれば走る
    - 時刻(at HH:MM): その時刻を過ぎていて、今日まだ走っていなければ走る。
      ただし grace 秒より古い時刻は走らせない(アプリを夕方に開いて、朝の予約が突然動くのを防ぐ)
    """
    if not job.get("enabled", True):
        return False
    now = now if now is not None else time.time()
    last = float(job.get("last_run") or 0)
    if job.get("once_at"):        # 1 回だけ: その時刻を過ぎていて、まだ走っていなければ
        return not last and now >= float(job["once_at"])
    if job.get("every"):
        return now - last >= float(job["every"]) * 60
    at = str(job.get("at") or "")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        return False
    lt = time.localtime(now)
    h, m = (int(x) for x in at.split(":"))
    today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if now < today:
        return False
    if now - today > grace:
        return False
    return last < today


def job_missed(job, now=None, grace=3600):
    """時刻の予約の「直近の予定時刻」(今日の分が未来なら昨日の分)を見送ったなら、その時刻。無ければ None。
    日付をまたいだ見送り(23:00 の予約を翌 0:30 に開いた)も拾う(codex 再反証 2026-09-19)。"""
    if not job.get("enabled", True) or job.get("every") or job.get("once_at"):
        return None
    now = now if now is not None else time.time()
    at = str(job.get("at") or "")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        return None
    lt = time.localtime(now)
    h, m = (int(x) for x in at.split(":"))
    occ = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if occ > now:
        yl = time.localtime(now - 86400)
        occ = time.mktime((yl.tm_year, yl.tm_mon, yl.tm_mday, h, m, 0, 0, 0, -1))
    if now - occ > grace and float(job.get("last_run") or 0) < occ and float(job.get("created") or 0) < occ:
        return occ
    return None


def job_next_at(job, now=None):
    """次に走る時刻(表示用)。止めてあれば None。"""
    if not job.get("enabled", True):
        return None
    now = now if now is not None else time.time()
    if job.get("once_at"):
        return None if job.get("last_run") else float(job["once_at"])
    if job.get("every"):
        return max(now, float(job.get("last_run") or 0) + float(job["every"]) * 60)
    at = str(job.get("at") or "")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        return None
    lt = time.localtime(now)
    h, m = (int(x) for x in at.split(":"))
    today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    return today if now < today and float(job.get("last_run") or 0) < today else today + 86400


def deleg_path(key):
    import aiboard_paths as ap
    d = ap.data("projects")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, safe_key(key) + ".delegations.json")


def read_delegations(key, limit=30):
    """その案件で「任せた仕事」の控え。新しい順。無ければ空。

    任せた後どうなったかが盤に戻ってこなかったので、依頼した事実をここに残し、
    経過(会話)と突き合わせて結果を出す。
    """
    try:
        with open(deleg_path(key), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return sorted([r for r in rows if isinstance(r, dict)], key=lambda r: r.get("at") or 0, reverse=True)[:limit]


def add_delegation(key, row, keep=100):
    if not isinstance(key, str) or not (1 <= len(key) <= 80):
        raise ValueError("案件の名前が不正")
    if not isinstance(row, dict):
        raise ValueError("控えの形が不正")
    rec = {"id": str(row.get("id") or f"d{int(time.time() * 1000)}")[:40],
           "at": time.time(),
           "text": str(row.get("text", ""))[:4000],
           "ai": str(row.get("ai", ""))[:40],
           "profile": str(row.get("profile", ""))[:40],
           "cwd": str(row.get("cwd", ""))[:400],
           "group": str(row.get("group", ""))[:40],      # 分解して同時に出した仕事は同じ束
           "title": str(row.get("title", ""))[:80]}
    if not rec["text"]:
        raise ValueError("依頼文が空")
    rows = read_delegations(key, limit=keep) + [rec]
    rows = sorted(rows, key=lambda r: r.get("at") or 0)[-keep:]
    p = deleg_path(key)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, p)
    return rec


def link_delegation(key, did, sid):
    """控え(did)に、一致で結び付いたセッション(sid)を書き込む。終わった後も同じ会話を指せるように。"""
    if not (isinstance(sid, str) and 8 <= len(sid) <= 80):
        raise ValueError("sid の形が不正")
    rows = read_delegations(key, limit=100)
    hit = 0
    for r in rows:
        if r.get("id") == did and r.get("sid") != sid:
            r["sid"] = sid
            hit += 1
    if hit:
        p = deleg_path(key)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(rows, key=lambda r: r.get("at") or 0), f, ensure_ascii=False)
        os.replace(tmp, p)
    return hit


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


@functools.lru_cache(maxsize=512)
def _cron_next_cached(expr, minute, horizon_days):
    return _cron_next(expr, minute * 60, horizon_days)


def cron_next(expr, after, horizon_days=8):
    """5 欄の cron の次の発火。答えは 1 分の間は変わらないので覚えておく(毎回作り直すと更新 1 回で 49ms)。"""
    return _cron_next_cached(expr, int(after // 60), horizon_days)


def _cron_next(expr, after, horizon_days=8):
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


_BIN = {}


def bin_path(name):
    """CLI の実体の場所を 1 回だけ引いて覚える。

    `zsh -l -c ...` は毎回ログインシェル(.zshrc 等)を読むので、`claude agents --json` が
    **340〜374ms** かかっていた(2026-09-20 実測。更新 1 回で最も重い)。場所さえ分かれば直接呼べる。
    """
    if name in _BIN:
        return _BIN[name]
    path = shutil.which(name) or ""
    if not path:
        try:
            r = subprocess.run(["/bin/zsh", "-lc", "command -v " + name], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL)
            path = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            path = ""
    _BIN[name] = path if path.startswith("/") else ""
    return _BIN[name]


_AGENTS = {"t": 0, "val": [], "wait": 5.0}
AGENTS_MIN, AGENTS_MAX = 5.0, 20.0


def official_agents(max_age=None):
    """`claude agents --json` の一覧(公式の状態源)。hook が無くても状態が分かる。

    返す項目: pid(対話セッション)・id(背景セッション)・status(busy/waiting/idle)・waitingFor・state・cwd・name。

    **呼ぶたびに Node が起動して 1 回 275ms の CPU を使う**(2026-09-20 実測)。5 秒ごとに呼ぶと
    それだけで CPU の 5% を常時使ってしまうので、**中身が前と同じなら間隔を倍にする**(5→10→20 秒で頭打ち)。
    変わったら 5 秒に戻すので、動きがある間は細かく、止まっている間は静かになる。
    代償: 静かな時に背景セッションが「判断待ち」に変わると、気づくのが最大 20 秒遅れる。
    """
    now = time.time()
    if now - _AGENTS["t"] < (_AGENTS["wait"] if max_age is None else max_age):
        return _AGENTS["val"]
    out = []
    try:
        claude = bin_path("claude")
        argv = [claude, "agents", "--json"] if claude else ["zsh", "-l", "-c", "command claude agents --json"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, cwd=HOME, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip().startswith("["):
            out = [x for x in json.loads(r.stdout) if isinstance(x, dict)]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        out = _AGENTS["val"]   # 取れなかった時は前の値(古いと分かるように t は進めない)
        _AGENTS["val"] = out
        return out
    same = [json.dumps(x, sort_keys=True) for x in out] == [json.dumps(x, sort_keys=True) for x in _AGENTS["val"]]
    _AGENTS.update(t=now, val=out, wait=min(AGENTS_MAX, _AGENTS["wait"] * 2) if same else AGENTS_MIN)
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
            "background": True, "project_hint": "", "auth_lost": None, "deleg": "", "idle": None, "trust_ask": "",
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
            "deleg": t.get("deleg", ""),      # 「任せる」で起こした端末なら、その控えの id
            "idle": t.get("idle"),            # 端末に最後に文字が出てからの秒数(記録を持たない CLI の判断に使う)
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
            # ログインが切れて止まっているか(実測で最多の止まり方。盤に出ていなかった)
            "auth_lost": (claude_auth_error(t["transcript"]) if t.get("transcript") and not (t.get("ai") or "").startswith("Codex") else None),
        })
        out[-1]["ui"] = ui_of(out[-1], t)      # 真理値表(board/decide.py)が決めた見せ方と次の一手
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
                b["ui"] = ui_of(b)
                out.append(b)
    return [apply_group(x) for x in out]


def ui_of(s, t=None):
    """セッション 1 本を真理値表の入力に直して、見せ方と次の一手を決める(board/decide.py)。"""
    import decide
    lim = s.get("limit") or {}
    stop = ""
    if s.get("auth_lost"):
        stop = s["auth_lost"].get("kind") or ""
    elif lim.get("active"):
        stop = {"5h": "five_hour", "usage": "five_hour", "weekly": "seven_day"}.get(lim.get("kind"), "five_hour")
    hook = ""
    if s.get("state") == "確認待ち":
        hook = "waiting"
    elif s.get("mark") in ("🟢", "🟩"):
        hook = "working"
    elif s.get("state") in ("返答待ち", "codex 返答待ち"):
        hook = "replied"
    return decide.decide({
        "proc": bool(s.get("pid")) or bool(s.get("background")),
        "stop": stop, "hook": hook,
        "loop": bool((s.get("loop") or {}).get("wake")),
        "trusted": (False if s.get("trust_ask") else None),
        "idle": s.get("idle"),
        "others": 0,        # 並行は snapshot 側で数える(ここでは 1 本しか見えない)
    })


def parallel_key(s):
    """そのセッションの「持ち場」。衝突しうるのは同じ持ち場のときだけ。

    以前は cwd をそのまま鍵にしていたので、`~` で起こした 14 本が全部「同じ場所・衝突注意」になっていた。
    実際には盤自身がその 14 本を 10 個の別プロジェクトと推定していた(2026-09-21 実機)。
    触っているファイルから推した持ち場(project_hint)があればそれを使い、無ければ cwd。
    ホームそのものは持ち場ではないので、推定が無ければ組にしない。
    """
    hint = (s.get("project_hint") or "").strip()
    if hint:
        # 名前で持ち場が分かった時、その名前のフォルダが実在すればフォルダを鍵にする。
        # そうしないと「~ で動いていて中身は A を触っている」組と「A で動いている」組が別々になる
        for base in (HOME, os.path.join(HOME, "Desktop")):
            p = os.path.join(base, hint)
            if os.path.isdir(p):
                return "cwd:" + p
        return "hint:" + hint
    cwd = (s.get("cwd") or "").rstrip("/")
    if not cwd or cwd == HOME.rstrip("/"):
        return ""      # ホーム直下で、何を触っているかも分からない = 持ち場が不明。衝突とは言えない
    return "cwd:" + cwd


def parallel_groups(sess):
    """同じ持ち場で 2 本以上動いている組。実測で 621 フォルダ中 206(33%)が該当し、
    「自分が 2 つ動かしていることに気づかない」が起きる。隠さずに数と顔ぶれを出す。"""
    by = {}
    for s in sess:
        if not s.get("ai") or s.get("mark") == "⚪" or s.get("background"):
            continue
        key = parallel_key(s)
        if not key:
            continue
        by.setdefault(key, []).append({"sid": s.get("sid"), "tab": s.get("tab"), "state": s.get("state"),
                                       "ai": s.get("ai"), "task": (s.get("task") or "")[:60],
                                       "where": (s.get("project_hint") or s.get("project") or "")})
    return {k: v for k, v in by.items() if len(v) > 1}


def attention(sess):
    """利用者が対応すべきもの。上から順に: ⚠確認待ち → 返答済みで長く放置 → 確認画面で停止。"""
    items = []
    for s in sess:
        why = None
        rank = None
        if s.get("auth_lost"):
            why, rank = "ログインが切れている: " + (s["auth_lost"].get("text") or "")[:60], 0
        elif s["state"] == "確認待ち":
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
    return _judge_priority(items)


_JUDGE_CACHE = {}   # (kind, key) -> (時刻, 結果)
_JUDGE_BUSY = set() # いま裏で問い合わせ中の key


def _judge_async(key, kind, options, context, ttl):
    """判定器の答えを返す。**盤の更新を待たせない**: 手元に新しい答えが無ければ裏で問い合わせ、
    今回は None(=規則のまま)を返す。次の snapshot で答えが使われる。
    (codex の反証 2026-09-19: 同期呼び出しだと、顔ぶれが変わるたびに最大 4 秒 snapshot が止まる)"""
    import threading
    import judge
    hit = _JUDGE_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    # 期限切れの答えは使わない(問い直している間は規則。codex 再反証: 古い判断を返していた)
    if key not in _JUDGE_BUSY:
        _JUDGE_BUSY.add(key)

        def run():
            try:
                _JUDGE_CACHE[key] = (time.time(), judge.decide(kind, options, context))
            finally:
                _JUDGE_BUSY.discard(key)
        threading.Thread(target=run, daemon=True).start()
    return None


def _judge_priority(items):
    """判定器が規則以外なら、上位 5 件の中から「最初に見せる 1 件」を選ばせて先頭に置く。"""
    import judge
    if len(items) < 2 or judge.config()["backend"] == "rules":
        return items
    top = items[:5]
    # 顔ぶれだけでなく状態も鍵に入れる(同じ sid でも「確認待ち→返答待ち」になれば問い直す)。経過時間は 5 分刻み
    key = ("priority", tuple((x.get("sid"), x.get("state"), int((x.get("state_for") or 0) // 300)) for x in top))
    res = _judge_async(key, "priority",
                       [{"id": x.get("sid"), "text": f'{x.get("state")} {fmt_dur(x.get("state_for") or 0)} {x.get("project") or ""} {x.get("task") or ""}'} for x in top],
                       {"count": len(items)}, 20)
    if not res:
        return items
    if res.get("id"):
        items = sorted(items, key=lambda x: 0 if x.get("sid") == res["id"] else 1)
        items[0] = {**items[0], "judged": res["by"], "judge_why": res["why"]}
    else:
        # 判定器が「どれでもない」と答えた / 失敗した: 並びは規則のまま、そうだったと分かる印を付ける
        items[0] = {**items[0], "judged": "none" if not res.get("fallback") else "fallback", "judge_why": res.get("why", "")}
    return items


def judge_clients(sess):
    """顧客の付いていないセッションに、判定器で候補を付ける(client_suggest。自動では付けない)。"""
    import judge
    if judge.config()["backend"] == "rules":
        return sess
    defs = client_defs()
    if not defs:
        return sess
    opts = [{"id": c["id"], "text": f'{c.get("label") or c["id"]} {" ".join((c.get("keywords") or [])[:6])}'} for c in defs]
    for s in sess:
        if s.get("client") or not s.get("ai") or not s.get("sid"):
            continue
        key = ("client", s["sid"])
        res = _judge_async(key, "client", opts, {"project": s.get("project_hint") or s.get("project") or "",
                                                  "cwd": os.path.basename(s.get("cwd") or ""), "task": (s.get("task") or "")[:120]}, 300)
        if res and res.get("id") and not res.get("fallback"):
            c = next((d for d in defs if d["id"] == res["id"]), None)
            if c:
                s["client_suggest"] = {"id": c["id"], "label": c.get("label") or c["id"], "by": res["by"]}
    return sess


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


_MACHINE = {"t": 0, "data": None}


def machine(procs=None, ttl=10):
    """メモリの使用率・圧縮・スワップ・内訳。**10 秒は使い回す**。

    中身は vm_stat・sysctl・memory_pressure の 4 プロセス(約 95ms)と全プロセスの分類(約 51ms)で、
    更新 1 回ぶんの 1/4 を占めていた(2026-09-20 実測)。メモリの棒は 2.5 秒ごとに描き直す必要が無い。
    """
    if procs is not None:
        return _machine(procs)          # 明示的に渡された時は、その表で作る(使い回すと別の表の答えを返してしまう)
    if _MACHINE["data"] and time.time() - _MACHINE["t"] < ttl:
        return _MACHINE["data"]
    data = _machine(None)
    _MACHINE.update(t=time.time(), data=data)
    return data


def _machine(procs=None):
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


# 遠隔の機械で動いている Claude のセッションと、止まり方(認証・上限・一時的な失敗)を読む。
# 向こうには何も入れない(python3 だけで動く小さな読み取り)。見るだけで、送ったり止めたりはしない。
REMOTE_SESS_SCRIPT = r"""
import json, os, glob, re, time
H = os.path.expanduser("~")
out = []
for f in glob.glob(os.path.join(H, ".claude", "sessions", "*.json")):
    try:
        d = json.load(open(f))
        pid = int(d.get("pid") or os.path.basename(f).split(".")[0])
        os.kill(pid, 0)
    except Exception:
        continue
    sid = d.get("sessionId") or ""
    tr = glob.glob(os.path.join(H, ".claude", "projects", "*", sid + ".jsonl")) if sid else []
    stop, last = "", ""
    if tr:
        try:
            sz = os.path.getsize(tr[0]); fh = open(tr[0], "rb"); fh.seek(max(0, sz - 200000))
            for line in fh.read().decode("utf-8", "replace").splitlines():
                if '"assistant"' not in line and '"user"' not in line:
                    continue
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                if x.get("type") == "user" and not x.get("isMeta"):
                    last = "user"
                if x.get("type") != "assistant":
                    continue
                last = "tool" if any(isinstance(b, dict) and b.get("type") == "tool_use"
                                     for b in (x.get("message", {}).get("content") or [])) else "end"
                t = "".join(b.get("text", "") for b in (x.get("message", {}).get("content") or []) if isinstance(b, dict))
                q = x.get("quotaLimits") or {}
                if x.get("isApiErrorMessage"):
                    if q.get("status") == "rejected": stop = q.get("rateLimitType") or "five_hour"
                    elif re.search(r"Invalid API key", t, re.I): stop = "apikey"
                    elif re.search(r"Not logged in|Login expired|OAuth session expired", t, re.I): stop = "login"
                    elif re.search(r"out of usage credits", t, re.I): stop = "credits"
                    elif re.search(r"went to sleep|response stopped|reach the API|overloaded|timeout", t, re.I): stop = "transient"
                elif (x.get("message", {}).get("model") or "") != "<synthetic>":
                    stop = ""
        except Exception:
            pass
    # hook が無い機械では status が無い。記録の末尾から推す(返答で終わった=こちらの番、それ以外=AI の番)
    st = d.get("status") or {"end": "idle", "tool": "busy", "user": "busy"}.get(last, "")
    out.append({"pid": pid, "sid": sid, "cwd": d.get("cwd") or "", "status": st, "status_from": "hook" if d.get("status") else "transcript",
                "updated": d.get("updatedAt") or (os.path.getmtime(tr[0]) if tr else None), "stop": stop,
                "name": (d.get("name") or "")[:60]})
import socket
print("@@rs" + json.dumps({"host": socket.gethostname(), "rows": out}) + "@@end")
"""
_RS_CACHE = {"t": 0, "data": {"ok": False, "reason": "まだ読んでいない", "rows": []}, "busy": False}


def remote_sessions(force=False, ttl=60):
    """遠隔の機械(remote_hosts)のセッション。盤を待たせないよう裏で読み、手元の最新を返す。"""
    import threading
    now = time.time()
    if not MACMINI_HOSTS:
        return {"ok": False, "reason": "未設定(~/.aiboard/config.json の remote_hosts)", "rows": []}
    if (force or now - _RS_CACHE["t"] > ttl) and not _RS_CACHE["busy"]:
        _RS_CACHE["busy"] = True

        def run():
            rows, errs, seen = [], [], set()
            try:
                for host in MACMINI_HOSTS:
                    try:
                        r = subprocess.run(["ssh"] + SSH_OPTS + [host, "python3 -"], input=REMOTE_SESS_SCRIPT,
                                           capture_output=True, text=True, timeout=40)
                    except (subprocess.TimeoutExpired, OSError) as e:
                        errs.append(f"{host}: {e}"[:120]); continue
                    m = re.search(r"@@rs(.*)@@end", r.stdout, re.S)
                    if r.returncode != 0 or not m:
                        errs.append(f"{host}: rc={r.returncode} {r.stderr.strip()[:120]}"); continue
                    got = json.loads(m.group(1))
                    if got["host"] in seen:
                        continue    # 同じ機械への別名(macmini-cf と macmini-m4nc など)は 1 回だけ数える
                    seen.add(got["host"])
                    for x in got["rows"]:
                        hook = {"waiting": "waiting", "busy": "working", "idle": "replied"}.get(x.get("status"), "")
                        x["host"], x["machine"] = host, got["host"]
                        x["ui"] = __import__("decide").decide({"proc": True, "stop": x.get("stop") or "", "hook": hook})
                        rows.append(x)
                _RS_CACHE.update(t=time.time(), data={"ok": not errs or bool(rows), "reason": " / ".join(errs), "rows": rows,
                                                      "fetched": time.time()})
            finally:
                _RS_CACHE["busy"] = False
        threading.Thread(target=run, daemon=True).start()
    return _RS_CACHE["data"]


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


def accounts_placeholder():
    """まだ CLI に聞けていない時に出す、待たない一覧。設定ファイルを読むだけ(数 ms)。

    ログイン状態は **None(確認中)**。ここで「未ログイン」と書くと嘘になる。
    """
    rows = []
    homes = [("default", HOME + "/.claude")] + [(os.path.basename(d), d) for d in sorted(glob.glob(HOME + "/.claude-profiles/*")) if os.path.isdir(d)]
    for name, base in homes:
        a = {}
        try:
            with open(os.path.join(HOME, ".claude.json") if name == "default" else os.path.join(base, ".claude.json"), encoding="utf-8") as f:
                a = json.load(f).get("oauthAccount") or {}
        except (OSError, ValueError):
            pass
        rows.append({"ai": "Claude", "profile": name, "config_dir": base, "email": a.get("emailAddress") or "",
                     "org": a.get("organizationName") or "", "plan": a.get("billingType") or "",
                     "logged_in": None, "method": "", "auth_error": "", "from_file": True, "running": 0, "limit": None})
    rows.append({"ai": "Codex", "profile": "codex", "config_dir": HOME + "/.codex", "email": "", "org": "", "plan": "",
                 "logged_in": None, "method": "", "auth_error": "", "running": 0, "limit": None})
    return rows


def login_status():
    """各アカウントのログイン状態を、それぞれの CLI 自身に聞く(推測しない)。

    1 本 4〜5 秒かかり、直列だと 20 秒を超える(実機で /api/accounts が 137 秒)。互いに独立なので同時に聞く。
    """
    import concurrent.futures as _cf
    homes = [("default", HOME + "/.claude")] + [(os.path.basename(d), d) for d in sorted(glob.glob(HOME + "/.claude-profiles/*")) if os.path.isdir(d)]
    with _cf.ThreadPoolExecutor(max_workers=max(2, len(homes) + 1)) as ex:
        futs = [ex.submit(_claude_login, name, base) for name, base in homes] + [ex.submit(_codex_login)]
        out = [f.result() for f in futs]
    return out


def _claude_login(name, base):
    """1 アカウント分。CLI 自身に聞き、答えが無ければ「分からない」(None)で返す。"""
    env = dict(os.environ)
    if name == "default":
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = base
    st = {"ai": "Claude", "profile": name, "config_dir": base, "logged_in": None, "method": "", "error": ""}
    j = {}
    try:
        # CLI の実体を直接呼ぶ。アプリから起動したサーバは PATH が細く、`command claude` が
        # 見つからないことがある。以前はその時 rc を見ずに「未ログイン」と表示していた
        # (2026-09-21 実機: CLI 直では loggedIn:true なのに盤は 4 アカウント全部「未ログイン」)
        claude = bin_path("claude")
        argv = [claude, "auth", "status", "--json"] if claude else ["/bin/zsh", "-l", "-c", "command claude auth status --json"]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        j = json.loads(r.stdout[r.stdout.find("{"):]) if "{" in r.stdout else {}
        if j:
            st["logged_in"] = bool(j.get("loggedIn"))
            st["method"] = j.get("authMethod") or ""
        else:
            # 答えが無い = 「ログインしていない」ではない。分からないまま出す(logged_in は None)
            why = (r.stderr or r.stdout).strip().splitlines()
            st["error"] = f"claude に聞けませんでした (rc={r.returncode}{': ' + why[-1][:60] if why else ''})"
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        st["error"] = f"claude に聞けませんでした ({type(e).__name__})"
    # 正はこの CLI の答え。設定ファイルは CLI が答えられなかった時だけ使う(古い値が残っていることがある)
    st.update(email=j.get("email") or "", org=j.get("orgName") or "", plan=j.get("subscriptionType") or "")
    if not st["email"]:
        try:
            with open(os.path.join(HOME, ".claude.json") if name == "default" else os.path.join(base, ".claude.json"), encoding="utf-8") as f:
                a = json.load(f).get("oauthAccount") or {}
            st.update(email=a.get("emailAddress") or "", org=a.get("organizationName") or "", plan=a.get("billingType") or "", from_file=True)
        except (OSError, ValueError):
            pass
    return st


def _codex_login():
    cx = {"ai": "Codex", "profile": "codex", "config_dir": HOME + "/.codex", "logged_in": None, "method": "", "error": "", "email": "", "org": "", "plan": ""}
    try:
        codex = bin_path("codex")
        argv = [codex, "login", "status"] if codex else ["/bin/zsh", "-l", "-c", "command codex login status"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        txt = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\\\)|\x1b\][^A-Za-z]*[A-Za-z=][^\n]*?(?=Logged|Not)", "", r.stdout + r.stderr)
        if "Logged in" in txt or "Not logged in" in txt:
            cx["logged_in"] = "Logged in" in txt
            m = re.search(r"Logged in using ([A-Za-z ]+)", txt)
            cx["method"] = m.group(1).strip() if m else ""
        else:
            cx["error"] = f"codex に聞けませんでした (rc={r.returncode})"
    except (subprocess.TimeoutExpired, OSError) as e:
        cx["error"] = f"codex に聞けませんでした ({type(e).__name__})"
    return cx


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


def inputs_fingerprint(procs=None, sess=None):
    """盤の中身を決める「入力」の指紋。変わっていなければ、作り直しても同じものが出る。

    入力はこれだけ: 端末に居るプロセスの顔ぶれ / hook が書く状態ファイル / 会話の記録 /
    端末そのものの更新時刻(記録を持たない CLI 用) / iTerm 一覧と agents を取り直した時刻 / 設定。
    どれも stat だけで見るので数 ms。これで、何も起きていない間は作り直しを丸ごと省ける。
    """
    parts = []
    procs = procs if procs is not None else cs.processes()
    parts.append(tuple(sorted((p, v["tty"]) for p, v in procs.items() if v.get("tty") != "??")))
    for pat in (os.path.join(HOME, ".claude", "sessions", "*.json"),
                os.path.join(HOME, ".claude-profiles", "*", "sessions", "*.json")):
        for f in glob.glob(pat):
            try:
                st = os.stat(f)
                parts.append((f, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    for s in (sess or []):
        for f in (s.get("transcript"), s.get("rollout")):
            if not f:
                continue
            try:
                st = os.stat(f)
                parts.append((f, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
        if s.get("tty") and not s.get("transcript"):
            try:
                parts.append((s["tty"], os.stat("/dev/" + s["tty"]).st_mtime_ns))
            except OSError:
                pass
    for f in (cs.APP_PANES, aiboard_paths.data("config.json"), aiboard_paths.data("schedule.json")):
        try:
            parts.append((f, os.stat(f).st_mtime_ns))
        except OSError:
            pass
    parts.append(("iterm", cs._LAST_ITERM.get("at", 0)))
    parts.append(("agents", _AGENTS["t"]))
    return hash(tuple(parts))


def snapshot(with_macmini=True):
    t0 = time.time()
    procs = cs.processes()
    sess = judge_clients(sessions(procs))   # 判定器が規則以外なら、顧客の候補を付ける(規則なら何もしない)
    try:
        __import__("autopilot").tick(sess)  # 表の一手のうち、システムがやってよいものだけ実行(既定は何もしない)
    except Exception as e:                  # 自動処理の失敗で盤を止めない
        __import__("autopilot").note("tick", "", f"自動処理が落ちた: {e}"[:160], done=False)
    sess = __import__("gitinfo").annotate(sess)   # git のブランチ/PR(入れてある時だけ。既定は切)
    for s in sess:
        s["parallel_key"] = parallel_key(s)     # 画面が同じ鍵で引けるように、各セッションに持たせる
    cl, pr = grouped(sess)
    snap = {
        "time": time.time(),
        "sessions": sess,
        "attention": attention(sess),
        "judge": __import__("judge").status(),
        "git": {**__import__("gitinfo").config(), "last_error": __import__("gitinfo").LAST["error"]},
        "clients": cl,
        "projects": pr,
        "machine": machine(),
        "macmini": macmini() if with_macmini else {"ok": False, "reason": "未取得"},
        "notify": {"auth": cs.app_notify_auth()},
        "parallel": parallel_groups(sess),
        "truth_table": [r[0] for r in __import__("decide").ROWS],
        "autopilot": __import__("autopilot").policy(),
        "remote_sessions": remote_sessions() if with_macmini else {"ok": False, "reason": "未取得", "rows": []},
        "sound": sound_on(),
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
        if "compact_boundary" in line or '"isCompactSummary"' in line:
            # 会話が圧縮された所。前後が別の話に見えるので、区切りとして出す(30 日で 124 回)
            try:
                d = json.loads(line)
            except ValueError:
                d = {}
            events.append({"t": d.get("timestamp", ""), "kind": "区切り", "text": "ここで会話が圧縮されました（前半は要約に置き換わっています）"})
            continue
        if '"user"' in line:      # すきまの入った JSON も拾う(型は読んでから確かめる)
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "user":
                continue
            if sidechain_ok:
                d["isSidechain"] = False   # サブエージェントの記録は全行 isSidechain=true なので外す
            if re.search(r"\[Request interrupted by user", line):
                events.append({"t": d.get("timestamp", ""), "kind": "区切り", "text": "ここであなたが止めました"})
                continue
            text = cs.prompt_text(d)
            if text:
                events.append({"t": d.get("timestamp", ""), "kind": "依頼", "text": text[:2000]})
        elif '"assistant"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" or (d.get("isSidechain") and not sidechain_ok):
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


def resume_point(path):
    """終わった・止まった会話の「どこから続けるか」。記録の末尾から、最後の依頼・その後に済んだこと・
    途中だった操作(結果が返っていない道具)・止まり方を拾う。推測で埋めない(無ければ空)。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last_ask, last_ask_t, done, pending, stop, interrupted = "", "", [], {}, None, False
    describe = _load_describe_tool()
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        typ = d.get("type")
        if typ == "user":
            if re.search(r"\[Request interrupted by user", line):
                interrupted = True
                continue
            txt = cs.prompt_text(d)
            if txt:
                last_ask, last_ask_t, done, pending, interrupted, stop = txt, d.get("timestamp", ""), [], {}, False, None
            for b in (d.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in pending:
                        done.append(pending.pop(tid))
        elif typ == "assistant":
            if d.get("isApiErrorMessage"):
                txt = "".join(b.get("text", "") for b in (d.get("message", {}).get("content") or []) if isinstance(b, dict))
                stop = txt[:120]
                continue
            for b in (d.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    pending[b.get("id")] = describe(b.get("name", ""), b.get("input") or {})
    if not last_ask:
        return None
    return {"ask": redact(last_ask[:400]), "at": last_ask_t,
            "done": [redact(x) for x in done[-5:]], "done_count": len(done),
            "pending": [redact(x) for x in list(pending.values())[-3:]],
            "interrupted": interrupted, "stop": redact(stop) if stop else ""}


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
