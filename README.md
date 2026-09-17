# AIBoard

**The whiteboard for your coding agents.** Every Claude Code and Codex session on one canvas, grouped by client and project, with real terminals inside. When an agent needs you, its card lights up — and your Dock tells you how many are waiting.

Native macOS · open source (MIT) · local only, no telemetry

![AIBoard: running sessions grouped by client; yellow cards are waiting for you](site/img/board-overview-crop.png)

30-second demo: [`site/demo.mp4`](site/demo.mp4) (recorded by `scripts/demo/record.py`)

> Working name. Screenshots use demo mode (`?demo=1`): client names, prompts and folders are fake.

## Install

```sh
git clone https://github.com/REPO_OWNER/aiboard && cd aiboard
./make_app.sh          # builds and installs /Applications/AIBoard.app
```

Requirements: macOS 13+, Xcode command line tools (Swift 5.9+), Python 3. A signed build and `brew install --cask` come with the first release.

## What it does

| | |
|---|---|
| **See** | Each running Claude Code / Codex session is a card. Squares are clients, dots are models. *Now* shows only what is running; *History* adds the last 30 days. |
| **Notice** | Red = stuck on a permission or question. Yellow = replied, your turn. Green = working. Your Dock badge counts them and macOS notifies you. |
| **Act** | Click a card: the conversation opens as a timeline with one input line. When the agent is waiting, Yes / No buttons appear. Double-click, or *Open in terminal*, to work in the real terminal (libghostty) on the right. |
| **Start** | ⌘T Claude · ⌥⌘T Codex · ⇧⌘W Claude in a new git worktree · ⇧⌘R restore last terminals |
| **Loops & tools** | A session waiting on `/loop` shows *Looping · next 02:12* and is not counted as your turn. Scheduled prompts (CronCreate) show their next run. Each card counts the skills and MCP servers it used; Settings lists your skills and each MCP server's connection state (needs auth / failed). |
| **Limits & accounts** | When a session hits a usage limit, its card turns purple with the reset time, and you can continue the same conversation on another signed-in account. |
| **Memory** | The memory button lists sessions largest first. *Interrupt* sends Esc; *Quit* (click twice) ends the agent — the terminal stays and History can resume it. |

AIBoard is the terminal: it opens a shell on the right at launch, and ⌘T / ⌥⌘T start Claude or Codex there. Sessions you already have running elsewhere still show up on the board (found by tty, iTerm today), and *Open in terminal* brings them over: the agent in the other terminal is quit and the same conversation resumes in AIBoard, with a confirmation — and for a session mid-task, an interrupt first.

### Controls

Two-finger scroll pans · pinch or ⌘-scroll zooms · Space-drag pans · ⇧1 fit all · ⇧2 zoom to selection · 0 = 100% · `? Tour` replays the 5-step guide.

## How it works

```
AIBoard.app (Swift/AppKit)
 ├─ board: WKWebView → board server on 127.0.0.1:8791 (Python, bundled)
 │    reads ~/.claude (Claude Code session files + an optional hook)
 │    reads ~/.codex  (Codex's own state DB and rollout files)
 │    index: ~/.aiboard/index.db (SQLite, last 30 days)
 └─ terminals: libghostty surfaces, one per pane
```

- **State detection.** Claude Code: a hook writes the current activity per session (`board/hooks/tab-status.py`). Codex: the turn boundaries in its rollout file (`task_started` / `task_complete` / error).
- **Your data.** Everything the app writes lives in `~/.aiboard/` (index, logs, pane list). Client colours are yours to define in `~/.aiboard/clients.json` (see `board/clients.example.json`).

## Privacy: local only

- The board server listens on `127.0.0.1` only, checks `Host` and `Origin`, and rejects cross-site writes.
- The board page is served with a Content-Security-Policy of `default-src 'self'; connect-src 'self'` — the browser engine itself blocks any request to another host.
- Secrets that appear in transcripts (API keys, tokens) are masked before display.
- **Measured:** `scripts/prove-local-only.sh` samples every socket of the app and the board server once a second. On 2026-09-18, 90 s with a clean config: **0 connections to anything other than this Mac**. Run it yourself.
- Not counted: the agents you run inside the terminals (Claude Code, Codex) talk to their own APIs — that is them, not AIBoard. If you add `remote_hosts` to `~/.aiboard/config.json`, the board uses `ssh` to those hosts, because you asked it to.

## Testing

`python3 scripts/uat/run_uat.py` runs the acceptance tests ([docs/uat/test-cases.md](docs/uat/test-cases.md)) against a separate board server on port 8793. It never sends input to or stops your real sessions: those requests are intercepted, and the real-signal test only targets a process the test started itself.

## Status

Early. Built and used daily by one person running many agents across clients. Known gaps: Mac only; no cloud or team features; Codex approvals are not detected yet.

## License

MIT
