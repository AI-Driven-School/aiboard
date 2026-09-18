"""install_hook.py — Claude Code の設定に AIBoard の hook を入れる / 外す / 確かめる。

Claude Code は hook が無いと「いま何をしているか」「判断待ちか」を教えてくれない。
AIBoard はこの hook で状態を読む。書き換える前に必ず控えを取り、既存の hook は消さない。

  python3 install_hook.py --check   [settings.json]   入っていれば exit 0、無ければ 1
  python3 install_hook.py --install [settings.json]   足す(既にあれば何もしない)。控え: settings.json.aiboard-backup-<時刻>
  python3 install_hook.py --uninstall [settings.json] AIBoard の hook だけ外す

settings.json の既定は $CLAUDE_CONFIG_DIR/settings.json、無ければ ~/.claude/settings.json。
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
HOOK = os.path.join(HERE, "hooks", "tab-status.py")
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop"]
MARK = "tab-status.py"   # 手がかり(最終判定は _is_ours。名前だけで決めると別人の同名ファイルを触る)


def _is_ours(cmd):
    """その命令が AIBoard の hook か。名前が同じだけの別人のファイルは触らない(2026-09-18 実測で発覚)。

    自分と認めるのは、実体がこのファイルと同じもの(symlink 経由も同じ)か、
    AIBoard の置き方 <どこか>/board/hooks/tab-status.py を指しているもの(前の版の置き場)だけ。
    """
    if MARK not in (cmd or ""):
        return False
    path = (cmd or "").split()[-1]
    try:
        if os.path.realpath(path) == os.path.realpath(HOOK):
            return True
    except OSError:
        pass
    return path.endswith(os.path.join("board", "hooks", "tab-status.py"))


def default_settings():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "settings.json")


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)   # 壊れた JSON は例外で止める(黙って上書きしない)


def ours(entry):
    return any(_is_ours(h.get("command")) for h in entry.get("hooks", []))


def check(path):
    hooks = load(path).get("hooks", {})
    return all(any(ours(e) for e in hooks.get(ev, [])) for ev in EVENTS)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = None
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777   # 秘密が入り得るので権限は元のまま(600 を 644 に緩めない)
        shutil.copy2(path, f"{path}.aiboard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode if mode is not None else 0o600)
    os.replace(tmp, path)


def install(path):
    data = load(path)
    hooks = data.setdefault("hooks", {})
    cmd = f"python3 {HOOK}"
    changed = False
    for ev in EVENTS:
        arr = hooks.setdefault(ev, [])
        mine = [e for e in arr if ours(e)]
        if mine:
            # 既にある: コマンドの場所だけ今のアプリに合わせる(アプリを動かした時のため)
            for e in mine:
                for h in e["hooks"]:
                    if _is_ours(h.get("command")) and h["command"] != cmd:
                        h["command"] = cmd; changed = True
            continue
        arr.append({"hooks": [{"type": "command", "command": cmd, "async": True, "timeout": 5}]})
        changed = True
    if changed:
        write(path, data)
    return changed


def uninstall(path):
    data = load(path)
    hooks = data.get("hooks", {})
    changed = False
    for ev in list(hooks):
        keep = [e for e in hooks[ev] if not ours(e)]
        if len(keep) != len(hooks[ev]):
            hooks[ev] = keep; changed = True
        if not hooks[ev]:
            del hooks[ev]
    if changed:
        write(path, data)
    return changed


if __name__ == "__main__":
    args = sys.argv[1:]
    op = args[0] if args else "--check"
    path = args[1] if len(args) > 1 else default_settings()
    if op == "--check":
        ok = check(path); print("installed" if ok else "missing"); sys.exit(0 if ok else 1)
    if op == "--install":
        print("installed" if install(path) else "already installed"); sys.exit(0)
    if op == "--uninstall":
        print("removed" if uninstall(path) else "not installed"); sys.exit(0)
    print(__doc__); sys.exit(2)
