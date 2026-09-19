"""判定器 — 「選ぶだけ」の判断を差し替えられる口。

盤の中には「候補から 1 つ選ぶ」判断がいくつかある:
  priority: 判断待ちのうち、最初に見せる 1 件
  client:   顧客の付いていないセッションに、どの顧客が近いか
  assignee: 任せる先(Claude のどのアカウント / Codex)

既定は規則(いままでどおり・外へ何も出さない)。設定で手元のモデル(LM Studio / Ollama の
OpenAI 互換口)や外部の決定モデル(OpenRouter 経由の Jev 等)に替えられる。

約束:
  - 外部は既定で切。入れると prove-local-only.sh が必ずその事実を書く
  - 外へ渡す前に redact() で鍵の形を伏せ、渡すのは題名・依頼の先頭・フォルダ名だけ(会話の全文は渡さない)
  - 鍵は AIBoard が持たない。環境変数 AIBOARD_JUDGE_KEY を読むだけ(設定ファイルには書かない)
  - 返事が壊れている・遅い・無い時は規則に戻り、理由を残す(黙って空にしない)

2026-09-19。設計の背景は docs/R6_competitors_2026-09-18.md「追記2」。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
BACKENDS = ("rules", "local", "external")
DEFAULTS = {
    "backend": "rules",
    "local_url": "http://127.0.0.1:1234/v1/chat/completions",   # LM Studio の既定
    "local_model": "",
    "external_url": "https://openrouter.ai/api/v1/chat/completions",
    "external_model": "typesafe/jev-1.13",
    "timeout": 4.0,
}
KEY_ENV = "AIBOARD_JUDGE_KEY"

# 直近の呼び出しの記録(設定画面に出す。何を選んだかでなく、動いているか・失敗の理由)
LAST = {"at": 0, "ms": 0, "backend": "rules", "ok": True, "why": "", "calls": 0, "fallbacks": 0}


def config():
    import aiboard_paths as ap
    c = dict(DEFAULTS)
    c.update({k: v for k, v in ((ap.config() or {}).get("judge") or {}).items() if k in DEFAULTS})
    if c["backend"] not in BACKENDS:
        c["backend"] = "rules"
    return c


def set_config(patch):
    """設定を書く。鍵は受け取らない(環境変数から読む)。"""
    import aiboard_paths as ap
    p = ap.data("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    j = dict(cfg.get("judge") or {})
    for k, v in (patch or {}).items():
        if k not in DEFAULTS or k == "timeout":
            continue
        if k == "backend" and v not in BACKENDS:
            raise ValueError("backend は rules / local / external")
        if k.endswith("_url") and v and not re.match(r"^https?://", str(v)):
            raise ValueError("URL は http(s):// で始める")
        if k == "local_url" and v and not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", str(v)):
            raise ValueError("手元モデルの URL は 127.0.0.1 / localhost だけ")
        j[k] = str(v)[:300]
    cfg["judge"] = j
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    ap._cfg = None
    return config()


def status():
    c = config()
    return {**{k: v for k, v in c.items() if k != "timeout"},
            "has_key": bool(os.environ.get(KEY_ENV)), "key_env": KEY_ENV, "last": dict(LAST)}


# ---------------------------------------------------------------- 規則 ----
def _rules(kind, options, context):
    """いままでどおりの決め方。候補は呼ぶ側が既に並べてあるので、先頭を返す。"""
    return (options[0]["id"] if options else None), "規則(先頭)"


# ---------------------------------------------------------------- モデル ----
def _safe(text, n=160):
    from overview import redact
    return redact(str(text or ""))[:n]


def _prompt(kind, options, context):
    ask = {
        "priority": "次のうち、人が最初に対応すべきものを 1 つ選んでください(止まっている・古い・影響が大きいものを優先)。",
        "client": "このセッションはどの顧客の仕事か、候補から 1 つ選んでください。どれでもなければ none。",
        "assignee": "この仕事を任せるのに向いている先を 1 つ選んでください。",
    }.get(kind, "候補から 1 つ選んでください。")
    lines = [ask, "", "状況: " + _safe(json.dumps(context, ensure_ascii=False), 400), "", "候補:"]
    for o in options:
        lines.append(f'- id={o["id"]}: {_safe(o.get("text", ""), 120)}')
    lines += ["", '答えは JSON だけ: {"id": "<候補の id または none>"}']
    return "\n".join(lines)


def _call(url, model, key, prompt, timeout):
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 40}
    if model:
        body["model"] = model
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", errors="replace"))
    txt = ""
    try:
        txt = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        txt = json.dumps(d)[:200]
    m = re.search(r'"id"\s*:\s*"([^"]*)"', txt)
    return (m.group(1) if m else None), txt[:120]


def _model(kind, options, context, c):
    ext = c["backend"] == "external"
    url = c["external_url"] if ext else c["local_url"]
    model = c["external_model"] if ext else c["local_model"]
    key = os.environ.get(KEY_ENV, "") if ext else ""
    if ext and not key:
        raise RuntimeError(f"外部の鍵が無い({KEY_ENV} を環境変数で)")
    choice, raw = _call(url, model, key, _prompt(kind, options, context), float(c["timeout"]))
    ids = {o["id"] for o in options}
    if choice == "none":
        return None, "モデル: none"
    if choice not in ids:
        raise RuntimeError("モデルの答えが候補に無い: " + raw)
    return choice, "モデル: " + ("外部 " + model if ext else "手元")


# ---------------------------------------------------------------- 入口 ----
def decide(kind, options, context=None):
    """候補(id, text)から 1 つ選ぶ。返り値 {"id", "by", "why", "fallback"}。

    規則なら即答。モデルは失敗・遅延・不正な答えのとき規則へ戻す(fallback=True・理由つき)。
    """
    options = [o for o in (options or []) if isinstance(o, dict) and o.get("id")]
    c = config()
    t0 = time.time()
    LAST["calls"] += 1
    if c["backend"] == "rules" or not options:
        cid, why = _rules(kind, options, context or {})
        LAST.update(at=t0, ms=0, backend="rules", ok=True, why="")
        return {"id": cid, "by": "rules", "why": why, "fallback": False}
    try:
        cid, why = _model(kind, options, context or {}, c)
        LAST.update(at=t0, ms=int((time.time() - t0) * 1000), backend=c["backend"], ok=True, why="")
        return {"id": cid, "by": c["backend"], "why": why, "fallback": False}
    except (urllib.error.URLError, RuntimeError, ValueError, OSError, TimeoutError) as e:
        LAST.update(at=t0, ms=int((time.time() - t0) * 1000), backend=c["backend"], ok=False, why=str(e)[:160])
        LAST["fallbacks"] += 1
        cid, why = _rules(kind, options, context or {})
        return {"id": cid, "by": "rules", "why": f"{why}(モデル失敗→規則: {str(e)[:80]})", "fallback": True}
