"""利用状況 — 「5 時間枠 / 7 日枠でどれだけ使ったか」を自分のログから数える。

公式の「使用率 %」はどこにも出ていない(2026-09-19 実測: `claude` に usage の口は無く、
会話ログに出るのは**上限に当たった時の** quotaLimits(status=rejected・resetsAt・rateLimitType)だけ)。
そこで:
  - 使った量は自分の会話ログから数える(リクエスト数とトークン数。これは確かな一次データ)
  - 上限(母数)は、**上限に当たった瞬間の使用量**を控えて学習する(当たるまでは「不明」と出す)
  - % は「前に上限に当たった時の使用量を 100% とした目安」であって、公式の値ではない。画面にもそう書く

重い会話ログ(最大 171MB)を全部読まないよう、窓に入りそうな末尾だけを読む。
"""
import glob
import json
import os
import time

HOME = os.path.expanduser("~")
WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
TAIL_BYTES = 6_000_000          # 1 ファイルあたり読む末尾の大きさ
_CACHE = {}                     # (dir, kind) -> (時刻, 結果)
_CAP_FILE = "usage-capacity.json"


def project_roots(config_dir):
    return [os.path.join(config_dir, "projects")]


def _iter_lines(path, tail=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail:
                f.seek(size - tail)
                f.readline()        # 途中から読み始めた 1 行目は捨てる
            for raw in f:
                yield raw
    except OSError:
        return


def _stamp(d):
    t = d.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        import datetime
        return datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def window_usage(config_dir, kind="five_hour", now=None, ttl=60):
    """その置き場(アカウント)の、いまの窓での使用量。

    返り値 {"requests", "tokens", "since", "files", "took", "rejected_at", "resets_at"}。
    rejected_at/resets_at は、その窓の中で上限に当たった記録があればその時刻。
    """
    now = now or time.time()
    key = (config_dir, kind)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    span = WINDOWS[kind]
    since = now - span
    req = tok = files = 0
    rejected_at = resets_at = None
    t0 = time.time()
    for root in project_roots(config_dir):
        for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            files += 1
            for raw in _iter_lines(path):
                if b'"timestamp"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                ts = _stamp(d)
                if ts is None or ts < since:
                    continue
                q = d.get("quotaLimits") or {}
                if q.get("status") == "rejected" and q.get("rateLimitType") == kind:
                    rejected_at, resets_at = ts, q.get("resetsAt")
                if d.get("type") != "assistant":
                    continue
                u = ((d.get("message") or {}).get("usage")) or {}
                if not u:
                    continue
                req += 1
                tok += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) \
                    + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
    out = {"requests": req, "tokens": tok, "since": since, "files": files,
           "took": round(time.time() - t0, 2), "rejected_at": rejected_at, "resets_at": resets_at}
    _CACHE[key] = (now, out)
    return out


def capacities():
    """学習した母数(上限に当たった時の使用量)。{置き場: {kind: {"tokens", "requests", "at"}}}"""
    import aiboard_paths as ap
    try:
        with open(ap.data(_CAP_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def learn(config_dir, kind, usage):
    """上限に当たっている今の使用量を母数として控える(前より大きければ更新)。"""
    import aiboard_paths as ap
    caps = capacities()
    cur = ((caps.get(config_dir) or {}).get(kind) or {})
    if usage["tokens"] <= int(cur.get("tokens") or 0):
        return cur
    rec = {"tokens": usage["tokens"], "requests": usage["requests"], "at": time.time()}
    caps.setdefault(config_dir, {})[kind] = rec
    p = ap.data(_CAP_FILE)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(caps, f, ensure_ascii=False)
    os.replace(tmp, p)
    return rec


_BG = set()


def status_async(config_dir):
    """盤を待たせずに返す: 手元に新しい答えがあればそれ、無ければ裏で数えて None。
    7 日枠は重いアカウントで 5 秒ほどかかる(実測)ので、要求の中では数えない。"""
    import threading
    ready = all((_CACHE.get((config_dir, k)) or (0,))[0] > time.time() - 60 for k in WINDOWS)
    if ready:
        return status(config_dir)
    if config_dir not in _BG:
        _BG.add(config_dir)

        def run():
            try:
                status(config_dir)
            finally:
                _BG.discard(config_dir)
        threading.Thread(target=run, daemon=True).start()
    return None


def status(config_dir, now=None):
    """アカウント 1 つぶんの利用状況。% は「前に上限に当たった時を 100% とした目安」。"""
    out = {"windows": {}}
    caps = capacities().get(config_dir) or {}
    for kind in WINDOWS:
        u = window_usage(config_dir, kind, now=now)
        cap = caps.get(kind) or {}
        if u.get("rejected_at"):
            cap = learn(config_dir, kind, u) or cap
        pct = None
        if cap.get("tokens"):
            pct = min(999, round(100 * u["tokens"] / float(cap["tokens"])))
        out["windows"][kind] = {**u, "cap_tokens": cap.get("tokens"), "cap_at": cap.get("at"),
                                "percent": pct, "estimated": True}
    return out
