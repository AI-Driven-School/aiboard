"""カードに出す git のブランチと PR(どちらも入れた時だけ。既定は切)。

- ブランチ: `git` を手元で 1 回叩くだけ(外へは出ない)。フォルダごとに 30 秒は前の答えを使う
- PR: `gh pr view` を呼ぶ = GitHub へ問い合わせる。だから別のスイッチで、既定は切。
  認証は gh 自身のもの(AIBoard は鍵を持たない)。フォルダ×ブランチごとに 5 分は前の答えを使う
- どちらも失敗は「無い」として扱い、盤を止めない(理由は last_error に残す)

config.json:  {"git": {"branch": true, "pr": true}}
"""
import json
import os
import subprocess
import time

_BRANCH = {}   # cwd -> (時刻, {"branch", "root"} or None)
_PR = {}       # (root, branch) -> (時刻, {...} or None)
LAST = {"error": "", "gh": None}


def config():
    import aiboard_paths as ap
    c = (ap.config() or {}).get("git") or {}
    return {"branch": bool(c.get("branch")), "pr": bool(c.get("pr"))}


def set_config(patch):
    import aiboard_paths as ap
    p = ap.data("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    g = dict(cfg.get("git") or {})
    for k in ("branch", "pr"):
        if k in (patch or {}):
            g[k] = bool(patch[k])
    if g.get("pr") and not g.get("branch"):
        g["branch"] = True    # PR はブランチが分からないと引けない
    cfg["git"] = g
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    ap._cfg = None
    return config()


def _run(argv, cwd, timeout):
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1"))
    except (OSError, subprocess.SubprocessError) as e:
        LAST["error"] = f"{argv[0]}: {e}"[:160]
        return None
    if r.returncode != 0:
        LAST["error"] = (r.stderr.strip().splitlines() or [f"{argv[0]} exit {r.returncode}"])[-1][:160]
        return None
    return r.stdout


def branch_of(cwd, ttl=30):
    """cwd が git の作業ツリーなら {"branch", "root"}。違えば None。"""
    if not cwd or not os.path.isdir(cwd):
        return None
    hit = _BRANCH.get(cwd)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = None
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd, 3)
    if root:
        # symbolic-ref はコミットの無い作りたての枝でも名前を返す。切り離し HEAD なら短い sha
        name = _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd, 3) or _run(["git", "rev-parse", "--short", "HEAD"], cwd, 3)
        if name:
            val = {"branch": name.strip(), "root": root.strip()}
    _BRANCH[cwd] = (time.time(), val)
    return val


def pr_of(root, branch, ttl=300):
    """そのブランチの PR(gh)。無ければ None。gh が無い・未ログインなら None(理由は LAST)。"""
    key = (root, branch)
    hit = _PR.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = None
    out = _run(["gh", "pr", "view", branch, "--json", "number,state,url,title,isDraft,reviewDecision"], root, 8)
    if out:
        try:
            d = json.loads(out)
            val = {"number": d.get("number"), "state": str(d.get("state") or "").lower(), "url": d.get("url") or "",
                   "title": (d.get("title") or "")[:80], "draft": bool(d.get("isDraft")), "review": (d.get("reviewDecision") or "").lower()}
        except ValueError:
            val = None
    _PR[key] = (time.time(), val)
    return val


def annotate(sessions):
    """セッションに git を付ける(入れてある時だけ)。形: {"branch", "root", "pr": {...}|None}"""
    c = config()
    if not c["branch"]:
        return sessions
    for s in sessions:
        cwd = s.get("cwd") or ""
        if not cwd or cwd == os.path.expanduser("~"):
            continue
        b = branch_of(cwd)
        if not b:
            continue
        g = dict(b)
        g["pr"] = pr_of(b["root"], b["branch"]) if c["pr"] else None
        s["git"] = g
    return sessions
