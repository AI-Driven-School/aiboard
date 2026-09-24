"""止まりの台帳 — 「止まってから、次に人が書くまで」がどれだけ空いたかを数える。

**これは成果でも「負担」でもなく、1 つの観測量**。2026-09-23 に codex へ 2 回当てて削った結果、
言ってよいのはここまで:
  - 測っているのは**止まり → 次にあなたが書くまでの経過時間**。失った仕事時間ではない
    （会議・週末・外出が混ざる。寝ている間だけは夜として差し引く。だから実際より長めに出る）
  - 測っているのは「反応するまで」であって「気づくまで」ではない。記録にあるのは次の発言だけで、
    見て放置したのか見ていないのかは分からない（2026-09-24 codex の指摘）
  - **時間を足すときは重なりを 1 回だけ数える**（`wall`）。1 件ずつ足した `hours` は、同時に 2 本
    止まっていれば 2 倍になる。実測では延べ 387 時間に対し実時間 194 時間で、半分が重複だった。
    見出しに出してよいのは `wall`
  - 「盤の操作が 15 分以内にあった」は**因果の証拠にならない**（通知だけでも戻れた分が混ざる）。
    別の行として出し、上の数字には足さない
  - 比べる 2 つの窓は**同じ長さ**にする（暦の週だと今週だけ途中で、並べた時点で誤読になる）。
    それでも仕事量・障害の多さ・休みの入り方はそろっていない。減っても「盤のおかげ」とは言えない
  - 盤自身が送る定型の再開文は「人が戻った」と数えない（自分の操作で自分の数字を良くしないため）
数え方は 1 か所（このファイル）に置き、scripts/measure_recovery.py もここを使う。

定義:
  止まり   : 会話記録の isApiErrorMessage。連続する同じ種類は 1 件にまとめる（行数は「しつこさ」で件数ではない）
  復帰     : その後、同じ会話に**人が書いた**時刻（道具の結果・中断の印は数えない）
  経過時間 : 止まり → 復帰。夜(既定 0-8 時)は差し引く。戻らなかったものは時間に入れない（開いた区間を足さない）
  対話/無人: 記録の entrypoint。sdk-cli(claude -p / SDK) は「戻る人が居ない」ので**合算しない**
"""
import glob
import json
import os
import re
import time
from datetime import datetime

HOME = os.path.expanduser("~")
LONG = 1800          # 30 分以上置かれたものを「放置」として数える
QUICK = 600          # 10 分以内に戻れたもの
AWAKE = (8, 24)      # 起きている時間帯（ここだけを経過時間に数える）
KINDS = ("auth", "limit", "credits", "transient")
CACHE = "recovery_cache.json"
# 盤自身が送る定型の文。これを「人が戻った」と数えると、自分の操作で自分の数字を良くしてしまう
# （2026-09-23 codex の指摘: 自動投稿による復帰の誤検出）
CANNED = re.compile(r"^(前回はここで止まりました|上限が解けたので続けてください)")


def _ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def kind_of(text, quota):
    if (quota or {}).get("status") == "rejected":
        return "limit"
    if re.search(r"Invalid API key", text, re.I):
        return "auth"
    if re.search(r"Not logged in|Login expired|OAuth (session )?expired|Please run /login", text, re.I):
        return "auth"
    if re.search(r"out of usage credits", text, re.I):
        return "credits"
    if re.search(r"usage limit|rate limit", text, re.I):
        return "limit"
    if re.search(r"went to sleep|response stopped|reach the API|overloaded|timeout|Connection error", text, re.I):
        return "transient"
    return ""


def awake_seconds(a, b, start=AWAKE[0], end=AWAKE[1]):
    """a→b のうち、起きている時間帯に入る秒数。夜通し止まっていた分を「放置」に数えないため。"""
    total, t = 0.0, a
    while t < b:
        lt = time.localtime(t)
        day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        total += max(0.0, min(b, day + end * 3600) - max(t, day + start * 3600))
        t = day + 86400
    return total


def entrypoint_of(path, head=40):
    """対話(cli)か無人(sdk-cli)か。分からなければ "不明"。"""
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if '"entrypoint"' in line:
                    try:
                        return json.loads(line).get("entrypoint") or "不明"
                    except ValueError:
                        return "不明"
                if i > head:
                    break
    except OSError:
        pass
    return "不明"


def stops_in(path):
    """1 つの会話から止まりの出来事を拾う。返り値: [{kind, at, back_at}]"""
    events, open_ev, last_kind = [], None, ""
    try:
        fh = open(path, errors="replace")
    except OSError:
        return events
    with fh:
        for line in fh:
            flat = line.replace(" ", "")
            if '"isApiErrorMessage":true' not in flat and '"type":"user"' not in flat:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            at = _ts(d.get("timestamp"))
            if d.get("type") == "user":
                if d.get("isMeta") or "[Request interrupted" in line:
                    continue
                c = d.get("message", {}).get("content")
                txt_u = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                human = bool(txt_u.strip()) and not CANNED.match(txt_u.strip())
                if human and open_ev is not None:
                    open_ev["back_at"] = at
                    events.append(open_ev)
                    open_ev, last_kind = None, ""
                continue
            if not d.get("isApiErrorMessage"):
                continue
            txt = "".join(b.get("text", "") for b in (d.get("message", {}).get("content") or []) if isinstance(b, dict))
            k = kind_of(txt, d.get("quotaLimits"))
            if not k:
                continue
            if open_ev is not None and k == last_kind:
                continue                      # 同じ止まりの繰り返し
            if open_ev is not None:
                events.append(open_ev)
            open_ev, last_kind = {"kind": k, "at": at, "back_at": None}, k
    if open_ev is not None:
        events.append(open_ev)
    return events


def transcripts(days):
    cut = time.time() - days * 86400
    out = []
    for root in [os.path.join(HOME, ".claude", "projects")] + sorted(glob.glob(os.path.join(HOME, ".claude-profiles", "*", "projects"))):
        for p in glob.glob(os.path.join(root, "*", "*.jsonl")):
            try:
                if os.path.getmtime(p) >= cut:
                    out.append(p)
            except OSError:
                pass
    return out


def board_action_times():
    """盤を経由した操作の時刻。**上の数字には足さない**（因果の証拠ではないので別の行で出す）。"""
    import aiboard_paths as ap
    out = []
    for name in ("send.log", "stop.log"):
        try:
            for line in open(ap.data(name), errors="replace"):
                if line.strip().startswith("{"):
                    try:
                        out.append(time.mktime(time.strptime(json.loads(line)["t"], "%Y-%m-%d %H:%M:%S")))
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
    try:
        for line in open(ap.data("actions.jsonl"), errors="replace"):
            try:
                out.append(float(json.loads(line)["t"]))
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return sorted(out)


def _cache_path():
    import aiboard_paths as ap
    return ap.data(CACHE)


def scan(days=14):
    """止まりの出来事を集める。**変わっていないファイルは読み直さない**(7 日で 2,900 本・22 秒かかるため)。"""
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    fresh, events = {}, []
    for p in transcripts(days):
        try:
            st = os.stat(p)
        except OSError:
            continue
        key = f"v2:{int(st.st_mtime)}:{st.st_size}"   # 数え方を変えたら v を上げる(古い結果を使い回さない)
        hit = cache.get(p)
        if hit and hit.get("key") == key:
            fresh[p] = hit
        else:
            got = stops_in(p)
            fresh[p] = {"key": key, "ep": entrypoint_of(p) if got else "", "events": got}
        events += [dict(e, ep=fresh[p]["ep"], file=p) for e in fresh[p]["events"]]
    tmp = _cache_path() + f".{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fresh, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass
    return events


def wall_hours(spans):
    """重なりを 1 回だけ数えた実時間（起きている時間だけ）。

    1 件ずつ足した「延べ」は、**同時に 2 本止まっていれば 2 倍になる**。人が失った時間には読めない。
    2026-09-23 実測: 30 分以上の止まり 110 件は、延べ 387 時間だが実時間では 194 時間（重複 50%）。
    110 件が 30 区間に併合される。codex の指摘（2026-09-24）で判明し、公開していた数字を直した。
    """
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return sum(awake_seconds(a, b) for a, b in merged) / 3600


def summarize(events, since, until, acts=()):
    """ある期間の 1 行分。対話だけを数え、無人は件数のみ添える。

    時間は 2 つ出す。`hours` は 1 件ずつ足した**延べ**、`wall` は重なりを 1 回だけ数えた**実時間**。
    見出しに使ってよいのは `wall` の方（`hours` は「同時に何本止まっていたか」が混ざる）。
    """
    import bisect
    inter = [e for e in events if e.get("ep") == "cli" and e.get("at") and since <= e["at"] < until]
    unattended = [e for e in events if e.get("ep") == "sdk-cli" and e.get("at") and since <= e["at"] < until]
    longs = [e for e in inter if e.get("back_at") and e["back_at"] - e["at"] >= LONG]
    hours = sum(awake_seconds(e["at"], e["back_at"]) for e in longs) / 3600
    never = [e for e in inter if not e.get("back_at")]
    quick = [e for e in inter if e.get("back_at") and e["back_at"] - e["at"] <= QUICK]
    touched = 0
    for e in inter:
        i = bisect.bisect_left(acts, e["at"])
        if i < len(acts) and acts[i] - e["at"] <= 900:
            touched += 1
    by_kind = {k: sum(1 for e in longs if e["kind"] == k) for k in KINDS}
    return {"since": since, "until": until, "stops": len(inter), "long": len(longs),
            "hours": round(hours, 1), "wall": round(wall_hours((e["at"], e["back_at"]) for e in longs), 1),
            "never": len(never), "quick": len(quick),
            "board_touched": touched, "unattended": len(unattended),
            "long_by_kind": {k: v for k, v in by_kind.items() if v}}


WINDOW = 7 * 86400   # 比べる窓。暦の週にすると「今週」だけ途中（3 日）で、先週と並べた時点で誤読になる
                     # （2026-09-23 codex の指摘）。直近 7 日と、その前の 7 日を比べる


_LAST = {"t": 0, "data": None, "busy": False}
HISTORY = "ledger_history.jsonl"   # 1 日 1 行。年に 100KB ほど


def remember(data, today=None):
    """その日の台帳を 1 行だけ残す。**元の会話記録は消えるから。**

    Claude Code は `cleanupPeriodDays`（既定 30 日）で会話記録を消す
    （2026-09-23 実測: 手元の最古 mtime はちょうど 30 日前だった）。
    つまり今日測った数字は、30 日後には誰も検算できない。実際 09-20 に公開した
    「認証 989 件」は、同じ窓を測り直しても 899 件にしかならず、差の 90 件は追えなかった。
    保存期間を伸ばす手は使えない（30 日で 10GB・空きは 4.9GiB しかない）。
    だから**結果だけ**を残す。1 日 1 行、同じ日には上書きしない（先に書いた方を正とする）。
    """
    import aiboard_paths as ap
    day = today or time.strftime("%Y-%m-%d")
    path = ap.data(HISTORY)
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if f'"day": "{day}"' in line or f'"day":"{day}"' in line:
                    return False              # その日はもう記録済み
    except OSError:
        pass
    row = {"day": day, "at": round(time.time()), "week": data.get("week"), "prev": data.get("prev"),
           "long_minutes": LONG // 60, "awake": list(AWAKE), "window_days": WINDOW // 86400}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def history(limit=400):
    """残してある日次の記録を古い順に返す。無ければ空。"""
    import aiboard_paths as ap
    out = []
    try:
        for line in open(ap.data(HISTORY), errors="replace"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out[-limit:]


def ledger(force=False, ttl=900):
    """今週と先週の 1 行ずつ。**待たせない**: 手元に新しいものが無ければ裏で作り、いまある物を返す。"""
    import threading
    now = time.time()
    if os.environ.get("OVERVIEW_NO_INDEX"):
        # 試験や検証用のサーバでは数えない。初回は数千ファイルを読む(36 秒)ので、
        # 試験のたびに走ると機械ごと重くなる(2026-09-23 実測: 走行中の負荷 128)
        return {"ok": True, "checking": False, "week": None, "prev": None, "skipped": "OVERVIEW_NO_INDEX"}
    if _LAST["data"] and not force and now - _LAST["t"] < ttl:
        return _LAST["data"]
    if _LAST["busy"]:
        return _LAST["data"] or {"ok": True, "checking": True, "week": None, "prev": None}

    def build():
        try:
            events = scan(days=21)
            acts = board_action_times()
            t1 = time.time()
            data = {"ok": True, "checking": False, "built": t1,
                    "week": summarize(events, t1 - WINDOW, t1 + 1, acts),
                    "prev": summarize(events, t1 - 2 * WINDOW, t1 - WINDOW, acts),
                    "long_minutes": LONG // 60, "awake": list(AWAKE), "window_days": WINDOW // 86400}
            remember(data)          # 会話記録が消える前に、その日の結果だけ残す
            _LAST.update(t=time.time(), data=data)
        except Exception as e:      # 台帳の失敗で盤を止めない
            _LAST.update(t=time.time(), data={"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]})
        finally:
            _LAST["busy"] = False

    _LAST["busy"] = True
    if force:
        build()
        return _LAST["data"]
    threading.Thread(target=build, daemon=True).start()
    return _LAST["data"] or {"ok": True, "checking": True, "week": None, "prev": None}
