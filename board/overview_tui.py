#!/usr/bin/env python3
"""overview_tui.py — `cs map`。iTerm の中で動く Figma 風の「AI作業の全体地図」(textual)。

ブラウザ版(overview.html)と同じ骨組みをターミナルに移したもの:
  上部バー   🔴あなたの番 / 🟢作業中 / メモリ / 今日の強制終了 / macmini / モデル別の件数(クリックで絞り込み)
  キャンバス 角丸の枠(顧客ごと → 自社プロジェクト → 自社・その他(直近) → macmini の無人ジョブ)の中にカード
             ズーム3段階(遠景=枠の要約だけ / 中景=カード1行 / 近景=カード4行)
  詳細パネル Enter かクリックで右(狭いときは下)に開く。いまやっていること・直近の流れ・画面末尾・関係

判定とデータ取得はここでは書かない。全部借りる:
  overview.snapshot() / overview.detail() / overview.timeline_*() / overview.redact()
  overview_index.load_index() / build() / query()   ※索引(index.json 80MB超)は読むと 700MB 使うので
                                                      子プロセスで読んで絞った行だけ受け取り、親は軽いまま
  overview_server.resume_in_iterm()  /  cs.go()     ※副作用のある操作(g / r)。r は y/N の確認を挟む
  clients.py / models.py の色と絵文字(model_style / client は snapshot と索引に既に入っている)

操作(画面下に常時表示): ←↓↑→ hjkl=カード移動  Tab/Shift+Tab=次/前の枠  Enter/クリック=詳細
  +/-=ズーム  g=iTermでそのタブへ  r=過去セッションを再開(確認あり)  /=検索  c=顧客  m=モデル
  p=過去の表示  u=無人実行の表示  t=あなたの番へ  w=作業中へ  1-9=関係先へ  x=長文の展開  q=終了
更新: 生きている情報は3秒ごと(スレッド)。索引は起動時+5分ごと(子プロセス)。選択・スクロール・パネルは保つ。
"""
import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.expanduser("~"), ".claude", "tools")
for p in (HERE, TOOLS):
    if p not in sys.path:
        sys.path.append(p)

SNAP_INTERVAL = 3          # 秒。生きている情報
INDEX_INTERVAL = 300       # 秒。索引
DETAIL_INTERVAL = 5        # 秒。開いている詳細パネル(生きているタブ)
INDEX_DAYS = 30
PAST_CAP = 8               # 1枠に出す過去カードの上限(自社・その他は HOME_CAP)
HOME_CAP = 16
UNATTENDED_CAP = 400       # 索引から受け取る無人実行の行数(新しい順)。u で出す分にしか使わない

ZOOM_FAR, ZOOM_MID, ZOOM_NEAR = 0, 1, 2
ZOOM_NAME = {ZOOM_FAR: "遠景", ZOOM_MID: "中景", ZOOM_NEAR: "近景"}
CARD_W = {ZOOM_MID: 40, ZOOM_NEAR: 44}
CARD_H = {ZOOM_MID: 1, ZOOM_NEAR: 4}
JOB_H = {ZOOM_MID: 1, ZOOM_NEAR: 2}
FAR_W, FAR_H = 54, 3       # 遠景の枠(中の行数)


# ================================================================ 索引(子プロセス) ====

def needs_you(n):
    """人が動かないと進まないか。真理値表(board/decide.py)の dock を読む。答えを持たない古い盤サーバの時だけ state で見る"""
    ui = n.get("ui")
    return bool(ui.get("dock")) if ui else n.get("state") == "確認待ち"


# 真理値表の kind → 色の種類(盤の overview.html の KIND_CLASS と同じ分け方)。状態名と印は表の答え(ui.label / ui.mark)を読む
KIND_COLOR = {"need": "turn", "auth": "turn", "billing": "turn", "error": "turn", "limited": "limited", "resumable": "yourturn",
              "yourturn": "yourturn", "loop": "loop", "work": "work", "idle": "past", "starting": "past", "ended": "past"}


def color_kind(n):
    """カードの色の種類: turn / yourturn / work / limited / past。答えが無い時だけ mark で見る"""
    ui = n.get("ui")
    if ui and ui.get("kind"):
        return KIND_COLOR.get(ui["kind"], "work")
    return "turn" if n.get("state") == "確認待ち" else "work" if n.get("mark") in ("🟢", "🟩") else "yourturn" if n.get("mark") == "🟡" else "past"


def mark_of(n):
    """カードの印の絵文字。表(decide.py の MARK)の答え。答えが無い古い形だけ観測の mark"""
    ui = n.get("ui")
    return (ui.get("mark") or n.get("mark", "")) if ui else n.get("mark", "")


def state_label(n):
    """状態名。表(decide.py の LABEL)の答え。答えが無い古い形だけ state"""
    ui = n.get("ui")
    return (ui.get("label") or ui.get("state") or n.get("state", "")) if ui else n.get("state", "")


def index_child(days, build, live_ids):
    """索引を読んで、表示に要る行だけ JSON で返す。親プロセスのメモリを増やさないために別プロセスで動く。"""
    import overview  # noqa
    import overview_index  # noqa
    t0 = time.time()
    stats = None
    if build:
        idx, stats = overview_index.build(days=days)
        del idx
    q = overview_index.query_db(days=days, include_unattended=True, live_ids=live_ids)
    rows = []
    n_unatt = 0
    for r in q["records"]:
        if r.get("role") == "subagent":               # サブエージェントは親の child_models(子の数)だけ使う。行は送らない
            continue
        if r.get("role") == "unattended":
            n_unatt += 1
            if n_unatt > UNATTENDED_CAP:              # 新しい順に並んでいる。u で出す分は上限まで
                continue
        r["path"] = r.get("path") or ""
        r["first_prompt"] = overview.redact(r.get("first_prompt", ""))
        r["last_prompt"] = overview.redact(r.get("last_prompt", ""))
        r["title"] = overview.redact(r.get("title", "") or "")
        role = r.get("role")
        if role in ("subagent", "review", "helper"):   # カードにしない行は小さく(親の「子N」と関係一覧にだけ使う)
            r = {k: r.get(k) for k in ("id", "ai", "model_style", "kind", "role", "parent", "parent_session",
                                       "start", "end", "path", "cwd", "project", "client", "prompts", "tools")} | {"first_prompt": r["first_prompt"][:80]}
        elif role == "unattended":                    # 無人実行は u で出すときだけ。件数が多い(30日で6千超)ので短く
            r = {k: r.get(k) for k in ("id", "ai", "model_style", "kind", "role", "start", "end", "path", "cwd", "project",
                                       "client", "prompts", "tools", "unattended", "unattended_by", "title")} | \
                {"first_prompt": r["first_prompt"][:80], "last_prompt": r["last_prompt"][:80]}
        rows.append(r)
    edges = []
    for e in q["edges"]:
        if e["kind"] == "subagent":
            continue
        e = dict(e)
        if isinstance(e.get("evidence"), str) and len(e["evidence"]) > 160:
            e["evidence"] = e["evidence"][:160]
        edges.append(e)
    return {"ok": True, "records": rows, "edges": edges, "built": q.get("built"), "counts": q["counts"],
            "stats": stats, "took": round(time.time() - t0, 1), "n_index": q.get("n_index", 0)}


def server_alive():
    """ブラウザ版のサーバーが動いていれば、索引の再構築(index.json への書き込み)はそちらに任せる。"""
    try:
        pid = int(open(os.path.join(TOOLS, "overview_server.pid")).read().strip())
        os.kill(pid, 0)
        cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        return "overview_server.py" in cmd and "--serve" in cmd
    except (OSError, ValueError):
        return False


def fetch_index(days, live_ids, build):
    """子プロセスで index_child() を動かして結果を受け取る。失敗は ok=False と reason。"""
    cmd = [sys.executable, os.path.abspath(__file__), "--index-child", "--days", str(days)] + (["--build"] if build else [])
    try:
        r = subprocess.run(cmd, input=json.dumps(list(live_ids)), capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "索引の子プロセスが900秒で終わらない"}
    except OSError as e:
        return {"ok": False, "reason": f"索引の子プロセス起動失敗: {e}"}
    if r.returncode != 0:
        return {"ok": False, "reason": f"索引の子プロセス rc={r.returncode}: {r.stderr.strip()[-300:]}"}
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        return {"ok": False, "reason": f"索引の JSON が読めない: {e} / stderr: {r.stderr.strip()[-200:]}"}


# ================================================================ 文字幅 ====
from rich.cells import cell_len, get_character_cell_size  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.segment import Segment  # noqa: E402
from rich.text import Text  # noqa: E402


def oneline(s):
    return " ".join(str(s or "").split())


def clip(s, w):
    s = oneline(s)
    if w <= 0:
        return ""
    if cell_len(s) <= w:
        return s
    out, cur = [], 0
    for ch in s:
        cw = get_character_cell_size(ch)
        if cw < 0:
            continue
        if cur + cw > w - 1:
            break
        out.append(ch)
        cur += cw
    return "".join(out) + "…"


def hexc(rgb, default="#888888"):
    if not rgb:
        return default
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb[:3])


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


def fmt_t(t):
    if not t:
        return "-"
    return time.strftime("%m/%d %H:%M", time.localtime(t))


def fmt_iso(s):
    if not s:
        return ""
    try:
        import datetime as dt
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
        return d.strftime("%m/%d %H:%M")
    except ValueError:
        return str(s)[:16]


# ================================================================ 描画バッファ ====
class Buf:
    """(文字, Style) のセル表。全角・絵文字は2セル(2つ目は None)。put で上書きすると相方を空白に戻す。"""

    def __init__(self, w, h, base):
        self.w, self.h, self.base = w, h, base
        self.rows = [[(" ", base)] * w for _ in range(h)]

    def _clear(self, row, x):
        c = row[x]
        if c is None:
            if x - 1 >= 0 and row[x - 1] is not None:
                row[x - 1] = (" ", row[x - 1][1])
        elif x + 1 < self.w and row[x + 1] is None and get_character_cell_size(c[0]) == 2:
            row[x + 1] = (" ", c[1])

    def put(self, x, y, s, style, maxw=None):
        if y < 0 or y >= self.h or not s:
            return x
        row = self.rows[y]
        limit = self.w if maxw is None else min(self.w, x + maxw)
        for ch in s:
            cw = get_character_cell_size(ch)
            if cw <= 0:
                continue
            if x + cw > limit:
                break
            if x >= 0:
                self._clear(row, x)
                if cw == 2:
                    self._clear(row, x + 1)
                row[x] = (ch, style)
                if cw == 2:
                    row[x + 1] = None
            x += cw
        return x

    def fill(self, x, y, w, h, style):
        for yy in range(max(0, y), min(self.h, y + h)):
            row = self.rows[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                self._clear(row, xx)
                row[xx] = (" ", style)

    def box(self, x, y, w, h, style, rounded=True, dotted=False):
        """枠線。(x,y) 左上、w×h。中は触らない。"""
        if w < 2 or h < 2:
            return
        tl, tr, bl, br = ("╭", "╮", "╰", "╯") if rounded else ("┌", "┐", "└", "┘")
        hz, vt = ("┄", "┆") if dotted else ("─", "│")
        self.put(x, y, tl + hz * (w - 2) + tr, style)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, style)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, vt, style)
            self.put(x + w - 1, yy, vt, style)

    def segments(self, y):
        row = self.rows[y]
        out, cur_style, cur = [], None, []
        for c in row:
            if c is None:
                continue
            ch, st = c
            if st is cur_style or st == cur_style:
                cur.append(ch)
            else:
                if cur:
                    out.append(Segment("".join(cur), cur_style))
                cur_style, cur = st, [ch]
        if cur:
            out.append(Segment("".join(cur), cur_style))
        return out


# ================================================================ ノード ====
def is_home(cwd):
    return not cwd or re.fullmatch(r"/Users/[^/]+/?", cwd) is not None


def frame_key_of(client, cwd, project):
    if client:
        return "c:" + client["id"]
    if is_home(cwd):
        return "home"
    return "p:" + (project or "?")


def live_node(s, now):
    ai = "Codex" if (s.get("ai") or "").startswith("Codex") else "Claude"
    return {"id": s["sid"], "live": True, "s": s, "ai": ai, "model": s.get("model_style"), "client": s.get("client"),
            "cwd": s.get("cwd", ""), "project": s.get("project", ""),
            "frame": frame_key_of(s.get("client"), s.get("cwd"), s.get("project")),
            "state": s["state"], "mark": s["mark"], "ui": s.get("ui"), "doing": s.get("doing", ""), "task": s.get("task", ""),
            "t": now - (s["ago"] or 0) if s.get("ago") is not None else now,
            "text": oneline(" ".join(str(x) for x in (s.get("task"), s.get("topic"), s.get("cwd"), s.get("doing"), s.get("tab"), (s.get("model_style") or {}).get("label")))).lower(),
            "tab": s.get("tab"), "role": "live", "kind": "live", "title": s.get("topic", ""), "parent": None, "unattended": False}


def past_node(r):
    role = r.get("role") or "human"
    parent = r.get("parent") if r.get("kind") == "subagent" else (r.get("parent_session") if role in ("review", "helper") else None)
    return {"id": r["id"], "live": False, "r": r, "ai": r.get("ai", ""), "model": r.get("model_style"), "client": r.get("client"),
            "cwd": r.get("cwd", ""), "project": r.get("project", ""),
            "frame": frame_key_of(r.get("client"), r.get("cwd"), r.get("project")),
            "state": {"subagent": "サブエージェント", "review": "査読", "helper": "補助", "unattended": "無人"}.get(role, "終了"),
            "mark": "⚪", "doing": r.get("last_prompt", "") or "", "task": r.get("first_prompt", "") or "", "t": r.get("end") or 0,
            "text": oneline(" ".join(str(x) for x in (r.get("title"), r.get("first_prompt"), r.get("last_prompt"), r.get("cwd"), " ".join(r.get("files") or []), (r.get("model_style") or {}).get("label")))).lower(),
            "tab": None, "role": role, "kind": r.get("kind"), "title": r.get("title") or "", "parent": parent,
            "unattended": bool(r.get("unattended")), "ghost": bool(r.get("ghost"))}


# ================================================================ Textual ====
from textual import events  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, VerticalScroll  # noqa: E402
from textual.geometry import Region, Size  # noqa: E402
from textual.screen import ModalScreen, Screen  # noqa: E402
from textual.scroll_view import ScrollView  # noqa: E402
from textual.strip import Strip  # noqa: E402
from textual.widgets import Input, Static  # noqa: E402

C_BG = "#14161c"
C_FRAME = "#5b6270"
C_CARD = "#262b35"
C_CARD_PAST = "#1a1d24"
C_INK = "#e5e7eb"
C_MUTED = "#9aa1ad"
C_DIM = "#6b7280"
C_RED = "#f87171"
C_GREEN = "#4ade80"
C_YELLOW = "#facc15"
C_BLUE = "#60a5fa"
C_TEAL = "#2dd4bf"
C_PURPLE = "#b98cff"
C_TURN_BG = "#3a1c20"
C_SEL_BG = "#33405a"


def st(color=None, bg=None, bold=False, dim=False):
    return Style(color=color, bgcolor=bg, bold=bold, dim=dim)


class MapCanvas(ScrollView):
    """キャンバス。1つのウィジェットに全部描く(カードごとにウィジェットを作らない=軽い)。"""

    can_focus = True
    DEFAULT_CSS = f"""
    MapCanvas {{ background: {C_BG}; overflow-x: hidden; overflow-y: scroll; scrollbar-size-vertical: 1; }}
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.buf = None
        self.hits = []          # (x0, y0, x1, y1, id)  クリック・矢印移動の当たり判定
        self.rects = {}         # id → (x, y, w, h)
        self.frame_rects = {}   # frame key → (x, y, w, h)
        self.order = []         # 表示順の id(フレーム順 → カード順)
        self._cache = {}
        self._w = 0

    def content_width(self):
        w = self.scrollable_content_region.width
        return w if w > 0 else max(20, self.size.width - 1)

    def set_buffer(self, buf, hits, rects, frame_rects, order):
        self.buf, self.hits, self.rects, self.frame_rects, self.order = buf, hits, rects, frame_rects, order
        self._cache = {}
        self.virtual_size = Size(buf.w, buf.h)
        self.refresh()

    def render_line(self, y):
        scroll_x, scroll_y = self.scroll_offset
        y += scroll_y
        if self.buf is None or y >= self.buf.h:
            return Strip.blank(self.size.width, st(bg=C_BG))
        s = self._cache.get(y)
        if s is None:
            s = Strip(self.buf.segments(y), self.buf.w)
            self._cache[y] = s
        return s.crop(scroll_x, scroll_x + self.size.width)

    def hit_at(self, x, y):
        for x0, y0, x1, y1, nid in self.hits:
            if x0 <= x < x1 and y0 <= y < y1:
                return nid
        return None

    def on_click(self, event):
        scroll_x, scroll_y = self.scroll_offset
        nid = self.hit_at(event.x + scroll_x, event.y + scroll_y)
        if nid:
            self.app.select(nid, open_panel=True)
        event.stop()

    def on_resize(self, event):
        if self.content_width() != self._w:
            self._w = self.content_width()
            self.app.rebuild()

    def show_rect(self, nid):
        r = self.rects.get(nid)
        if r:
            x, y, w, h = r
            self.scroll_to_region(Region(x, max(0, y - 1), w, h + 2), animate=False, force=True)


class TopBar(Static):
    """上部バー(2行)。クリックできる範囲を zones に持つ。"""

    DEFAULT_CSS = "TopBar { height: 2; background: #1c1f27; color: #e5e7eb; padding: 0 1; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.zones = []   # (line, x0, x1, action, arg)

    def set_lines(self, lines):
        """lines: [[(text, style, action, arg[, short]), ...], ...]。行は幅で切る(折り返さない)。
        幅に入らない行は、short(短い言い方)を持つ部分をそれに置き換えてから切る。"""
        self.zones = []
        width = max(20, self.size.width - 2)
        out = Text(no_wrap=True, overflow="crop")
        for li, parts in enumerate(lines):
            parts = [tuple(p) + (None,) * (5 - len(p)) for p in parts]
            # 入らないときは、右端の部分から順に短い言い方へ(入った時点でやめる)
            for i in range(len(parts) - 1, -1, -1):
                if sum(cell_len(p[0]) for p in parts) <= width:
                    break
                if parts[i][4] is not None:
                    parts[i] = (parts[i][4],) + parts[i][1:4] + (None,)
            x = 0
            for text, style, action, arg, _ in parts:
                text = clip(text, width - x) if x + cell_len(text) > width else text
                if not text:
                    break
                if action:
                    self.zones.append((li, x, x + cell_len(text), action, arg))
                out.append(text, style)
                x += cell_len(text)
            if li < len(lines) - 1:
                out.append("\n")
        self.update(out)

    def on_click(self, event):
        for li, x0, x1, action, arg in self.zones:
            if li == event.y and x0 <= event.x - 1 < x1:   # padding 1
                getattr(self.app, action)(arg)
                break
        event.stop()


class Confirm(ModalScreen):
    """y/N の確認。"""

    DEFAULT_CSS = """
    Confirm { align: center middle; background: rgba(0,0,0,0.55); }
    Confirm > Static { width: 70; max-width: 95%; padding: 1 2; border: round #f87171; background: #1c1f27; color: #e5e7eb; }
    """
    BINDINGS = [Binding("y", "yes", "はい"), Binding("n", "no", "いいえ"), Binding("escape", "no", "いいえ"), Binding("enter", "no", "いいえ")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Static(Text(self.message + "\n\n実行しますか？  [y] はい   [N] いいえ(既定)", no_wrap=False))

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class MapScreen(Screen):
    """Screen 既定の Tab(focus_next, priority) を、枠の移動に差し替える。"""
    BINDINGS = [Binding("tab", "app.next_frame(1)", "次の枠", priority=True),
                Binding("shift+tab", "app.next_frame(-1)", "前の枠", priority=True)]


class MapApp(App):
    TITLE = "AI作業の全体地図"
    CSS = f"""
    Screen {{ background: {C_BG}; }}
    #main {{ layout: horizontal; height: 1fr; }}
    #canvas {{ width: 1fr; height: 1fr; }}
    #panel {{ width: 58; height: 1fr; display: none; background: #1c1f27; border-left: solid #3a3f4a; padding: 0 1; }}
    #panel.open {{ display: block; }}
    Screen.narrow #main {{ layout: vertical; }}
    Screen.narrow #panel {{ width: 100%; height: 45%; border-left: none; border-top: solid #3a3f4a; }}
    #hint {{ height: 1; background: #1c1f27; color: {C_MUTED}; padding: 0 1; }}
    #search {{ display: none; height: 3; }}
    #search.open {{ display: block; }}
    """
    BINDINGS = [
        Binding("q", "quit", "終了"),
        Binding("up,k", "move('up')", "上", show=False), Binding("down,j", "move('down')", "下", show=False),
        Binding("left,h", "move('left')", "左", show=False), Binding("right,l", "move('right')", "右", show=False),
        Binding("enter", "open_detail", "詳細", show=False),
        Binding("escape", "escape", "閉じる", show=False),
        Binding("g", "go_tab", "iTermで開く", show=False), Binding("r", "resume", "再開", show=False),
        Binding("c", "cycle_client", "顧客", show=False), Binding("m", "cycle_model", "モデル", show=False),
        Binding("p", "toggle_past", "過去", show=False), Binding("u", "toggle_unattended", "無人", show=False),
        Binding("t", "jump('turn')", "あなたの番へ", show=False), Binding("w", "jump('work')", "作業中へ", show=False),
        Binding("x", "toggle_expand", "長文", show=False),
        Binding("pagedown", "panel_scroll(1)", "", show=False), Binding("pageup", "panel_scroll(-1)", "", show=False),
    ] + [Binding(str(i), f"relation({i})", "", show=False) for i in range(1, 10)]

    def __init__(self, days=INDEX_DAYS, build_index=None, snap_interval=SNAP_INTERVAL):
        super().__init__()
        self.days = days
        self.build_index = build_index      # None=サーバーが居なければ構築
        self.snap_interval = snap_interval
        self.snap = None
        self.snap_error = ""
        self.snap_at = None
        self.index = {"records": [], "edges": [], "built": None, "counts": None}
        self.index_error = ""
        self.index_loading = True
        self.zoom = ZOOM_NEAR
        self.sel = None
        self.filters = {"q": "", "client": "", "model": "", "past": True, "unattended": False}
        self.expand_long = False
        self.panel_open = False
        self.detail = None          # 詳細パネルの中身(取得結果)
        self.detail_for = None
        self.nodes = []
        self.by_id = {}
        self.shown = {}
        self.frames = []
        self.edges_by = {}
        self.related = set()
        self.first_fit = True

    # ------------------------------------------------------------ 構成 ----
    def get_default_screen(self):
        return MapScreen(id="_default")

    def compose(self) -> ComposeResult:
        yield TopBar(id="top")
        with Horizontal(id="main"):
            yield MapCanvas(id="canvas")
            yield VerticalScroll(Static(id="panel_body"), id="panel")
        yield Input(placeholder="検索: 依頼文・フォルダ・ファイル名・タブ (Enter で確定 / Esc で解除)", id="search")
        yield Static(id="hint")

    def on_mount(self):
        self.query_one("#canvas").focus()
        self.update_hint()
        self.tick_snapshot()
        self.tick_index()
        self.set_interval(self.snap_interval, self.tick_snapshot)
        self.set_interval(INDEX_INTERVAL, self.tick_index)
        self.set_interval(DETAIL_INTERVAL, self.tick_detail)

    def on_resize(self, event):
        self.screen.set_class(self.size.width < 150, "narrow")

    # ------------------------------------------------------------ 取得(UI を止めない) ----
    def tick_snapshot(self):
        self.run_worker(self._snapshot_work, thread=True, exclusive=True, group="snap", exit_on_error=False)

    def _snapshot_work(self):
        import overview
        try:
            snap = overview.snapshot()
        except BaseException as e:   # cs.osa() は sys.exit する
            self.call_from_thread(self.apply_snapshot, None, f"{type(e).__name__}: {e}")
            return
        self.call_from_thread(self.apply_snapshot, snap, "")

    def apply_snapshot(self, snap, error):
        self.snap_error = error
        if snap:
            self.snap = snap
            self.snap_at = time.time()
        self.rebuild()
        if self.first_fit and self.snap:
            self.first_fit = False
            self.action_jump("turn", quiet=True) or self.action_jump("work", quiet=True)

    def tick_index(self):
        self.index_loading = True
        self.run_worker(self._index_work, thread=True, exclusive=True, group="index", exit_on_error=False)

    def _index_work(self):
        live = [s["sid"] for s in (self.snap or {}).get("sessions", []) if s.get("sid")]
        build = self.build_index if self.build_index is not None else not server_alive()
        res = fetch_index(self.days, live, build)
        self.call_from_thread(self.apply_index, res)

    def apply_index(self, res):
        self.index_loading = False
        if res.get("ok"):
            self.index = res
            self.index_error = ""
        else:
            self.index_error = res.get("reason", "不明")
        self.rebuild()

    def tick_detail(self):
        if self.panel_open and self.sel and self.sel in self.by_id and self.by_id[self.sel]["live"]:
            self.load_detail(self.sel)

    def load_detail(self, nid):
        n = self.by_id.get(nid)
        if not n:
            return
        self.run_worker(lambda: self._detail_work(n), thread=True, exclusive=True, group="detail", exit_on_error=False)

    def _detail_work(self, n):
        import overview
        try:
            if n["live"]:
                d = overview.detail(n["tab"], sess=self.snap["sessions"])
            else:
                r = n["r"]
                if r.get("ai") == "Codex":
                    tl = overview.timeline_codex(r["path"], limit=400, with_text=True)
                else:
                    tl = overview.timeline_claude(r["path"], limit=400, tail_bytes=6_000_000, with_text=True,
                                                  sidechain_ok=(r.get("kind") == "subagent"))
                d = {"ok": True, "timeline": tl}
        except BaseException as e:
            d = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        self.call_from_thread(self.apply_detail, n["id"], d)

    def apply_detail(self, nid, d):
        self.detail, self.detail_for = d, nid
        self.render_panel()

    # ------------------------------------------------------------ ノードと枠 ----
    def build_nodes(self):
        now = time.time()
        nodes, seen = [], set()
        for s in (self.snap or {}).get("sessions", []):
            n = live_node(s, now)
            nodes.append(n)
            seen.add(n["id"])
        for r in self.index.get("records", []):
            if r["id"] in seen:
                continue
            nodes.append(past_node(r))
            seen.add(r["id"])
        self.by_id = {n["id"]: n for n in nodes}
        # 子(サブエージェント/査読/補助)は親の数として持つ。カードにはしない
        for n in nodes:
            n["kids"] = {}
        for n in nodes:
            cm = (n.get("r") or {}).get("child_models") or {}
            if cm:
                n["kids"]["子"] = sum(cm.values())
            p = n.get("parent")
            if p and p in self.by_id:
                k = {"subagent": "子", "review": "査読", "helper": "補助"}.get(n["role"], "子")
                self.by_id[p]["kids"][k] = self.by_id[p]["kids"].get(k, 0) + 1
        # エッジ
        eb = {}
        for e in self.index.get("edges", []):
            eb.setdefault(e["from"], []).append(e)
            eb.setdefault(e["to"], []).append(e)
        self.edges_by = eb
        self.nodes = nodes

    def related_of(self, nid):
        out = set()
        for e in self.edges_by.get(nid, []):
            out.add(e["from"] if e["to"] == nid else e["to"])
        out.discard(nid)
        return out

    def passes(self, n):
        f = self.filters
        if n["role"] in ("subagent", "review", "helper"):
            return False
        if n["id"] == self.sel or n["id"] in self.related:
            return True
        if not n["live"]:
            if not f["past"]:
                return False
            if n["role"] == "unattended" and not f["unattended"]:
                return False
        if f["q"] and f["q"] not in n["text"]:
            return False
        if f["client"] and (not n["client"] or n["client"]["id"] != f["client"]):
            return False
        if f["model"] and (not n["model"] or n["model"].get("label") != f["model"]):
            return False
        return True

    @staticmethod
    def live_rank(n):
        if not n["live"]:
            return 9
        return {"turn": 0, "work": 1, "yourturn": 2, "limited": 3, "loop": 3, "past": 4}.get(color_kind(n), 5)   # 並びも表の答えから

    def build_frames(self):
        groups = {}
        for n in self.nodes:
            if self.passes(n):
                groups.setdefault(n["frame"], []).append(n)
        frames = []
        for key, g in groups.items():
            g.sort(key=lambda n: (self.live_rank(n), -(n["t"] or 0)))
            cap = HOME_CAP if key == "home" else PAST_CAP
            keep, hidden = [], 0
            for n in g:
                if n["live"] or n["id"] == self.sel or n["id"] in self.related or sum(1 for k in keep if not k["live"]) < cap:
                    keep.append(n)
                else:
                    hidden += 1
            live = [n for n in keep if n["live"]]
            sample = keep[0]
            if key.startswith("c:"):
                c = sample["client"]
                label, color, rank = f"{c.get('emoji', '')} {c['label']}", hexc(c.get("rgb")), 0
            elif key == "home":
                label, color, rank = "● 自社・その他（直近）", C_FRAME, 2
            else:
                label, color, rank = "● " + key[2:], "#8b93a5", 1
            snapg = None
            if self.snap:
                snapg = next((c for c in self.snap.get("clients", []) if "c:" + c["id"] == key), None) or \
                    next((p for p in self.snap.get("projects", []) if "p:" + p["name"] == key), None)
            frames.append({"key": key, "label": label, "color": color, "rank": rank, "nodes": keep, "hidden": hidden,
                           "turn": sum(1 for n in live if needs_you(n)),
                           "work": sum(1 for n in live if color_kind(n) == "work"),
                           "wait": sum(1 for n in live if color_kind(n) == "yourturn"),
                           "past": sum(1 for n in keep if not n["live"]),
                           "today": (snapg or {}).get("today_requests"), "today_partial": (snapg or {}).get("today_requests_partial"),
                           "n_live": len(live), "latest": (live[0] if live else keep[0]), "jobs": None})
        frames.sort(key=lambda f: (f["rank"], -f["turn"], -f["work"], -f["n_live"], -(f["latest"]["t"] or 0)))
        # macmini
        mm = (self.snap or {}).get("macmini")
        if mm is not None:
            jobs = []
            if mm.get("ok"):
                y0 = mm.get("ytfactory") or {}
                jobs.append({"id": "mm:ytf", "title": "YouTube工場 " + str(y0.get("label", "")),
                             "sub": ("⏸ PAUSE中" if y0.get("paused") else "稼働") + f" 最後 {fmt_t(y0.get('out_log_mtime'))} exit {y0.get('last_exit')} runs {y0.get('runs')}",
                             "ok": y0.get("last_exit") == 0})
                for i, c in enumerate(mm.get("cron_ai") or []):
                    jobs.append({"id": f"mm:cron{i}", "title": f"cron claude -p {c.get('schedule', '')}", "sub": "「" + c.get("prompt", "") + "」", "ok": True})
                pm2 = mm.get("pm2") or {}
                down = [p.get("name") for p in pm2.get("list", []) if p.get("status") != "online"]
                jobs.append({"id": "mm:pm2", "title": f"PM2 online {pm2.get('online')}/{pm2.get('total')}", "sub": ("停止: " + ", ".join(map(str, down))) if down else "全部 online", "ok": not down})
                cw = mm.get("cron_wrap") or {}
                td, l24 = cw.get("today", {}), cw.get("last24h", {})
                jobs.append({"id": "mm:wrap", "title": f"cron-wrap 今日 ok {td.get('ok')} / FAIL {td.get('FAIL')}", "sub": f"24h ok {l24.get('ok')} / FAIL {l24.get('FAIL')}・{len(cw.get('jobs', []))}本", "ok": td.get("FAIL") == 0})
                for j in [j for j in cw.get("jobs", []) if not j.get("ok") and (j.get("ago") or 0) < 86400][:12]:
                    jobs.append({"id": "mm:job:" + j["name"], "title": "FAIL " + j["name"], "sub": f"exit={j.get('exit')} {fmt_dur(j.get('ago'))}前" + (" 外部送信あり" if j.get("external") else ""), "ok": False})
                sub = f"取得 {fmt_dur(time.time() - mm.get('fetched', time.time()))}前 ({mm.get('host')}) load {mm.get('load1')}"
                color = C_TEAL
            else:
                jobs.append({"id": "mm:err", "title": "取得失敗", "sub": mm.get("reason", "不明"), "ok": False})
                sub = "取得失敗"
                color = C_RED
            frames.append({"key": "mm", "label": "▣ macmini 無人AIジョブ", "color": color, "rank": 3, "nodes": [], "hidden": 0,
                           "turn": 0, "work": 0, "wait": 0, "past": 0, "today": None, "n_live": 0, "latest": None, "jobs": jobs, "sub": sub,
                           "fail": sum(1 for j in jobs if not j["ok"])})
        self.frames = frames
        self.shown = {n["id"]: n for f in frames for n in f["nodes"]}

    # ------------------------------------------------------------ 描画 ----
    def rebuild(self):
        canvas = self.query_one("#canvas", MapCanvas)
        W = canvas.content_width()
        if self.sel and self.sel not in getattr(self, "by_id", {}) and not str(self.sel).startswith(("frame:", "mm:")):
            pass   # 索引が入れ替わっても選択は id で保つ(無くなれば描かれないだけ)
        self.build_nodes()
        self.related = self.related_of(self.sel) if self.sel else set()
        self.build_frames()
        z = self.zoom
        # --- 配置 ---
        x = y = row_h = 0
        placed = []   # (frame, fx, fy, fw, fh, cards[(node_or_job, cx, cy, cw, ch)])
        for f in self.frames:
            items = f["jobs"] if f["jobs"] is not None else f["nodes"]
            if z == ZOOM_FAR:
                fw, fh = min(W, FAR_W), FAR_H + 2
                cards, cols = [], 0
            else:
                cw = CARD_W[z]
                ch = JOB_H[z] if f["jobs"] is not None else CARD_H[z]
                pitch, pitch_h = cw + 2, ch + 2
                maxcols = max(1, (W - 4) // pitch)
                cols = max(1, min(len(items), maxcols))
                rows = max(1, math.ceil(len(items) / cols))
                fw, fh = min(W, cols * pitch + 4), rows * pitch_h + 2
                cards = []
            if x + fw > W and x > 0:
                x, y, row_h = 0, y + row_h + 1, 0
            if z != ZOOM_FAR:
                for i, it in enumerate(items):
                    cards.append((it, x + 2 + (i % cols) * pitch + 1, y + 1 + (i // cols) * pitch_h + 1, cw, ch))
            placed.append((f, x, y, fw, fh, cards))
            x, row_h = x + fw + 2, max(row_h, fh)
        H = y + row_h + 1
        base = st(color=C_INK, bg=C_BG)
        buf = Buf(W, max(H, 1), base)
        hits, rects, frame_rects, order = [], {}, {}, []
        for f, fx, fy, fw, fh, cards in placed:
            fstyle = st(color=f["color"], bg=C_BG)
            sel_frame = self.sel == "frame:" + f["key"]
            buf.box(fx, fy, fw, fh, st(color=C_BLUE if sel_frame else f["color"], bg=C_BG, bold=sel_frame))
            # 枠の左上に名前、右上に要約
            title = clip(("▶ " if sel_frame else "") + f["label"], fw - 6)
            buf.put(fx + 2, fy, " " + title + " ", st(color="#ffffff" if sel_frame else f["color"], bg=C_SEL_BG if sel_frame else C_BG, bold=True))
            if f["jobs"] is not None:
                summ = (f"❌{f['fail']} " if f["fail"] else "") + f["sub"]
            else:
                summ = f"🔴{f['turn']} 🟢{f['work']} " + (f"今日の依頼{f['today']}{'+' if f.get('today_partial') else ''}件 " if f["today"] is not None else "") + (f"+{f['hidden']}過去 " if f["hidden"] else "")
            summ = clip(summ, fw - cell_len(title) - 8)
            sx = fx + fw - 2 - cell_len(summ)
            if sx > fx + 2 + cell_len(title) + 3:
                buf.put(sx, fy, summ, st(color=C_RED if (f["turn"] or (f["jobs"] is not None and f["fail"])) else C_MUTED, bg=C_BG))
            frame_rects[f["key"]] = (fx, fy, fw, fh)
            if z == ZOOM_FAR:
                hits.append((fx, fy, fx + fw, fy + fh, "frame:" + f["key"]))
                order.append("frame:" + f["key"])
                self.draw_far(buf, f, fx, fy, fw)
                continue
            for it, cx, cy, cw, ch in cards:
                nid = it["id"]
                if f["jobs"] is not None:
                    self.draw_job(buf, it, cx, cy, cw, ch)
                else:
                    self.draw_card(buf, it, cx, cy, cw, ch)
                hits.append((cx - 1, cy - 1, cx + cw + 1, cy + ch + 1, nid))
                rects[nid] = (cx - 1, cy - 1, cw + 2, ch + 2)
                order.append(nid)
        canvas.set_buffer(buf, hits, rects, frame_rects, order)
        self.render_top()
        self.update_hint()
        if self.panel_open:
            self.render_panel()

    def draw_far(self, buf, f, fx, fy, fw):
        inner = fw - 4
        if f["jobs"] is not None:
            l1 = f["sub"] if f["fail"] == 0 else f"❌ 失敗 {f['fail']} / {len(f['jobs'])}件"
            buf.put(fx + 2, fy + 1, clip(l1, inner), st(color=C_RED if f["fail"] else C_GREEN, bg=C_BG))
            x = fx + 2
            for j in f["jobs"][:inner // 2]:
                x = buf.put(x, fy + 2, "● ", st(color=C_GREEN if j["ok"] else C_RED, bg=C_BG))
            buf.put(fx + 2, fy + 3, clip(" / ".join(j["title"] for j in f["jobs"] if not j["ok"]) or "全部 ok", inner), st(color=C_MUTED, bg=C_BG))
            return
        parts = [(f"🔴{f['turn']} ", C_RED if f["turn"] else C_DIM), (f"🟢{f['work']} ", C_GREEN if f["work"] else C_DIM),
                 (f"🟡{f['wait']}  ", C_YELLOW if f["wait"] else C_DIM), (f"過去{f['past']}", C_MUTED)]
        if f["today"] is not None:
            parts.append((f"  今日の依頼{f['today']}件", C_MUTED))
        x = fx + 2
        for txt, col in parts:
            x = buf.put(x, fy + 1, txt, st(color=col, bg=C_BG), maxw=fx + 2 + inner - x)
        # モデルの丸の並び(件数順)
        cnt = {}
        for n in f["nodes"]:
            m = n.get("model") or {}
            k = m.get("label") or "モデル不明"
            cnt.setdefault(k, [m.get("rgb"), 0, n["live"]])
            cnt[k][1] += 1
        x = fx + 2
        for k, (rgb, c, _) in sorted(cnt.items(), key=lambda kv: -kv[1][1]):
            x = buf.put(x, fy + 2, "●" * min(c, 12) + (f"{k}×{c} " if c else ""), st(color=hexc(rgb), bg=C_BG), maxw=fx + 2 + inner - x)
        lt = f["latest"]
        buf.put(fx + 2, fy + 3, clip("最新: " + (lt["task"] or lt["title"] or lt["doing"]), inner), st(color=C_INK if lt["live"] else C_MUTED, bg=C_BG))

    def ring(self, buf, cx, cy, cw, ch, color, dotted=False):
        buf.box(cx - 1, cy - 1, cw + 2, ch + 2, st(color=color, bg=C_BG, bold=not dotted), dotted=dotted)

    def draw_card(self, buf, n, cx, cy, cw, ch):
        live = n["live"]
        turn = live and needs_you(n)
        selected = self.sel == n["id"]
        related = n["id"] in self.related
        bg = C_SEL_BG if selected else C_TURN_BG if turn else C_CARD if live else C_CARD_PAST
        ink = C_INK if live else C_MUTED
        buf.fill(cx, cy, cw, ch, st(color=ink, bg=bg))
        m = n.get("model") or {}
        mc = hexc(m.get("rgb"))
        for yy in range(cy, cy + ch):
            buf.put(cx, yy, "█", st(color=mc, bg=bg, dim=not live))
        if selected:
            self.ring(buf, cx, cy, cw, ch, C_BLUE)
        elif turn:
            self.ring(buf, cx, cy, cw, ch, C_RED)
        elif related:
            self.ring(buf, cx, cy, cw, ch, "#8fa3c7", dotted=True)
        x0, w = cx + 2, cw - 2
        s = n.get("s") or {}
        acct = f"({s['account']})" if s.get("account") else ""
        badge = f"{m.get('emoji', '❔')} {m.get('label', 'モデル不明')}{acct}"
        rel_labels = []
        for e in self.edges_by.get(n["id"], []):
            other = e["from"] if e["to"] == n["id"] else e["to"]
            if e["kind"] == "continue" and e["to"] == n["id"]:
                rel_labels.append("↳つづき")
            elif self.sel and other == self.sel and e["kind"] != "subagent":
                rel_labels.append("↔" + e["label"])
        rel = " ".join(dict.fromkeys(rel_labels))[:20]
        kids = " ".join(f"{k}{v}" for k, v in n.get("kids", {}).items())
        if live and s.get("subagents"):
            kids = (kids + " " if kids else "") + f"子{len(s['subagents'])}動作中"
        if ch == 1:   # 中景: 1行。関係は「↳」1文字だけ
            rel = "↳" if rel else ""
            x = buf.put(x0, cy, badge + " ", st(color=mc, bg=bg, bold=True), maxw=w)
            state = f"{mark_of(n)} " + (f"{n['tab']} " if n["tab"] else "") + (fmt_dur(s.get("state_for")) + " " if live and s.get("state_for") is not None else fmt_t(n["t"]) + " " if not live else "")
            x = buf.put(x, cy, state, st(color=C_RED if turn else ink, bg=bg), maxw=x0 + w - x)
            body = (n["doing"] if live else (n["title"] or n["doing"])) or n["task"]
            buf.put(x, cy, clip(body, x0 + w - x - (cell_len(rel) + 1 if rel else 0)), st(color=ink, bg=bg), maxw=x0 + w - x)
            if rel:
                buf.put(x0 + w - cell_len(rel), cy, rel, st(color=C_BLUE, bg=bg))
            return
        # 近景: 4行
        tab = str(n["tab"]) if n["tab"] else ""
        x = buf.put(x0, cy, badge, st(color=mc, bg=bg, bold=True), maxw=w - cell_len(tab) - 1)
        if n.get("client"):
            c = n["client"]
            x = buf.put(x + 1, cy, clip(f"{c.get('emoji', '')}{c['label']}", w - (x + 1 - x0) - cell_len(tab) - 1), st(color=hexc(c.get("rgb")), bg=bg, bold=True))
        if tab:
            buf.put(x0 + w - cell_len(tab), cy, tab, st(color=ink, bg=bg, bold=True))
        if live:
            ck = color_kind(n)   # 色と状態名は真理値表の答えから(盤のカードと同じ)
            state = f"{mark_of(n)} {state_label(n)}" + (f" {fmt_dur(s['state_for'])}" if s.get("state_for") is not None else "")
            scol = {"turn": C_RED, "work": C_GREEN, "yourturn": C_YELLOW, "limited": C_PURPLE}.get(ck, ink)
        else:
            state = f"⚪ {n['state']} {fmt_t(n['t'])}"
            scol = C_MUTED
        right = " ".join(x for x in (rel, kids) if x)
        x = buf.put(x0, cy + 1, clip(state, w - cell_len(right) - 1), st(color=scol, bg=bg, bold=turn))
        if right:
            buf.put(x0 + w - cell_len(right), cy + 1, right, st(color=C_BLUE if rel else C_MUTED, bg=bg))
        if live:
            l3, l4 = "いま: " + (n["doing"] or ""), "依頼: " + (n["task"] or "")
        else:
            r = n["r"]
            l3 = ("題: " + n["title"]) if n["title"] else ("最後: " + (n["doing"] or ""))
            l4 = "依頼: " + (n["task"] or "") + (f"  ({r.get('prompts', 0)}発言/{r.get('tools', 0)}操作)" if r.get("prompts") else "")
        buf.put(x0, cy + 2, clip(l3, w), st(color=ink, bg=bg))
        buf.put(x0, cy + 3, clip(l4, w), st(color=C_MUTED, bg=bg))

    def draw_job(self, buf, j, cx, cy, cw, ch):
        selected = self.sel == j["id"]
        bg = C_SEL_BG if selected else C_CARD
        buf.fill(cx, cy, cw, ch, st(color=C_INK, bg=bg))
        col = C_GREEN if j["ok"] else C_RED
        for yy in range(cy, cy + ch):
            buf.put(cx, yy, "█", st(color=col, bg=bg))
        if selected:
            self.ring(buf, cx, cy, cw, ch, C_BLUE)
        elif not j["ok"]:
            self.ring(buf, cx, cy, cw, ch, C_RED)
        if ch == 1:
            buf.put(cx + 2, cy, clip(j["title"] + "  " + j["sub"], cw - 2), st(color=C_INK if j["ok"] else C_RED, bg=bg, bold=not j["ok"]))
        else:
            buf.put(cx + 2, cy, clip(j["title"], cw - 2), st(color=C_INK if j["ok"] else C_RED, bg=bg, bold=True))
            buf.put(cx + 2, cy + 1, clip(j["sub"], cw - 2), st(color=C_MUTED, bg=bg))

    # ------------------------------------------------------------ 上部バー・ヒント ----
    def render_top(self):
        top = self.query_one("#top", TopBar)
        s = self.snap
        l1, l2 = [], []
        if not s:
            l1.append((" 取得中… " if not self.snap_error else f" 取得失敗（{self.snap_error}）", st(color=C_RED if self.snap_error else C_MUTED), None, None))
        else:
            c = s["counts"]
            att = len(s.get("attention", []))
            l1.append((f"🔴 あなたの番 {att} ", st(color=C_RED, bold=True) if att else st(color=C_DIM), "action_jump", "turn", f"🔴 番{att} "))
            l1.append((f"🟢 作業中 {c['working']} ", st(color=C_GREEN, bold=True) if c["working"] else st(color=C_DIM), "action_jump", "work", f"🟢 作業{c['working']} "))
            l1.append((f"🟡 返答待ち {c['waiting']} ", st(color=C_YELLOW), None, None, f"🟡 待{c['waiting']} "))
            m = s.get("machine") or {}
            if m.get("ok"):
                pct = m.get("used_pct") or 0
                n = max(0, min(10, round(pct / 10)))
                col = C_RED if pct >= 85 else C_YELLOW if pct >= 70 else C_GREEN
                l1.append((" メモリ ", st(color=C_MUTED), None, None))
                l1.append(("█" * n, st(color=col), None, None))
                l1.append(("░" * (10 - n), st(color=C_DIM), None, None))
                l1.append((f" {pct}% ({m.get('used_gb')}/{m.get('total_gb')}GB swap {((m.get('swap_used_mb') or 0) / 1024):.1f}GB) ", st(color=col), None, None,
                           f" {pct}% swap{((m.get('swap_used_mb') or 0) / 1024):.1f}G "))
            else:
                l1.append((f" メモリ 取得失敗（{m.get('reason', '')}） ", st(color=C_RED), None, None))
            j = m.get("jetsam") or {}
            jt = j.get("today")
            l1.append((f"💥 今日 {jt if jt is not None else '?'}回 ", st(color=C_RED if jt else C_MUTED, bold=bool(jt)), None, None))
            mm = s.get("macmini") or {}
            if mm.get("ok"):
                l1.append((f"▣ macmini 取得OK {fmt_dur(time.time() - mm.get('fetched', time.time()))}前 ", st(color=C_TEAL), None, None,
                           f"▣ mm OK {fmt_dur(time.time() - mm.get('fetched', time.time()))}前 "))
            else:
                l1.append((f"▣ macmini 取得失敗（{clip(mm.get('reason', '不明'), 40)}） ", st(color=C_RED, bold=True), None, None))
            if self.snap_error:
                l1.append((f" ⚠ 更新失敗（{clip(self.snap_error, 40)}）", st(color=C_RED), None, None))
            # モデル別(生きている分)
            cnt = {}
            for x in s["sessions"]:
                ms = x.get("model_style")
                if x["mark"] == "⚪" or not ms:
                    continue
                k = ms["label"]
                cnt.setdefault(k, {"n": 0, "busy": 0, "st": ms})
                cnt[k]["n"] += 1
                if color_kind(x) == "work":   # 作業中は表の答えで数える(盤の counts.working と同じ)
                    cnt[k]["busy"] += 1
            for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]["n"]):
                on = self.filters["model"] == k
                l2.append((f"[{v['st']['emoji']} {k} ×{v['n']} 作業中{v['busy']}] ", st(color=hexc(v["st"]["rgb"]), bold=True, bg="#2e3444" if on else None), "set_model_filter", k,
                           f"[{v['st']['emoji']}{v['st'].get('short', k)}×{v['n']}] "))
        f = self.filters
        fl = []
        if f["q"]:
            fl.append(f"検索「{f['q']}」")
        if f["client"]:
            fl.append(f"顧客={f['client']}")
        if f["model"]:
            fl.append(f"モデル={f['model']}")
        fl.append("過去=" + ("表示" if f["past"] else "非表示"))
        if f["unattended"]:
            fl.append("無人=表示")
        l2.append((" ｜ " + " ".join(fl) + f" ｜ {ZOOM_NAME[self.zoom]}", st(color=C_MUTED), None, None))
        if self.index_error:
            l2.append((f" ｜ 索引 取得失敗（{clip(self.index_error, 50)}）", st(color=C_RED, bold=True), None, None))
        elif self.index_loading:
            l2.append((" ｜ 索引 読込中…", st(color=C_MUTED), None, None))
        else:
            ic = self.index.get("counts") or {}
            l2.append((f" ｜ 索引 会話{ic.get('human', 0)} 無人{ic.get('unattended', 0)} 子{ic.get('subagent', 0)} 更新{fmt_t(self.index.get('built'))}", st(color=C_MUTED), None, None,
                       f" ｜ 索引{ic.get('human', 0)}"))
        if s:
            l2.append((f" ｜ 更新 {time.strftime('%H:%M:%S', time.localtime(self.snap_at))} 取得{s.get('took')}s タブ{s['counts']['tabs']}", st(color=C_DIM), None, None,
                       f" ｜ {time.strftime('%H:%M:%S', time.localtime(self.snap_at))}"))
        top.set_lines([l1, l2])

    def update_hint(self):
        sel = self.by_id.get(self.sel) if self.sel else None
        keys = ["←↓↑→/hjkl 移動", "Tab 次の枠", "Enter 詳細", "+/- ズーム"]
        if sel and sel["live"] and sel.get("tab"):
            keys.append(f"g iTermでタブ{sel['tab']}へ")
        if sel and not sel["live"] and sel.get("role") != "subagent":
            keys.append("r 再開(確認あり)")
        keys += ["/ 検索", "c 顧客", "m モデル", "p 過去", "u 無人", "t 番", "w 作業中"]
        if self.panel_open:
            keys += ["1-9 関係先へ", "x 長文", "PgUp/PgDn パネル", "Esc 閉じる"]
        keys.append("q 終了")
        self.query_one("#hint", Static).update(Text(clip("  ".join(keys), max(10, self.size.width - 2)), no_wrap=True))

    # ------------------------------------------------------------ 詳細パネル ----
    def render_panel(self):
        body = self.query_one("#panel_body", Static)
        n = self.by_id.get(self.sel) if self.sel else None
        if not n:
            if self.sel and str(self.sel).startswith("mm:"):
                j = next((j for f in self.frames if f["jobs"] for j in f["jobs"] if j["id"] == self.sel), None)
                t = Text()
                if j:
                    t.append("▣ macmini 無人AIジョブ\n", st(color=C_TEAL, bold=True))
                    t.append(j["title"] + "\n", st(color=C_INK if j["ok"] else C_RED, bold=True))
                    t.append(j["sub"] + "\n", st(color=C_MUTED))
                    mm = (self.snap or {}).get("macmini") or {}
                    if mm.get("ok"):
                        t.append(f"\n{mm.get('host')} uptime {mm.get('uptime')} load {mm.get('load1')}\n取得 {fmt_t(mm.get('fetched'))} ({mm.get('took')}s)\n", st(color=C_MUTED))
                    else:
                        t.append("\n取得失敗: " + str(mm.get("reason")), st(color=C_RED))
                body.update(t)
            else:
                body.update(Text("カードを選ぶと詳細が出ます", style=st(color=C_MUTED)))
            return
        t = Text()
        m = n.get("model") or {}
        t.append(f" {m.get('emoji', '❔')} {m.get('label', 'モデル不明')} ", st(color="#ffffff", bg=hexc(m.get("rgb")), bold=True))
        if n.get("client"):
            c = n["client"]
            t.append(" ")
            t.append(f" {c.get('emoji', '')}{c['label']} ", st(color="#ffffff", bg=hexc(c.get("rgb")), bold=True))
            t.append(f" ({c.get('by', '')})", st(color=C_MUTED))
        else:
            t.append(" 自社 ", st(color="#ffffff", bg=C_DIM))
        t.append("\n")
        if n["live"]:
            s = n["s"]
            turn = needs_you(n)
            kv = [("状態", f"{mark_of(n)} {state_label(n)}" + (f"（{fmt_dur(s.get('state_for'))}）" if s.get("state_for") is not None else "")),
                  ("タブ", f"{s['tab']}   → g で iTerm のこのタブへ"), ("AI", s.get("ai", "") + (f" / アカウント {s['account']}" if s.get("account") else "")),
                  ("フォルダ", s.get("cwd", "")), ("メモリ", f"{s.get('mem_mb')}MB"),
                  ("開始/更新", f"{fmt_t(s.get('started'))} / {fmt_dur(s['ago']) + '前' if s.get('ago') is not None else '-'}"),
                  ("今日の依頼", f"{s.get('today_requests')}{'+' if s.get('today_requests_partial') else ''}件"), ("session", s.get("sid", ""))]
            for k, v in kv:
                t.append(k + " " * max(1, 11 - cell_len(k)), st(color=C_MUTED))
                t.append(str(v) + "\n", st(color=C_RED if (k == "状態" and turn) else C_INK, bold=(k == "状態" and turn)))
            t.append("\n■ いまやっていること\n", st(color=C_BLUE, bold=True))
            t.append(str(n["doing"]) + "\n")
            t.append("\n■ 関係\n", st(color=C_BLUE, bold=True))
            self.relations_text(t, n["id"])
            d = self.detail if self.detail_for == n["id"] else None
            t.append("\n■ 直近の流れ\n", st(color=C_BLUE, bold=True))
            if d is None:
                t.append("取得中…\n", st(color=C_MUTED))
            elif not d.get("ok"):
                t.append(f"取得失敗（{d.get('reason')}）\n", st(color=C_RED))
            else:
                self.timeline_text(t, d.get("timeline") or [])
                t.append("\n■ ターミナルの画面(末尾)\n", st(color=C_BLUE, bold=True))
                t.append((d.get("screen") or "(取得できない)") + "\n", st(color="#d5dbe5", bg="#0b0e14"))
        else:
            r = n["r"]
            kv = [("種類", f"⚪ {n['state']}" + (f"（無人: {r.get('unattended_by')}）" if r.get("unattended") else "")),
                  ("期間", f"{fmt_t(r.get('start'))} 〜 {fmt_t(r.get('end'))}"),
                  ("AI", f"{r.get('ai', '')}" + (f" / {r['account']}" if r.get("account") else "")), ("フォルダ", r.get("cwd", "")),
                  ("発言/操作", f"人 {r.get('prompts')} · AI {r.get('responses')} · ツール {r.get('tools')}"),
                  ("編集", ", ".join(os.path.basename(f[0]) + f"×{f[1]}" for f in (r.get("files_top") or [])[:6]) or "-"),
                  ("子", " ".join(f"{k}{v}" for k, v in n.get("kids", {}).items()) or "-"),
                  ("再開", "r で iTerm の新しいタブに " + ("codex resume" if r.get("ai") == "Codex" else "claude --resume") + "（確認あり）" if n["role"] != "subagent" else "サブエージェントは再開できない"),
                  ("id", n["id"])]
            for k, v in kv:
                t.append(k + " " * max(1, 11 - cell_len(k)), st(color=C_MUTED))
                t.append(str(v) + "\n")
            t.append("\n■ 関係\n", st(color=C_BLUE, bold=True))
            self.relations_text(t, n["id"])
            d = self.detail if self.detail_for == n["id"] else None
            t.append("\n■ 会話(依頼・返答・操作)  x で長文の展開\n", st(color=C_BLUE, bold=True))
            if d is None:
                t.append("読込中…\n", st(color=C_MUTED))
            elif not d.get("ok"):
                t.append(f"取得失敗（{d.get('reason')}）\n", st(color=C_RED))
            else:
                self.timeline_text(t, d.get("timeline") or [])
        body.update(t)

    def relations_text(self, t, nid):
        es = self.edges_by.get(nid, [])
        if not es:
            t.append("根拠のある関係は見つかっていない\n", st(color=C_MUTED))
            return
        self.rel_targets = []
        for i, e in enumerate(es[:9], 1):
            other = e["from"] if e["to"] == nid else e["from"] if e["from"] != nid else e["to"]
            if e["to"] == nid:
                other = e["from"]
            o = self.by_id.get(other)
            self.rel_targets.append(other)
            arrow = "←" if e["to"] == nid else "→"
            t.append(f"[{i}] ", st(color=C_BLUE, bold=True))
            t.append(f"{e['label']} {arrow} ", st(color=C_INK, bold=True))
            if o:
                m = o.get("model") or {}
                t.append(f"{m.get('emoji', '')}{m.get('label', '')} {fmt_t(o['t'])} ", st(color=hexc(m.get("rgb"))))
                t.append(clip(o.get("title") or o.get("task") or o.get("doing") or "", 60) + "\n")
            else:
                t.append(other[:24] + "…（索引の範囲外）\n", st(color=C_MUTED))
            ev = e.get("evidence")
            evs = ev if isinstance(ev, str) else ("一致行: " + " / ".join(ev.get("matched_lines", [])) if isinstance(ev, dict) else str(ev))
            t.append("    " + clip(evs, 100) + "\n", st(color=C_DIM))

    def timeline_text(self, t, tl):
        if not tl:
            t.append("(記録なし)\n", st(color=C_MUTED))
        for e in tl:
            kind = e.get("kind", "")
            col = {"依頼": C_BLUE, "返答": C_GREEN, "操作": C_MUTED}.get(kind, C_INK)
            t.append(f"{fmt_iso(e.get('t'))} ", st(color=C_DIM))
            t.append(f"{kind} ", st(color=col, bold=True))
            txt = e.get("text", "")
            if len(txt) > 300 and not self.expand_long:
                t.append(txt[:200].rstrip() + f"…（全文 {len(txt)}字、x で展開）\n", st(color=C_INK if kind != "操作" else C_MUTED))
            else:
                t.append(txt + "\n", st(color=C_INK if kind != "操作" else C_MUTED))

    # ------------------------------------------------------------ 操作 ----
    def select(self, nid, open_panel=False):
        self.sel = nid
        self.detail = None if self.detail_for != nid else self.detail
        if open_panel and not str(nid).startswith("frame:"):
            self.open_panel()
        elif open_panel and self.zoom == ZOOM_FAR:
            self.action_open_detail()
            return
        self.rebuild()
        self.query_one("#canvas", MapCanvas).show_rect(nid)
        if self.panel_open and nid in self.by_id:
            self.load_detail(nid)

    def open_panel(self):
        self.panel_open = True
        self.query_one("#panel").add_class("open")

    def close_panel(self):
        self.panel_open = False
        self.query_one("#panel").remove_class("open")

    def action_open_detail(self):
        if not self.sel:
            return
        if str(self.sel).startswith("frame:"):
            key = self.sel[6:]
            self.zoom = ZOOM_MID
            f = next((f for f in self.frames if f["key"] == key), None)
            first = (f["nodes"] or f["jobs"] or [None])[0] if f else None
            self.sel = first["id"] if first else None
            self.rebuild()
            if self.sel:
                self.query_one("#canvas", MapCanvas).show_rect(self.sel)
            return
        if self.panel_open and self.detail_for == self.sel:
            self.close_panel()
            self.rebuild()
            return
        self.open_panel()
        self.rebuild()
        self.load_detail(self.sel)

    def action_escape(self):
        inp = self.query_one("#search", Input)
        if inp.has_class("open"):
            inp.remove_class("open")
            self.filters["q"] = ""
            inp.value = ""
            self.query_one("#canvas").focus()
            self.rebuild()
        elif self.panel_open:
            self.close_panel()
            self.rebuild()
        elif self.sel:
            self.sel = None
            self.rebuild()

    def action_move(self, direction):
        canvas = self.query_one("#canvas", MapCanvas)
        ids = canvas.order
        if not ids:
            return
        if self.sel not in canvas.rects and self.sel not in canvas.frame_rects and not (self.sel or "").startswith("frame:"):
            self.select(ids[0])
            return
        rects = canvas.rects if self.zoom != ZOOM_FAR else {"frame:" + k: v for k, v in canvas.frame_rects.items()}
        cur = rects.get(self.sel)
        if not cur:
            self.select(ids[0])
            return
        cx, cy = cur[0] + cur[2] / 2, cur[1] + cur[3] / 2
        best = None
        for nid, (x, y, w, h) in rects.items():
            if nid == self.sel:
                continue
            ox, oy = x + w / 2, y + h / 2
            dx, dy = ox - cx, oy - cy
            if direction == "right" and dx <= 0 or direction == "left" and dx >= 0 or direction == "down" and dy <= 0 or direction == "up" and dy >= 0:
                continue
            if direction in ("left", "right"):
                score = abs(dx) + 3 * abs(dy) * 2   # 縦のズレを重く(同じ行を優先)
            else:
                score = abs(dy) * 2 + abs(dx) / 2
            if best is None or score < best[0]:
                best = (score, nid)
        if best:
            self.select(best[1])

    def action_next_frame(self, step):
        keys = [f["key"] for f in self.frames]
        if not keys:
            return
        cur_key = None
        if self.sel:
            if str(self.sel).startswith("frame:"):
                cur_key = self.sel[6:]
            else:
                cur_key = next((f["key"] for f in self.frames if any(it["id"] == self.sel for it in (f["nodes"] or f["jobs"] or []))), None)
        i = (keys.index(cur_key) + step) % len(keys) if cur_key in keys else 0
        f = self.frames[i]
        if self.zoom == ZOOM_FAR:
            self.select("frame:" + f["key"])
        else:
            first = (f["nodes"] or f["jobs"] or [None])[0]
            if first:
                self.select(first["id"])

    def action_jump(self, which, quiet=False):
        for f in self.frames:
            for n in f["nodes"]:
                if n["live"] and ((which == "turn" and needs_you(n)) or (which == "work" and color_kind(n) == "work")):
                    if self.zoom == ZOOM_FAR:
                        self.select("frame:" + f["key"])
                    else:
                        self.select(n["id"])
                    return True
        if not quiet:
            self.notify("該当するカードはありません", severity="warning")
        return False

    def set_model_filter(self, label):
        self.filters["model"] = "" if self.filters["model"] == label else label
        self.rebuild()

    def action_cycle_client(self):
        ids = []
        for n in self.nodes:
            if n.get("client") and n["client"]["id"] not in ids:
                ids.append(n["client"]["id"])
        opts = [""] + ids
        cur = self.filters["client"]
        self.filters["client"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else ""
        self.rebuild()

    def action_cycle_model(self):
        labels = []
        for n in self.nodes:
            lab = (n.get("model") or {}).get("label")
            if lab and lab not in labels:
                labels.append(lab)
        opts = [""] + labels
        cur = self.filters["model"]
        self.filters["model"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else ""
        self.rebuild()

    def action_toggle_past(self):
        self.filters["past"] = not self.filters["past"]
        self.rebuild()

    def action_toggle_unattended(self):
        self.filters["unattended"] = not self.filters["unattended"]
        self.rebuild()

    def action_toggle_expand(self):
        self.expand_long = not self.expand_long
        if self.panel_open:
            self.render_panel()

    def action_panel_scroll(self, d):
        p = self.query_one("#panel", VerticalScroll)
        p.scroll_page_down(animate=False) if d > 0 else p.scroll_page_up(animate=False)

    def action_relation(self, i):
        targets = getattr(self, "rel_targets", [])
        if not self.panel_open or i > len(targets):
            return
        target = targets[i - 1]
        if target not in self.by_id:
            self.notify("その相手は索引の範囲外です", severity="warning")
            return
        self.select(target, open_panel=True)

    def zoom_to(self, z):
        z = max(ZOOM_FAR, min(ZOOM_NEAR, z))
        if z == self.zoom:
            return
        old = self.zoom
        self.zoom = z
        if z == ZOOM_FAR and self.sel and not str(self.sel).startswith("frame:"):
            key = next((f["key"] for f in self.frames if any(it["id"] == self.sel for it in (f["nodes"] or f["jobs"] or []))), None)
            self.sel = "frame:" + key if key else None
        elif old == ZOOM_FAR and self.sel and str(self.sel).startswith("frame:"):
            f = next((f for f in self.frames if f["key"] == self.sel[6:]), None)
            first = (f["nodes"] or f["jobs"] or [None])[0] if f else None
            self.sel = first["id"] if first else None
        self.rebuild()
        if self.sel:
            self.query_one("#canvas", MapCanvas).show_rect(self.sel)

    def on_key(self, event):
        ch = event.character
        if self.query_one("#search", Input).has_focus:
            return
        if ch in ("+", "="):
            self.zoom_to(self.zoom + 1)
            event.stop()
        elif ch == "-":
            self.zoom_to(self.zoom - 1)
            event.stop()
        elif ch == "/":
            inp = self.query_one("#search", Input)
            inp.add_class("open")
            inp.focus()
            event.stop()

    def on_input_changed(self, event):
        self.filters["q"] = event.value.strip().lower()
        self.rebuild()

    def on_input_submitted(self, event):
        self.query_one("#search", Input).remove_class("open")
        self.query_one("#canvas").focus()
        self.rebuild()

    # ---- 副作用のある操作(g / r)。r は確認ダイアログを挟む ----
    def action_go_tab(self):
        n = self.by_id.get(self.sel) if self.sel else None
        if not n or not n["live"] or not n.get("tab"):
            self.notify("生きているタブのカードを選んでから g", severity="warning")
            return
        import cs
        try:
            cs.go(str(n["tab"]))
            self.notify(f"iTerm でタブ {n['tab']} を前面にしました")
        except SystemExit as e:
            self.notify(f"失敗: {e}", severity="error")

    def action_resume(self):
        n = self.by_id.get(self.sel) if self.sel else None
        if not n or n["live"] or n.get("role") == "subagent":
            self.notify("過去セッション(サブエージェント以外)のカードを選んでから r", severity="warning")
            return
        r = n["r"]
        cmd = "codex resume" if r.get("ai") == "Codex" else "claude --resume"
        msg = f"新しい iTerm タブで再開します:\n  cd {r.get('cwd') or '~'}\n  {cmd} {n['id']}"

        def done(ok):
            if not ok:
                self.notify("やめました")
                return
            import overview_server
            okk, err, c = overview_server.resume_in_iterm({"id": n["id"], "ai": r.get("ai"), "cwd": r.get("cwd")})
            self.notify(("開いた: " + c) if okk else ("失敗: " + err), severity="information" if okk else "error")
        self.push_screen(Confirm(msg), done)


# ================================================================ 入口 ====
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--index-child" in argv:
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else INDEX_DAYS
        live = json.loads(sys.stdin.read() or "[]")
        print(json.dumps(index_child(days, "--build" in argv, live), ensure_ascii=False))
        return
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else INDEX_DAYS
    build = False if "--no-build" in argv else True if "--build" in argv else None
    MapApp(days=days, build_index=build).run()


if __name__ == "__main__":
    main()
