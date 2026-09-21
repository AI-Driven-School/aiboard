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


def recent_exchange(path):
    """要約の材料: 直近の本人の依頼(system 注入を除く)と、AIの最後の返答。
    末尾が画像やツール出力の巨大な行で埋まっていると 600KB では1件も拾えない(実測)ので、段階的に遡る。"""
    for tail in (600_000, 6_000_000):
        prompts, reply = _recent_exchange(path, tail)
        if prompts:
            break
    return prompts, reply


def _recent_exchange(path, tail_bytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return [], ""
    prompts, reply = [], ""
    for line in chunk.splitlines():
        if '"type":"user"' not in line and '"type":"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = d.get("message", {}).get("content")
        text = c if isinstance(c, str) else "".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        text = text.strip()
        if not text:
            continue
        if d.get("type") == "user":
            if not text.startswith("<") and not text.startswith("Continue from where you left off"):
                prompts.append(one_line(text, 200))
        else:
            reply = text
    return prompts[-8:], one_line(reply, 1500)


NOW_REFRESH_SEC = 600


def summarize_now(path):
    """「いま何をしているか」を Haiku で1行にする(2026-09-21)。
    話題名(ai-title)は会話全体の題で、付かないセッションもある。その場合の代わりに
    最後の依頼を出すと「はい」「<task-notification>」が題名になっていた(17タブ中6)。"""
    prompts, reply = recent_exchange(path)
    if not prompts and not reply:
        return ""
    title = ai_title(path, tail_bytes=6_000_000)
    body = (f"[会話の題名] {title}\n" if title else "") + "\n".join(f"[依頼] {p}" for p in prompts) + (
        f"\n[AIの最後の返答] {reply}" if reply else "")
    ask = ("次はAI作業セッションの最近のやり取り。このセッションがいま取り組んでいる作業の中身を、"
           "日本語20字以内の名詞句1行だけで答えよ。「指示待ち」「確認待ち」のような状態ではなく、何の作業かを書く。"
           "前置き・引用符・句点なし。\n" + body)
    env = dict(os.environ, TAB_STATUS_CHILD="1")    # 子の claude でこのフックが再帰しないように
    # stdin は必ず DEVNULL(開いたパイプだと警告文が本文に混ざる)
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                        "--strict-mcp-config", "--setting-sources", "", ask],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, env=env)
    out = r.stdout.strip().splitlines()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"summarize_now rc={r.returncode} {r.stderr.strip()[:200]}")
    return one_line(out[-1].strip("「」\"'。 "), 30)


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
    if os.environ.get("TAB_STATUS_CHILD"):
        return
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

    if event == "UserPromptSubmit" and not data.get("prompt", "").lstrip().startswith("<"):
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
    # 題名の本文: いまやっていること(Haiku の要約) > 話題名(ai-title) > 最後の依頼 > フォルダ名
    if data.get("transcript_path"):
        t = ai_title(data["transcript_path"])
        if t:
            state["ai_title"] = t
    mark = {"Notification": "⚠", "Stop": "💬", "_ignore": state.get("mark", "⏳")}.get(event, "⏳")
    state["mark"] = mark
    if event == "_subagent":
        mark = "⏳"

    def render():
        state["topic"] = (state.get("now") or state.get("ai_title") or one_line(state.get("task", ""), 30)
                          or os.path.basename(state.get("cwd", "")))
        title = f"{mark}{ms['emoji']}{state['client']['emoji'] if state.get('client') else ''} {one_line(state['topic'], 40)}"
        state["tty"] = write_tty(osc_tab_color(color) + osc_badge(badge) + f"\033]0;{title}\a")
        save(path, state)

    render()

    # 返答の区切り(Stop)と起動・再開(SessionStart)で要約し直す。10分に1回まで(先に時刻を書いて同時実行を防ぐ)
    if (event in ("Stop", "SessionStart") and data.get("transcript_path")
            and time.time() - state.get("now_at", 0) > NOW_REFRESH_SEC):
        state["now_at"] = time.time()
        save(path, state)
        now = summarize_now(data["transcript_path"])
        if now:
            try:   # 要約の7秒の間に他のフックが書いた状態(mark・doing)を消さない
                latest = json.load(open(path))
            except (OSError, ValueError):
                latest = {}
            if latest.get("mark") and latest.get("mark") != state.get("mark"):
                latest["now"] = now     # 次の作業がもう始まっている。題名は次のフックに任せ、要約だけ残す
                save(path, latest)
                return
            state["now"] = now
            render()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # フックの失敗で Claude を止めない。ただし黙らない
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {type(e).__name__}: {e}\n")
    sys.exit(0)
