"""鍵の棚 — どの鍵がどこにあり、生きているかを 1 画面に。**値は AIBoard を通さない**。

毎回「あの鍵どこだっけ」「入れ直しが面倒」を減らすための台帳。持つのは在処だけ:
  - keychain: 項目名(サービス名)。存在の確認は `security find-generic-password -s <名前>`(値は読まない)
  - env:      環境変数の名前。あるかどうかだけ見る
  - file:     ファイルのパス。あるかどうかと権限だけ見る
  - cli:      その CLI 自身に聞く(claude auth status / gh auth status / codex login status)

「入れる」「端末に出す」は**端末でコマンドを走らせる**形にする。値は人が端末に打ち、AIBoard は見ない:
  入れる    : security add-generic-password -U -s <名前> -a $USER -w
  端末に出す: export NAME=$(security find-generic-password -s <名前> -w)
"""
import json
import os
import re
import subprocess
import time

HOME = os.path.expanduser("~")
FILE = "keys.json"
NAME_RE = re.compile(r"^aiboard-[A-Za-z0-9_.-]{1,40}$")     # 鍵束に置く名前はこの形だけ(他人の項目を触らない)

DEFAULTS = [
    {"id": "claude", "label": "Claude Code", "where": {"kind": "cli", "name": "claude"},
     "doc": "https://claude.com/product/claude-code", "note": "ログインは claude auth login"},
    {"id": "codex", "label": "Codex", "where": {"kind": "cli", "name": "codex"},
     "doc": "https://developers.openai.com/codex/cli", "note": "ログインは codex login"},
    {"id": "gh", "label": "GitHub (gh)", "where": {"kind": "cli", "name": "gh"},
     "doc": "https://cli.github.com/", "note": "PR バッジと Releases に使う"},
    {"id": "judge", "label": "判定器の鍵 (OpenRouter 等)", "where": {"kind": "keychain", "name": "aiboard-judge"},
     "env": "AIBOARD_JUDGE_KEY", "doc": "https://openrouter.ai/keys", "note": "外部の決定モデルを使う時だけ"},
    {"id": "asc", "label": "App Store Connect の鍵 (公証)", "where": {"kind": "file", "name": "~/.appstoreconnect/private_keys"},
     "doc": "https://appstoreconnect.apple.com/access/integrations/api", "note": "リリースの公証に使う"},
]


def path():
    import aiboard_paths as ap
    return ap.data(FILE)


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        rows = []
    known = {r.get("id"): r for r in rows if isinstance(r, dict) and r.get("id")}
    out = []
    for d in DEFAULTS:
        out.append({**d, **known.pop(d["id"], {})})
    out += [r for r in known.values()]
    return out


def save(rows):
    """台帳を書く。**値は受け取らない**(value/secret/token のような欄が来たら捨てる)。"""
    clean = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        w = r.get("where") or {}
        kind = w.get("kind") if w.get("kind") in ("keychain", "env", "file", "cli") else "env"
        name = str(w.get("name") or "")[:120]
        if kind == "keychain" and not NAME_RE.match(name):
            raise ValueError("鍵束の名前は aiboard- で始めてください")
        clean.append({"id": str(r["id"])[:40], "label": str(r.get("label") or r["id"])[:60],
                      "where": {"kind": kind, "name": name}, "env": str(r.get("env") or "")[:60],
                      "doc": str(r.get("doc") or "")[:300], "note": str(r.get("note") or "")[:200]})
    p = path()
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return load()


def _cli_state(name, logins):
    for a in (logins or []):
        if name == "claude" and a.get("ai") == "Claude" and a.get("profile") == "default":
            return ("ok", a.get("email") or "ログイン中") if a.get("logged_in") else ("ng", a.get("error") or "未ログイン")
        if name == "codex" and a.get("ai") == "Codex":
            return ("ok", a.get("method") or "ログイン中") if a.get("logged_in") else ("ng", a.get("error") or "未ログイン")
    if name == "gh":
        try:
            r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=8,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return "unknown", str(e)[:80]
        line = (r.stdout + r.stderr).strip().splitlines()
        return ("ok", next((l.strip() for l in line if "account" in l.lower()), "ログイン中")[:80]) if r.returncode == 0 \
            else ("ng", (line[-1].strip() if line else "未ログイン")[:80])
    return "unknown", ""


def state(row, logins=None):
    """その鍵が「ある/ない」。**値は読まない**(keychain は存在確認のコマンドだけ)。"""
    w = row.get("where") or {}
    kind, name = w.get("kind"), w.get("name") or ""
    if kind == "env":
        return ("ok", "環境変数にある") if os.environ.get(name) else ("ng", "環境変数に無い")
    if kind == "file":
        p = os.path.expanduser(name)
        if not os.path.exists(p):
            return "ng", "ファイルが無い"
        if os.path.isdir(p):
            n = len([x for x in os.listdir(p) if not x.startswith(".")])
            return ("ok", f"{n} 個") if n else ("ng", "空")
        return "ok", f"{os.stat(p).st_size}B mode {oct(os.stat(p).st_mode & 0o777)[2:]}"
    if kind == "keychain":
        if not NAME_RE.match(name):
            return "unknown", "名前の形が違う"
        try:                    # -w を付けない = 値は取り出さない(存在の確認だけ)
            r = subprocess.run(["security", "find-generic-password", "-s", name], capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError) as e:
            return "unknown", str(e)[:80]
        return ("ok", "鍵束にある") if r.returncode == 0 else ("ng", "鍵束に無い")
    if kind == "cli":
        return _cli_state(name, logins)
    return "unknown", ""


def commands(row):
    """画面のボタンが端末で走らせるコマンド。値は端末で人が入れる(AIBoard は見ない)。"""
    w = row.get("where") or {}
    kind, name, env = w.get("kind"), w.get("name") or "", row.get("env") or ""
    out = {}
    if kind == "keychain" and NAME_RE.match(name):
        out["put"] = f"security add-generic-password -U -s {name} -a $USER -w"
        if env:
            out["export"] = f"export {env}=$(security find-generic-password -s {name} -w)"
    if kind == "cli" and name in ("claude", "codex", "gh"):
        out["login"] = {"claude": "env -u CLAUDE_CONFIG_DIR command claude auth login",
                        "codex": "command codex login", "gh": "command gh auth login"}[name]
    return out


def status(logins=None):
    rows = []
    for r in load():
        st, why = state(r, logins)
        rows.append({**r, "state": st, "why": why, "commands": commands(r), "checked": time.time()})
    return rows
