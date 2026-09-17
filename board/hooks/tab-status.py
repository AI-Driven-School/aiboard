#!/usr/bin/env python3
"""tab-status.py — Claude Code のフックから、iTerm のタブに「どのAIが・いま何をしているか」を出す。

なぜ要るか(2026-09-17):
  iTerm のタブが増えると、どのタブがどのAI(Claude / Codex、どのモデル、どのアカウント)で、
  いま何をしているのかが分からなくなる。タブの題名は Claude が話題を書くだけで、
  「いまテストを走らせている」「承認を待っている」は見えない。

やること(フックの種類ごと):
  - タブの色: Claude=橙。顧客プロダクトの作業は顧客の色(~/.claude/tools/clients.json)。
    承認・入力の確認待ち(Notification)のときだけ赤
  - タブ右上の透かし文字(iTerm のバッジ): 「Claude opus-5（アカウント）」と「いまやっていること」
  - 状態ファイル ~/.claude/tabstate/<session_id>.json: `cs` が一覧に使う

Claude の処理は止めない(async で登録し、失敗しても exit 0)。失敗は握り潰さずログに残す。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "tabstate")
LOG = os.path.join(STATE_DIR, "_errors.log")
CLAUDE_RGB = (217, 119, 87)     # 橙(Claude)
ALERT_RGB = (220, 50, 50)       # 赤(確認待ち)


def one_line(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def account():
    d = os.environ.get("CLAUDE_CONFIG_DIR", "")
    m = re.search(r"\.claude-profiles/([^/]+)", d)
    return m.group(1) if m else "自社"


def model_from_transcript(path):
    """トランスクリプト末尾から、最後に応答したモデル名を取る。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 300_000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()):
        if '"model"' not in line:
            continue
        m = re.search(r'"model"\s*:\s*"(claude-[^"]+)"', line)
        if m:
            return m.group(1)
    return ""


def short_model(model):
    # claude-opus-5 → opus-5 / claude-fable-5-1 → fable-5.1 / claude-sonnet-5-20260101 → sonnet-5
    m = model.replace("claude-", "")
    m = re.sub(r"-\d{8}$", "", m)
    m = re.sub(r"^(\w+)-(\d+)-(\d+)$", r"\1-\2.\3", m)
    return m


def describe_tool(name, inp):
    inp = inp or {}
    base = lambda p: os.path.basename(str(p).rstrip("/")) if p else ""
    if name == "Bash":
        return "Bash: " + one_line(inp.get("description") or inp.get("command", ""), 60)
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        label = {"Edit": "編集", "Write": "書込", "Read": "読込", "NotebookEdit": "編集"}[name]
        return f"{label}: {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name in ("Grep", "Glob"):
        return f"検索: {one_line(inp.get('pattern', ''), 50)}"
    if name in ("Agent", "Task"):
        return "サブエージェント: " + one_line(inp.get("description", ""), 50)
    if name == "WebSearch":
        return "Web検索: " + one_line(inp.get("query", ""), 50)
    if name == "WebFetch":
        return "Web取得: " + one_line(inp.get("url", ""), 50)
    if name == "AskUserQuestion":
        return "質問中（あなたの返事待ち）"
    if name == "Skill":
        return "スキル: " + str(inp.get("skill", ""))
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP: {parts[1] if len(parts) > 1 else ''} {parts[-1]}"
    return name


def ai_title(path, tail_bytes=400_000):
    """Claude が自動で付けた話題名(会話記録の type=ai-title)。題名の本文に使う。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()):
        if '"ai-title"' in line:
            m = re.search(r'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
            if m:
                return json.loads(f'"{m.group(1)}"')
    return ""


def first_user_prompt(path, head_bytes=200_000):
    """会話の最初の依頼(顧客判定の手がかり)。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(head_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in chunk.splitlines():
        if '"type":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = d.get("message", {}).get("content")
        text = c if isinstance(c, str) else "".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        if text.strip() and not text.lstrip().startswith("<"):
            return text[:2000]
    return ""


def client_of(state, data):
    """顧客の判定は ~/.claude/tools/clients.py に1か所で持つ。一度当たったら保持する。"""
    if state.get("client"):
        return state["client"]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
    import clients
    texts = [data.get("prompt", ""), state.get("task", "")]
    if not state.get("client_head_checked") and data.get("transcript_path"):
        texts.append(first_user_prompt(data["transcript_path"]))
        state["client_head_checked"] = True
    return clients.classify(state.get("cwd", ""), state.get("account", "") if state.get("account") != "自社" else "",
                            texts)


def claude_tty():
    """フックの親をたどって、端末を持つ Claude のプロセスの tty を探す。
    async のフックは制御端末を持たないので /dev/tty は開けない(2026-09-17 実測)。
    Claude 本体の tty デバイス(/dev/ttysNNN)に直接書く。"""
    pid = os.getppid()
    for _ in range(6):
        out = subprocess.run(["/bin/ps", "-o", "ppid=,tty=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.split()
        if len(out) < 2:
            return ""
        ppid, tty = out[0], out[1]
        if tty not in ("??", "-"):
            return "/dev/" + tty
        pid = int(ppid)
        if pid <= 1:
            return ""
    return ""


def write_tty(seq):
    dev = claude_tty()
    if not dev:
        return ""      # 端末の無い実行(cron の claude -p 等)。バッジは出せないが異常ではない
    with open(dev, "w") as tty:
        tty.write(seq)
        tty.flush()
    return dev


def osc_badge(text):
    b64 = base64.b64encode(text.replace("\\", "/").encode()).decode()
    return f"\033]1337;SetBadgeFormat={b64}\a"


def osc_tab_color(rgb):
    r, g, b = rgb
    return (f"\033]6;1;bg;red;brightness;{r}\a"
            f"\033]6;1;bg;green;brightness;{g}\a"
            f"\033]6;1;bg;blue;brightness;{b}\a")


def save(path, state):
    # async のフックは同じセッションで同時に走る(実測 2026-09-17: 一時ファイル名の衝突で FileNotFoundError)。
    # 一時ファイルはプロセスごとに別名にして、置き換えだけを原子的に行う。
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    if not sid:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{sid}.json")
    try:
        state = json.load(open(path))
    except (OSError, ValueError):
        state = {}

    state.update(session_id=sid, ai="Claude", account=account(),
                 cwd=data.get("cwd") or state.get("cwd", ""), event=event, updated=time.time())
    model = data.get("model") or (
        model_from_transcript(data.get("transcript_path", "")) if data.get("transcript_path") else "")
    if model and not data.get("agent_id"):
        state["model"] = model

    # サブエージェントの中で動いたフックには agent_id が付く(公式 hooks ドキュメント)。
    # 親の「いまやっていること」は上書きせず、サブエージェントの欄に分けて持つ。
    subs = {k: v for k, v in state.get("subagents", {}).items() if time.time() - v.get("updated", 0) < 600}
    if data.get("agent_id"):
        if event == "PreToolUse":
            subs[data["agent_id"]] = {"type": data.get("agent_type", ""), "updated": time.time(),
                                      "doing": describe_tool(data.get("tool_name", ""), data.get("tool_input"))}
        state["subagents"] = subs
        event = "_subagent"
    else:
        state["subagents"] = subs

    if event == "UserPromptSubmit":
        state["task"] = one_line(data.get("prompt", ""), 80)
    state["client"] = client_of(state, data)
    color = tuple(state["client"]["rgb"]) if state.get("client") else CLAUDE_RGB
    if event == "SessionStart":
        state["doing"] = "起動"
        # 2日以上更新の無い状態ファイルを片付ける(無人実行の分が溜まらないように)
        for f in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, f)
            if f.endswith(".json") and time.time() - os.path.getmtime(p) > 2 * 86400:
                os.remove(p)
    elif event == "UserPromptSubmit":
        state["doing"] = "考え中"
    elif event == "PreToolUse":
        state["doing"] = describe_tool(data.get("tool_name", ""), data.get("tool_input"))
    elif event == "Notification":
        # 通知の種類は notification_type(公式 hooks ドキュメント)。
        # 「返答後しばらく入力が無い(idle_prompt)」まで確認待ちの赤にすると、自分の番と区別できない。
        ntype = data.get("notification_type", "")
        if ntype in ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            state["doing"] = "⚠ 確認待ち: " + one_line(data.get("message", ""), 50)
            color = ALERT_RGB
        elif ntype == "idle_prompt":
            state["doing"] = "✅ 返答済み（あなたの番）"
            event = "Stop"          # 題名の印も「あなたの番」に揃える
        else:
            event = "_ignore"       # 認証完了などは状態を変えない
    elif event == "Stop":
        state["doing"] = "✅ 返答済み（あなたの番）"

    save(path, state)

    # 1行目: モデル(丸の絵文字＋正式名)を先頭に。顧客は四角の絵文字で後ろに付ける(形で区別)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
    import models
    ms = models.style(state.get("model", ""))
    who = f"{ms['emoji']} {ms.get('vendor') or 'Claude'} {ms['label']}"
    if state["account"] != "自社":
        who += f"（{state['account']}）"
    head = f"{who} ｜ {state['client']['emoji']} 顧客: {state['client']['label']}" if state.get("client") else who
    badge = f"{head}\n▶ {state.get('doing', '')}"
    if subs:
        latest = max(subs.values(), key=lambda v: v.get("updated", 0))
        badge += f"\n└ サブエージェント×{len(subs)}: {latest.get('doing', '')}"

    # タブの題名: 「状態・モデル・顧客」の印 + 話題名。タブバーに並んだまま見分けられるように。
    # Claude 自身の題名更新は settings の CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 で止め、ここで組み立てる。
    if data.get("transcript_path"):
        t = ai_title(data["transcript_path"])
        if t:
            state["topic"] = t
    topic = state.get("topic") or one_line(state.get("task", ""), 30) or os.path.basename(state.get("cwd", ""))
    mark = {"Notification": "⚠", "Stop": "💬", "_ignore": state.get("mark", "⏳")}.get(event, "⏳")
    state["mark"] = mark
    if event == "_subagent":
        mark = "⏳"
    title = f"{mark}{ms['emoji']}{state['client']['emoji'] if state.get('client') else ''} {one_line(topic, 40)}"
    title_seq = f"\033]0;{title}\a"
    state["tty"] = write_tty(osc_tab_color(color) + osc_badge(badge) + title_seq)
    save(path, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # フックの失敗で Claude を止めない。ただし黙らない
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {type(e).__name__}: {e}\n")
    sys.exit(0)
