#!/usr/bin/env python3
"""cs — iTerm の全タブで「いま何が動いているか」を1画面に出す。

なぜ要るか(2026-09-17):
  iTerm が 4ウィンドウ・38タブになり、どのタブが何をしているか分からなくなった。実測すると
    - 多くのタブは Claude が既に終わってただのシェルなのに、Claude が付けた題名が残っていた
    - 9タブは再起動後に `claude --resume` が「このフォルダを信頼しますか？」で止まったまま
      1.1GB を使い、題名は「claude」だけだった
  題名は「最後に誰かが書いた文字列」で、いまの状態ではない。状態はプロセスと
  ~/.claude/sessions/<pid>.json から取る。

使い方:
  cs              全タブの一覧(状態・メモリ・最終更新・フォルダ・いまの話題/最後の依頼)
  cs go 4-7       ウィンドウ4のタブ7を前面に出す
  cs dead         閉じてよい候補(終了済みのシェル・確認画面で停止)だけを出す
  cs close-dead   上の候補を確認のうえ閉じる(確認画面で停止しているものは「No, exit」で終了させてから)
  cs watch        3秒ごとに描き直す全画面(あなたの番 → 作業中 → 顧客/プロジェクト別 → macmini)。Ctrl-C で終了
  cs web          ブラウザ版(127.0.0.1 のみ)。未起動なら背景で起動してから開く
  cs map          Figma 風の全体地図 TUI(textual)。iTerm の中で動く。q で終了。--no-build で索引の再構築をしない

読むだけの部分(cs / cs dead / cs watch / cs web)は何も変更しない。
顧客の判定は ~/.claude/tools/clients.py の classify() に任せる(ここでは判定を書かない)。
"""
import ctypes
import ctypes.util
import glob
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

HOME = os.path.expanduser("~")
IS_MAC = sys.platform == "darwin"   # 盤サーバは Linux でも動く(iTerm の代わりに tmux、lsof の代わりに /proc)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import aiboard_paths  # noqa: E402
try:
    import clients as _clients   # ~/.claude/tools/clients.py(顧客判定の唯一の場所)
except ImportError:
    _clients = None
try:
    import models as _models     # ~/.claude/tools/models.py(モデルの表示名・絵文字・色の唯一の場所)
except ImportError:
    _models = None


def model_style(model_id):
    """models.style() を借りる。無ければ最低限の dict。"""
    if _models:
        return _models.style(model_id)
    return {"label": short_model(model_id) or "モデル不明", "short": short_model(model_id), "vendor": "",
            "emoji": "❔", "rgb": [120, 120, 120], "id": model_id or ""}


def ai_label(t, color=True):
    """AI列の表示: 「🟠 Opus 5」(+アカウント)。color=True なら 24bit 色。"""
    st = t.get("model_style") or model_style(t.get("model_id", ""))
    label = f"{st['emoji']} {st['label']}"
    if t.get("account"):
        label += f"({t['account']})"
    rgb = st.get("rgb")
    if color and rgb and not os.environ.get("NO_COLOR"):
        r, g, b = rgb
        return f"\033[38;2;{r};{g};{b}m{label}\033[0m"
    return label
# 別アカウント(~/.claude-profiles/<名前>)で動く Claude は、セッション記録もそちらに置かれる
SESS_DIRS = [os.path.join(HOME, ".claude", "sessions")] + glob.glob(
    os.path.join(HOME, ".claude-profiles", "*", "sessions"))
PROJECT_ROOTS = [os.path.join(HOME, ".claude", "projects")] + glob.glob(
    os.path.join(HOME, ".claude-profiles", "*", "projects"))


# ---------------------------------------------------------------- 取得 ----
OSA_ERROR = ""   # 直前の問い合わせが失敗した理由(盤に出すため。空なら正常)


def osa(script, timeout=8):
    """iTerm への問い合わせ。返事が無ければあきらめる。

    iTerm が固まっていると AppleEvent は 2 分近く返らない。以前はここで待ち続け、
    しかも失敗すると sys.exit していたので、盤サーバごと巻き添えで止まっていた(2026-09-18 実測)。
    """
    global OSA_ERROR
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        OSA_ERROR = f"timeout:{timeout}"
        return ""
    except OSError as e:
        OSA_ERROR = f"nocmd:{e}"
        return ""
    if r.returncode != 0:
        OSA_ERROR = "error:" + r.stderr.strip()[:160]
        return ""
    OSA_ERROR = ""
    return r.stdout


def on_tty(tty, body, pre=""):
    """tty が一致する iTerm セッションを探して body(AppleScript。w=ウィンドウ t=タブ s=セッション)を実行する。

    「ウィンドウ番号-タブ番号」で指すと、番号はウィンドウの重なり順なので、別のウィンドウを触っただけで
    違うタブに当たる(2026-09-17 実地試験で発覚)。tty はセッションに固有で、位置が変わっても同じものを指す。
    返り値 (ok, 出力)。見つからなければ (False, "NOTFOUND")。
    """
    tty = "/dev/" + str(tty).replace("/dev/", "")
    if not re.fullmatch(r"/dev/ttys\d+", tty):
        return False, "tty の形が違う"
    script = f'''{pre}
    tell application "iTerm2"
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            if (tty of s) is "{tty}" then
              {body}
            end if
          end repeat
        end repeat
      end repeat
      return "NOTFOUND"
    end tell'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return False, r.stderr.strip()[:300]
    out = r.stdout.rstrip("\n")
    return out != "NOTFOUND", out


def tty_of(target, sessions=None):
    """"<win>-<tab>" → tty。sessions(盤の snapshot)があればそこから、無ければ今の iTerm から引く。"""
    m = re.fullmatch(r"(\d+)-(\d+)", str(target))
    if not m:
        return ""
    if sessions is not None:
        return next((x.get("tty", "") for x in sessions if x.get("tab") == target), "")
    return next((x["tty"] for x in iterm_sessions() if (x["win"], x["tab"]) == (int(m.group(1)), int(m.group(2)))), "")


def go_tty(tty):
    return on_tty(tty, 'select w\n tell w to select t\n tell t to select s\n activate\n return "OK"')


_LAST_ITERM = {"rows": [], "t": 0, "fail_t": 0}


_TMUX = {"t": 0, "rows": []}


def tmux_panes(ttl=4.0):
    """tmux の窓を、iTerm のタブと同じ形で返す(win=8、tab=通し番号)。Linux ではこれが端末の出どころ。

    mac でも返す: tmux の中で動いている claude は、パネル自身の tty に居るので iTerm の一覧には出てこない。
    tmux が無い・動いていなければ空。1 回 13ms ほどかかるので、少しの間は使い回す(更新 1 回で何度も呼ばれる)。
    送信は tmux send-keys(overview_server 側)。
    """
    if time.time() - _TMUX["t"] < ttl:
        return list(_TMUX["rows"])
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        _TMUX.update(t=time.time(), rows=[])
        return []
    if r.returncode != 0:
        _TMUX.update(t=time.time(), rows=[])
        return []
    rows = []
    for i, line in enumerate(r.stdout.splitlines(), 1):
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("/dev/"):
            rows.append({"win": 8, "tab": i, "tty": parts[0].replace("/dev/", ""),
                         "title": parts[2] if len(parts) > 2 else "", "tmux": parts[1]})
    _TMUX.update(t=time.time(), rows=list(rows))
    return rows


def tabs_dirty():
    """タブの顔ぶれが変わったはずの時に呼ぶ(送信・停止・端末を開いた後)。次の問い合わせで取り直す。"""
    _LAST_ITERM["at"] = 0


def iterm_sessions(ttl=8.0):
    """iTerm のタブ一覧。**8 秒は使い回す**(問い合わせに 285ms かかり、更新 1 回の半分を占めていた)。

    タブの顔ぶれと題名だけがこの遅れの対象で、状態(hook・記録・プロセス)は毎回取り直す。
    送信・停止の直後は tabs_dirty() で取り直すので、操作した結果は待たされない。
    """
    if not IS_MAC:
        return _with_tmux(app_panes())          # iTerm は mac だけ。他の OS では tmux
    if _LAST_ITERM.get("cached") is not None and time.time() - _LAST_ITERM.get("at", 0) < ttl:
        rows = [dict(r) for r in _LAST_ITERM["cached"]]
    else:
        rows = _iterm_rows_now()
        _LAST_ITERM.update(cached=[dict(r) for r in rows], at=time.time())
    # アプリ自身の端末は毎回読む(ファイル 1 つ)。使い回しに混ぜると、開いたばかりの端末が 8 秒出てこない
    return _with_tmux(rows + app_panes())


def _iterm_rows_now():
    """iTerm のタブだけを問い合わせる(アプリの端末・tmux は呼ぶ側で足す)。"""
    # iTerm の tell ブロック内では `tab` がタブ文字でなく「タブ」オブジェクトになるので、区切りは外で作る
    script = '''
    set TB to ASCII character 9
    tell application "iTerm2"
      set out to ""
      set wi to 0
      repeat with w in windows
        set wi to wi + 1
        set ti to 0
        repeat with t in tabs of w
          set ti to ti + 1
          set s to current session of t
          set out to out & wi & TB & ti & TB & (tty of s) & TB & (name of s) & linefeed
        end repeat
      end repeat
      return out
    end tell'''
    rows = []
    # 失敗した直後は間を空ける(固まった iTerm に 2.5 秒ごとに 8 秒待たされると、盤の更新が止まる)
    if _LAST_ITERM["fail_t"] and time.time() - _LAST_ITERM["fail_t"] < 30:
        return list(_LAST_ITERM["rows"])   # 問い合わせに失敗: 前の一覧を使う(アプリの端末は呼ぶ側が足す)
    out = osa(script)
    if not out and OSA_ERROR:
        _LAST_ITERM["fail_t"] = time.time()
        return list(_LAST_ITERM["rows"])   # 問い合わせに失敗: iTerm は前回の一覧(アプリの端末は呼ぶ側が足す)
    _LAST_ITERM["fail_t"] = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append({"win": int(parts[0]), "tab": int(parts[1]),
                         "tty": parts[2].replace("/dev/", ""), "title": parts[3]})
    _LAST_ITERM.update(rows=list(rows), t=time.time())
    return rows


def _with_tmux(rows):
    """tmux のパネルを足す。同じ tty のものは足さない(iTerm のタブが tmux のクライアントを持っている時)。"""
    have = {r.get("tty") for r in rows}
    return rows + [p for p in tmux_panes() if p["tty"] not in have]


APP_PANES = aiboard_paths.data("app_panes.json")


def app_panes():
    """AIBoard.app が持っている端末を、iTerm のタブと同じ形で返す(win=0、tab=端末番号)。

    アプリが app_panes.json に {app_pid, panes:[{pane, tty, ...}]} を書く。アプリが死んでいれば無視する
    (古い一覧が残っていても、tty でプロセスを引く先が無いだけで害は無いが、数えないのが正しい)。
    """
    try:
        with open(APP_PANES, encoding="utf-8") as f:
            d = json.load(f)
        os.kill(int(d["app_pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [{"win": 0, "tab": int(p["pane"]), "tty": str(p["tty"]).replace("/dev/", ""),
             "title": str(p.get("title", "")), "app": True, "deleg": str(p.get("deleg") or "")}
            for p in d.get("panes", []) if p.get("tty")]


def tty_idle(tty):
    """その端末に最後に文字が出てから何秒経ったか。端末のデバイスの更新時刻を見るだけ(実測で出力に追従する)。

    状態の記録を持たない CLI(Gemini / Grok / Cursor)でも「動いているのか、こちらを待っているのか」を
    これで見分ける。分からなければ None。
    """
    t = str(tty or "").replace("/dev/", "")
    if not re.fullmatch(r"ttys\d+", t):
        return None
    try:
        return max(0.0, time.time() - os.stat("/dev/" + t).st_mtime)
    except OSError:
        return None


def app_notify_auth():
    """アプリが記録した通知の許可の状態(authorized / denied / notDetermined …)。アプリが動いていなければ空。

    止められていると「あなたを待っている」を知らせる手段が無くなるのに、これまで誰も気づけなかった
    (この Mac も denied だった。2026-09-18 実測)。
    """
    try:
        with open(APP_PANES, encoding="utf-8") as f:
            d = json.load(f)
        os.kill(int(d["app_pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return str(d.get("notify_auth") or "")


# プロセス表は 2.5 秒ごとに作り直すので、ここが盤サーバの一番の重さだった。
# `ps -axo` はこの Mac で 763 件・**180〜380ms**(実測 2026-09-20)。中身は同じものが
# libproc(proc_listpids + proc_pidinfo)なら **1.6〜2.9ms** で取れる。
# コマンド行と RSS は要る時にだけ引く(端末に居るものだけで足りることが多い)。
#   照合済み: 共通 547 件で ppid・tty は 1 件も食い違わない。
#   取れないのは他人(root 等)のプロセス 218 件で、自分のものは 0 件だった。
#   コマンド行はむしろ ps より正確(引数に改行が入ると ps の出力は行が割れる)。
# うまく行かない時のために AIBOARD_PS=1 で昔の ps に戻せる。
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except Exception:      # ctypes が使えない環境(考えにくいが)では ps に落ちる
    _libc = None
_PROC_ALL_PIDS, _PROC_PIDTBSDINFO, _PROC_PIDTASKINFO = 1, 3, 4
_CTL_KERN, _KERN_PROCARGS2, _KERN_ARGMAX = 1, 49, 8


class _BsdInfo(ctypes.Structure):
    _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32), ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]


class _TaskInfo(ctypes.Structure):
    _fields_ = [("pti_virtual_size", ctypes.c_uint64), ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64), ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64), ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32), ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32), ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32), ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32), ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32), ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32), ("pti_priority", ctypes.c_int32)]


class _VinfoStat(ctypes.Structure):
    _fields_ = [("vst_dev", ctypes.c_uint32), ("vst_mode", ctypes.c_uint16), ("vst_nlink", ctypes.c_uint16),
                ("vst_ino", ctypes.c_uint64), ("vst_uid", ctypes.c_uint32), ("vst_gid", ctypes.c_uint32),
                ("vst_atime", ctypes.c_int64), ("vst_atimensec", ctypes.c_int64),
                ("vst_mtime", ctypes.c_int64), ("vst_mtimensec", ctypes.c_int64),
                ("vst_ctime", ctypes.c_int64), ("vst_ctimensec", ctypes.c_int64),
                ("vst_birthtime", ctypes.c_int64), ("vst_birthtimensec", ctypes.c_int64),
                ("vst_size", ctypes.c_int64), ("vst_blocks", ctypes.c_int64),
                ("vst_blksize", ctypes.c_int32), ("vst_flags", ctypes.c_uint32),
                ("vst_gen", ctypes.c_uint32), ("vst_rdev", ctypes.c_uint32), ("vst_qspare", ctypes.c_int64 * 2)]


class _VnodeInfo(ctypes.Structure):
    _fields_ = [("vi_stat", _VinfoStat), ("vi_type", ctypes.c_int), ("vi_pad", ctypes.c_int),
                ("vi_fsid", ctypes.c_int32 * 2)]


class _VnodeInfoPath(ctypes.Structure):
    _fields_ = [("vip_vi", _VnodeInfo), ("vip_path", ctypes.c_char * 1024)]


class _VnodePathInfo(ctypes.Structure):
    _fields_ = [("pvi_cdir", _VnodeInfoPath), ("pvi_rdir", _VnodeInfoPath)]


class _ProcFdInfo(ctypes.Structure):
    _fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]


class _FileInfo(ctypes.Structure):
    _fields_ = [("fi_openflags", ctypes.c_uint32), ("fi_status", ctypes.c_uint32),
                ("fi_offset", ctypes.c_int64), ("fi_type", ctypes.c_int32), ("fi_guardflags", ctypes.c_uint32)]


class _VnodeFdInfoWithPath(ctypes.Structure):
    _fields_ = [("pfi", _FileInfo), ("pvip", _VnodeInfoPath)]


_PROC_PIDVNODEPATHINFO, _PROC_PIDLISTFDS, _PROC_PIDFDVNODEPATHINFO, _PROX_FDTYPE_VNODE = 9, 1, 2, 1
_ARGMAX = [0]


def _proc_rss(pid):
    t = _TaskInfo()
    if _libc.proc_pidinfo(pid, _PROC_PIDTASKINFO, ctypes.c_uint64(0), ctypes.byref(t), ctypes.sizeof(t)) != ctypes.sizeof(t):
        return 0
    return t.pti_resident_size // 1024          # ps と同じ KB 単位


def _proc_cmd(pid):
    if not _ARGMAX[0]:
        v, sz = ctypes.c_int(0), ctypes.c_size_t(4)
        mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
        _libc.sysctl(mib, 2, ctypes.byref(v), ctypes.byref(sz), None, 0)
        _ARGMAX[0] = v.value or 262144
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(_ARGMAX[0])
    buf = ctypes.create_string_buffer(_ARGMAX[0])
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return ""
    raw = buf.raw[:size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder)
    parts = raw[4:].split(b"\0")
    i = 0
    while i < len(parts) and not parts[i]:
        i += 1
    i += 1                                       # 実行ファイルのパス
    while i < len(parts) and not parts[i]:       # 詰め物
        i += 1
    args = []
    while i < len(parts) and len(args) < argc:
        args.append(parts[i])
        i += 1
    return b" ".join(args).decode("utf-8", "replace")


class _Proc(dict):
    """1 プロセス分。cmd と rss は初めて見た時に引く(全部引くと 547 件で 80ms かかる)。"""

    def __init__(self, pid, ppid, tty):
        super().__init__(ppid=ppid, tty=tty)
        self.pid = pid

    def _fill(self, k):
        v = _proc_cmd(self.pid) if k == "cmd" else _proc_rss(self.pid)
        self[k] = v
        return v

    def __missing__(self, k):
        if k in ("cmd", "rss"):
            return self._fill(k)
        raise KeyError(k)

    def get(self, k, default=None):     # .get("cmd") でも引く(既定値で黙って空を返さない)
        if k in ("cmd", "rss") and k not in self:
            return self._fill(k)
        return super().get(k, default)


def _tty_name(tdev):
    if tdev in (0, 0xFFFFFFFF):
        return "??"
    return f"ttys{tdev & 0xFFFFFF:03d}" if (tdev >> 24) & 0xFF == 16 else "??"


def _processes_ps():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,tty=,command="],
                         capture_output=True, text=True).stdout
    procs = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)", line)
        if m:
            procs[int(m.group(1))] = {"ppid": int(m.group(2)), "rss": int(m.group(3)),
                                      "tty": m.group(4), "cmd": m.group(5)}
    return procs


def processes():
    if _libc is None or os.environ.get("AIBOARD_PS") == "1" or not IS_MAC:
        return _processes_ps()
    try:
        n = _libc.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
        buf = (ctypes.c_int32 * (n // 4 + 128))()
        got = _libc.proc_listpids(_PROC_ALL_PIDS, 0, ctypes.byref(buf), ctypes.sizeof(buf))
        procs = {}
        info = _BsdInfo()
        for pid in buf[:max(0, got // 4)]:
            if pid <= 0:
                continue
            if _libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0),
                                  ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                continue                         # 他人のプロセス(自分のものは全部取れる)
            procs[pid] = _Proc(pid, info.pbi_ppid, _tty_name(info.e_tdev))
        if len(procs) < 20:                      # 何かおかしい: 昔の道に戻る
            return _processes_ps()
        return procs
    except Exception:
        return _processes_ps()


def descendants_rss(procs, pid):
    kids = {}
    for p, v in procs.items():
        kids.setdefault(v["ppid"], []).append(p)
    total, stack, seen = 0, [pid], set()   # ps の取得中に親子が循環して見えることがある(そこで止まると盤が固まる)
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        total += procs.get(x, {}).get("rss", 0)
        stack.extend(kids.get(x, []))
    return total


def _argv0_is(cmd, name):
    """そのコマンド行が「その道具を動かしている」か。引数に名前が出るだけのもの(vim ~/logs/codex)は数えない。

    以前は行のどこかに /codex があれば Codex と見なしていて、`tail -f ~/logs/codex` まで
    セッション扱いになっていた(2026-09-18 実測)。
    """
    toks = cmd.split()
    if not toks:
        return False
    if os.path.basename(toks[0]) == name:
        return True
    # node/bun/npx 経由(node …/bin/codex exec)は 2 つ目までを見る
    if os.path.basename(toks[0]) in ("node", "bun", "npx", "deno") and len(toks) > 1:
        return os.path.basename(toks[1]) == name
    return False


def outermost_ai(procs, pids):
    """同じ tty に AI が入れ子で居るとき、他の AI の子孫でない「外側」を 1 つ選ぶ。

    以前は min(pid) で選んでいたので、pid が一周した後に起きた入れ子の `claude -p` を
    本体と取り違えることがあった(メモリ・状態・終了の宛先が全部ずれる。2026-09-18 実測)。
    """
    s = set(pids)
    outer = []
    for p in s:
        up, seen = procs.get(p, {}).get("ppid"), set()
        while up in procs and up not in seen:
            seen.add(up)
            if up in s:
                break
            up = procs[up]["ppid"]
        else:
            outer.append(p)
    return min(outer) if outer else min(s)


def is_claude(cmd):
    return _argv0_is(cmd, "claude")


def is_codex(cmd):
    return _argv0_is(cmd, "codex") or "@openai/codex" in cmd.split(" ")[0]


OTHER_AI = [("Gemini", "gemini"), ("Grok", "grok"), ("Cursor", "cursor-agent")]


def other_ai(cmd):
    """Claude / Codex 以外の AI の CLI。名前だけは分かるが、状態を知る口は無い。"""
    for label, name in OTHER_AI:
        if _argv0_is(cmd, name):
            return label
    return ""


def session_record(pid):
    for d in SESS_DIRS:
        try:
            rec = json.load(open(os.path.join(d, f"{pid}.json")))
        except (OSError, ValueError):
            continue
        m = re.search(r"\.claude-profiles/([^/]+)/", d + "/")
        rec["_account"] = m.group(1) if m else ""
        return rec
    return None


def find_transcript(session_id):
    for root in PROJECT_ROOTS:
        hits = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
        if hits:
            return hits[0]
    return None


def prompt_text(d):
    """トランスクリプトの1レコード(dict)が「人が打った依頼」ならその本文、違えば ""。
    ツール結果・system 注入(< で始まる)・isMeta・サブエージェント側(isSidechain)は除く。
    依頼の判定はここ1か所(overview / 索引 もこれを使う)。"""
    if not isinstance(d, dict) or d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return ""
    m = d.get("message")
    if not isinstance(m, dict):   # 壊れた行(message が文字列/None)で索引ごと落ちない(2026-09-18 実測)
        return ""
    c = m.get("content")
    text = c if isinstance(c, str) else "".join(
        b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
    text = " ".join(text.split())
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return text


_FILE_MEMO = {}


def memo_by_file(fn):
    """第1引数のファイルが変わっていなければ(サイズと更新時刻が同じなら)前回の結果を返す。

    盤は数秒おきに全タブを見に来る。そのたびに各 transcript の末尾数MBを読み直すと、
    常駐プロセスのメモリが1回あたり数十MBずつ増えて戻らない(2026-09-17 実測: 8回で 536MB)。
    """
    def wrap(path, *a, **k):
        try:
            st = os.stat(path)
        except (OSError, TypeError):
            return fn(path, *a, **k)
        slot = _FILE_MEMO.setdefault((fn.__name__, path, a, tuple(sorted(k.items()))), {})
        if slot.get("key") != (st.st_size, st.st_mtime_ns):
            slot.update(key=(st.st_size, st.st_mtime_ns), val=fn(path, *a, **k))
        return slot["val"]
    wrap.__name__, wrap.__doc__ = fn.__name__, fn.__doc__
    return wrap


def user_prompts_in(chunk):
    """transcript の文字列から、人が打った依頼を (timestamp, 本文) で順に返す。"""
    for line in chunk.splitlines():
        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        text = prompt_text(d)
        if text:
            yield d.get("timestamp", ""), text


def iter_user_prompts(path, tail_bytes=1_500_000):
    """トランスクリプト末尾 tail_bytes から、人が打った依頼を (timestamp, 本文) で順に返す。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return
    yield from user_prompts_in(chunk)


@memo_by_file
def last_user_prompt(path, tail_bytes=1_500_000):
    """トランスクリプト末尾から、人が打った最後の依頼を取る(ツール結果や system 注入は除く)。"""
    last = ""
    for _, text in iter_user_prompts(path, tail_bytes):
        last = text
    return last


TABSTATE_DIR = os.path.join(HOME, ".claude", "tabstate")
CODEX_SESS = os.path.join(HOME, ".codex", "sessions")


def tab_state(session_id):
    """~/.claude/hooks/tab-status.py が書く「いま何をしているか」。"""
    try:
        return json.load(open(os.path.join(TABSTATE_DIR, f"{session_id}.json")))
    except (OSError, ValueError):
        return {}


def short_model(model):
    m = (model or "").replace("claude-", "")
    m = re.sub(r"-\d{8}$", "", m)
    return re.sub(r"^(\w+)-(\d+)-(\d+)$", r"\1-\2.\3", m)


def proc_start(pid):
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    try:
        return time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        pass
    if not IS_MAC:      # Linux: /proc の作成時刻(ps の書式が違うため)
        try:
            return os.stat(f"/proc/{int(pid)}").st_ctime
        except (OSError, ValueError):
            return None
    return None


def codex_rollout_path(cwd, started):
    """codex のプロセスに対応する記録(rollout)のパスを、作業フォルダと開始時刻の近さで選ぶ。"""
    files = sorted(glob.glob(os.path.join(CODEX_SESS, "*", "*", "*", "rollout-*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:40]
    best, best_gap = None, None
    for f in files:
        m = re.search(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})", f)
        try:
            with open(f, errors="replace") as fh:
                meta = json.loads(fh.readline()).get("payload", {})
        except (OSError, ValueError):
            continue
        if cwd and meta.get("cwd") != cwd:
            continue
        t0 = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S")) if m else os.path.getmtime(f)
        gap = abs(t0 - started) if started else 0
        if best is None or gap < best_gap:
            best, best_gap = f, gap
    return best


def _open_files_proc(pids):
    """Linux: /proc/<pid>/fd から開いているファイルを読む(lsof の代わり)。"""
    out = []
    for p in pids:
        d = f"/proc/{p}/fd"
        try:
            for fd in os.listdir(d):
                try:
                    out.append(os.readlink(os.path.join(d, fd)))
                except OSError:
                    pass
        except OSError:
            pass
    return out


def _open_files_libproc(pids):
    """そのプロセスが開いているファイルの名前。lsof の代わり(同じ答えで桁違いに速い)。使えなければ None。"""
    if _libc is None or os.environ.get("AIBOARD_PS") == "1":
        return None
    try:
        names = []
        for pid in pids:
            n = _libc.proc_pidinfo(int(pid), _PROC_PIDLISTFDS, ctypes.c_uint64(0), None, 0)
            if n <= 0:
                continue
            cnt = n // ctypes.sizeof(_ProcFdInfo) + 16
            buf = (_ProcFdInfo * cnt)()
            got = _libc.proc_pidinfo(int(pid), _PROC_PIDLISTFDS, ctypes.c_uint64(0), ctypes.byref(buf), ctypes.sizeof(buf))
            for fd in buf[:max(0, got // ctypes.sizeof(_ProcFdInfo))]:
                if fd.proc_fdtype != _PROX_FDTYPE_VNODE:
                    continue
                vi = _VnodeFdInfoWithPath()
                if _libc.proc_pidfdinfo(int(pid), fd.proc_fd, _PROC_PIDFDVNODEPATHINFO,
                                        ctypes.byref(vi), ctypes.sizeof(vi)) == ctypes.sizeof(vi):
                    p = vi.pvip.vip_path.decode("utf-8", "replace")
                    if p:
                        names.append(p)
        return names or None
    except Exception:
        return None


def codex_rollout_by_lsof(pids):
    """codex のプロセスが実際に開いている記録(rollout)を返す。推定でなく確定。

    `codex resume` は記録の日付が過去で、フォルダと開始時刻の近さでは別のセッション
    (同じフォルダで走った codex exec など)を拾う(2026-09-17 実例)。開いているファイルを正とする。
    """
    if not pids:
        return None
    if not IS_MAC:
        names = _open_files_proc(pids)
    else:
        names = _open_files_libproc(pids)
        if names is None:        # 使えなければ従来どおり lsof(遅いが確実)
            r = subprocess.run(["lsof", "-p", ",".join(str(p) for p in pids), "-Fn"], capture_output=True, text=True)
            names = [l[1:] for l in r.stdout.splitlines() if l.startswith("n")]
    hits = [n for n in names if re.search(r"/rollout-[^/]+\.jsonl$", n)]
    return max(hits, key=os.path.getmtime) if hits else None


def codex_session(cwd, started, pids=()):
    """codex のプロセスに対応する記録(rollout)を読み、モデル・いまの操作・依頼を返す。"""
    best = codex_rollout_by_lsof(pids) or codex_rollout_path(cwd, started)
    if not best:
        return {}
    return dict(_codex_read(best))


@memo_by_file
def _codex_read(best):
    m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]+)\.jsonl$", best)
    info = {"model": "", "doing": "", "task": "", "updated": os.path.getmtime(best), "rollout": best,
            "sid": m.group(1) if m else os.path.basename(best)}
    with open(best, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            pl = d.get("payload") or {}
            if d.get("type") == "event_msg" and pl.get("type") in ("task_started", "task_complete", "turn_aborted"):
                # ターンの区切り。これが Codex の状態の正(hook が無いぶん、記録の区切り行で判定する)
                info["turn"] = pl["type"]
                info["error"] = " ".join(str((pl.get("error") or {}).get("message") or "").split())
            if d.get("type") == "turn_context" and pl.get("model"):
                info["model"] = pl["model"]
            elif d.get("type") == "response_item" and pl.get("type") in ("function_call", "custom_tool_call"):
                arg = str(pl.get("arguments") or pl.get("input") or "")
                cmd = re.search(r'cmd["\']?\s*[:=]\s*["\']([^"\']+)', arg)
                info["doing"] = f"{pl.get('name', '操作')}: {' '.join((cmd.group(1) if cmd else arg).split())}"
            elif d.get("type") == "response_item" and pl.get("type") == "message":
                if pl.get("role") == "user":
                    txt = "".join(x.get("text", "") for x in (pl.get("content") or []) if isinstance(x, dict))
                    if txt and not txt.lstrip().startswith("<"):
                        info["task"] = " ".join(txt.split())
                        info["doing"] = "考え中"
                elif pl.get("role") == "assistant":
                    info["doing"] = "✅ 返答済み（あなたの番）"
    if info.get("error"):
        info["doing"] = "⛔ エラーで停止: " + info["error"]
    elif info.get("turn") == "turn_aborted":
        info["doing"] = "⏹ 中断された"
    elif info.get("turn") == "task_complete" and not info["doing"].startswith("✅"):
        info["doing"] = "✅ 返答済み（あなたの番）"
    return info


@memo_by_file
def model_from_transcript(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 300_000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()):
        m = re.search(r'"model"\s*:\s*"(claude-[^"]+)"', line)
        if m:
            return m.group(1)
    return ""


def proc_cwd(pid):
    if not IS_MAC:      # Linux: lsof を入れなくても読める
        try:
            return os.readlink(f"/proc/{int(pid)}/cwd")
        except (OSError, ValueError):
            return ""
    # lsof は 1 プロセスあたり約 28ms かかり、更新のたびに何回も呼ぶので盤サーバの重さの半分だった
    # (2026-09-20 実測: classify 372ms のうち 250ms)。libproc なら 0.01ms で、同じ答えが返る(8 件で照合)
    if _libc is not None and os.environ.get("AIBOARD_PS") != "1":
        try:
            v = _VnodePathInfo()
            if _libc.proc_pidinfo(int(pid), _PROC_PIDVNODEPATHINFO, ctypes.c_uint64(0),
                                  ctypes.byref(v), ctypes.sizeof(v)) == ctypes.sizeof(v):
                return v.pvi_cdir.vip_path.decode("utf-8", "replace")
        except Exception:
            pass
    r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def tmux_capture(tty, lines=200):
    """tmux のパネルの画面を読む(Linux の端末はこれ)。読むだけ。"""
    target = next((p.get("tmux") for p in tmux_panes() if p["tty"] == tty), None)
    if not target:
        return ""
    try:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", f"-{int(lines)}"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def screen_text(win, tab, tty=None):
    tty = tty or tty_of(f"{win}-{tab}")
    if win == 8 or not IS_MAC:          # tmux のパネル(mac でも 8-N は tmux)
        return tmux_capture(tty)
    ok, out = on_tty(tty, "return contents of s")
    return out if ok else ""


_TRUST = {"t": 0, "paths": set()}


def trust_files():
    """claude が「このフォルダを信頼しますか」の答えを書く設定ファイル(既定と各プロファイル)。"""
    out = [os.path.join(HOME, ".claude.json")]
    prof = os.path.join(HOME, ".claude-profiles")
    if os.path.isdir(prof):
        out += [os.path.join(prof, d, ".claude.json") for d in sorted(os.listdir(prof))]
    return [p for p in out if os.path.exists(p)]


def trusted_cwd(cwd, ttl=20):
    """そのフォルダは、どれかのアカウントで「信頼する」と答え済みか。

    答える前の claude は最初の画面で止まり、会話を始めないので、盤では「起動中?」に見えていた
    (アプリ自身の端末は AppleScript で画面を読めないので、画面文字では判定できない。2026-09-18)。
    """
    if not cwd:
        return True
    now = time.time()
    if now - _TRUST["t"] > ttl:
        paths = set()
        for f in trust_files():
            try:
                with open(f, encoding="utf-8") as h:
                    d = json.load(h)
            except (OSError, ValueError):
                continue
            for p, v in (d.get("projects") or {}).items():
                if isinstance(v, dict) and v.get("hasTrustDialogAccepted"):
                    paths.add(p.rstrip("/") or "/")
        _TRUST.update(t=now, paths=paths)
    return cwd.rstrip("/") in _TRUST["paths"]


# ---------------------------------------------------------------- 判定 ----
# 記録を持たない CLI の見分け方: 出力が OTHER_AI_BUSY 秒以内なら作業中、OTHER_AI_TURN 秒以上止まったらこちらの番
OTHER_AI_BUSY = 5
OTHER_AI_TURN = 30


def fmt_idle(sec):
    return f"{int(sec)}秒" if sec < 60 else f"{sec / 60:.0f}分"


def classify(tabs, procs):
    by_tty = {}
    for pid, v in procs.items():
        by_tty.setdefault(v["tty"], []).append(pid)
    now = time.time()
    for t in tabs:
        pids = by_tty.get(t["tty"], [])
        claude = [p for p in pids if is_claude(procs[p]["cmd"])]
        codex = [p for p in pids if is_codex(procs[p]["cmd"])]
        t.update(state="終了(古い題名)", mark="⚪", mem=0, ago=None, cwd="", topic="", pid=None,
                 ai="", doing="", sid="", task="", model="", model_id="", model_style=None,
                 account="", transcript="", state_since=None, started=None, client=None, subagents={},
                 trust_ask="", idle=None)
        topic = re.sub(r"^[✳◐◑◒◓⠂⠐·\s]+", "", t["title"])
        topic = re.sub(r"\s*\([^)]*\)\s*$", "", topic)
        topic = re.sub(r"^\[ [.!] \] Action Required \| ", "", topic)
        t["topic"] = topic
        t["title_topic"] = topic
        if claude:
            pid = outermost_ai(procs, claude)   # 入れ子の一時実行(claude -p)でなく、外側の本体を選ぶ
            t["pid"] = pid
            t["mem"] = descendants_rss(procs, pid)
            rec = session_record(pid)
            if rec:
                t["cwd"] = rec.get("cwd", "")
                upd = rec.get("updatedAt") or rec.get("statusUpdatedAt")
                t["ago"] = (now - upd / 1000) if upd else None
                busy = rec.get("status") not in (None, "idle")
                t["state"], t["mark"] = ("作業中", "🟢") if busy else ("返答待ち", "🟡")
                # その状態になってからの時間(status が変わった時刻)と、セッションの開始時刻
                su = rec.get("statusUpdatedAt")
                t["state_since"] = su / 1000 if su else None
                t["started"] = (rec.get("startedAt") or 0) / 1000 or None
                t["sid"] = rec.get("sessionId", "")
                st = tab_state(rec.get("sessionId", ""))
                tr = find_transcript(rec.get("sessionId", ""))
                t["transcript"] = tr or ""
                model = st.get("model") or (model_from_transcript(tr) if tr else "")
                acct = rec.get("_account", "")
                t["model"], t["account"], t["model_id"] = short_model(model), acct, model
                t["model_style"] = model_style(model)
                t["ai"] = "Claude " + short_model(model) + (f"({acct})" if acct else "")
                t["doing"] = st.get("doing") or ("考え中" if busy else "✅ 返答済み（あなたの番）")
                # 状態の正は ~/.claude/sessions の status。作業中なのに記録が「返答済み」のままなのは
                # 依頼を受けた通知がフックに届いていない(フック導入前から続くセッション等)だけなので、そう表示する
                if busy and t["doing"].startswith("✅"):
                    t["doing"] = "考え中（直前の操作は未記録）"
                # フックの mark: ⚠=承認・入力待ち(permission_prompt 等) / 💬=返答済み(あなたの番) / ⏳=作業中
                mk = st.get("mark")
                # doing が ⚠ のままなら承認待ち(PostToolUse 等で mark だけ ⏳ に戻ることがある)
                if mk == "⚠" or st.get("doing", "").startswith("⚠"):
                    t["state"], t["mark"] = "確認待ち", "🔴"
                elif mk == "💬" and not busy:
                    t["state"], t["mark"] = "返答待ち", "🟡"
                if st.get("topic"):
                    topic = st["topic"]
                    t["title_topic"] = topic
                t["subagents"] = st.get("subagents") or {}
                prompt = st.get("task") or ""
                if not prompt or prompt.startswith("<"):   # フックが拾った system 注入(<task-notification> 等)は依頼でない
                    prompt = (last_user_prompt(tr) if tr else "") or ""
                t["task"] = prompt
                if prompt:
                    t["topic"] = f"{topic} ／ 依頼: {prompt}" if topic and topic != "claude" else f"依頼: {prompt}"
                # 顧客判定: tabstate に client があればそれを優先、無ければ clients.classify()
                t["client"] = st.get("client") or (_clients and _clients.classify(
                    cwd=t["cwd"], account=acct,
                    texts=(prompt, topic, first_user_prompt(tr) if tr else "")))
            else:
                t["cwd"] = proc_cwd(pid)
                # 信頼の確認で止まっているか。設定に答えが無ければ確認中(アプリの端末は画面を読めないのでこれが正)。
                # ただし起動直後は記録がまだ無いだけなので、10 秒は「起動中?」のまま待つ
                # (codex の反証 2026-09-19: 記録が遅れる数秒を「確認画面で停止」と誤認していた)
                started = proc_start(pid)
                young = started is not None and time.time() - started < 10
                untrusted = (not young) and not trusted_cwd(t["cwd"])
                screen = "" if t["win"] == 0 else screen_text(t["win"], t["tab"])
                m = re.search(r"--resume\s+(\S+)", procs[pid]["cmd"])
                if "trust this folder" in screen:
                    # 画面で確かめた(iTerm): 断定してよい
                    t["state"], t["mark"] = "確認画面で停止", "🔴"
                    t["topic"] = "フォルダ信頼の確認で止まって未起動" + (f"（resume {m.group(1)[:8]}）" if m else "")
                    t["trust_ask"] = t["cwd"]
                else:
                    # 画面を読めない(アプリの端末)か、画面に出ていない: 断定しない。
                    # 設定に信頼の答えが無ければ「かもしれない」として答えるボタンを出す(codex 再反証 2026-09-19)
                    t["state"], t["mark"] = "起動中?", "🔴"
                    if untrusted:
                        t["trust_ask"] = t["cwd"]
                        t["topic"] = "起動中か、フォルダ信頼の確認で止まっている可能性" + (f"（resume {m.group(1)[:8]}）" if m else "")
        elif codex:
            root = outermost_ai(procs, codex)   # 親が codex でなくても、間に bash を挟んだ入れ子がある
            t["state"], t["mark"] = "codex", "🟩"
            t["pid"] = root
            t["mem"] = descendants_rss(procs, root)
            t["cwd"] = proc_cwd(root)
            t["started"] = proc_start(root)
            cx = codex_session(t["cwd"], t["started"], codex)
            t["ai"] = "Codex " + cx.get("model", "")
            t["model"] = t["model_id"] = cx.get("model", "")
            t["model_style"] = model_style(cx.get("model", ""))
            t["doing"] = cx.get("doing", "")
            t["sid"] = cx.get("sid", "")
            t["transcript"] = cx.get("rollout", "")
            t["task"] = cx.get("task", "")
            if cx.get("updated"):
                t["ago"] = time.time() - cx["updated"]
                t["state_since"] = cx["updated"]
            if cx.get("task"):
                t["topic"] = f"依頼: {cx['task']}"
            if cx.get("doing", "").startswith("✅"):
                t["state"], t["mark"] = "codex 返答待ち", "🟡"
            elif cx.get("doing", "").startswith(("⛔", "⏹")):
                t["state"], t["mark"] = "codex 停止", "🔴"
            t["client"] = _clients and _clients.classify(cwd=t["cwd"], texts=(cx.get("task", ""), topic))
        elif any(other_ai(procs[p]["cmd"]) for p in pids):
            # Gemini / Grok / Cursor など: 動いていることは分かるが、状態の記録が無いので「状態不明」と出す
            oth = [p for p in pids if other_ai(procs[p]["cmd"])]
            root = outermost_ai(procs, oth)
            label = other_ai(procs[root]["cmd"])
            # 状態の記録が無い CLI は、端末に文字が出ているかで見分ける(唯一取れる一次情報)
            idle = tty_idle(t.get("tty"))
            if idle is None:
                state, mark, doing = "他の AI", "🔵", ""
            elif idle < OTHER_AI_BUSY:
                state, mark, doing = "作業中", "🟢", f"{label}: 出力が続いている"
            elif idle >= OTHER_AI_TURN:
                state, mark, doing = "返答待ち", "🟡", f"{label}: 出力が {fmt_idle(idle)} 止まっている"
            else:
                state, mark, doing = "作業中", "🟢", f"{label}: 出力が {fmt_idle(idle)} 止まっている"
            t.update(state=state, mark=mark, ai=label, model="", model_id="", model_style=model_style(""),
                     pid=root, mem=descendants_rss(procs, root), cwd=proc_cwd(root), started=proc_start(root),
                     doing=doing, task="", topic=label, idle=idle, state_since=(time.time() - idle) if idle is not None else None)
        elif len(pids) > 1:
            # zsh 以外のジョブがある(ssh・npm run dev など)
            # iTerm はシェルを login 経由で起動するので、login とシェル自身はジョブに数えない
            jobs = [b for b in (procs[p]["cmd"].split()[0].split("/")[-1].lstrip("-") for p in pids)
                    if b not in ("zsh", "login", "bash", "sh")]
            if jobs:
                t["state"], t["mark"] = "他のジョブ", "🔵"
                t["topic"] = f"{topic} ／ {','.join(sorted(set(jobs))[:3])}"
                t["mem"] = sum(procs[p]["rss"] for p in pids)
        if not t["cwd"] and pids:
            shells = [p for p in pids if procs[p]["cmd"].split()[0].split("/")[-1].lstrip("-") in ("zsh", "bash")]
            t["cwd"] = proc_cwd(max(shells or pids))
        if t["client"] is None and _clients:
            t["client"] = _clients.classify(cwd=t["cwd"], texts=(topic,))
        if not t["sid"]:
            t["sid"] = "tty:" + t["tty"]
    return tabs


def first_user_prompt(path, head_bytes=400_000, max_bytes=8_000_000):
    """トランスクリプト先頭から、人が打った最初の依頼(顧客判定の材料)。

    判定は末尾側と同じ規則(user_prompts_in)を使う。以前はここだけ '"type":"user"' の
    文字列一致で、すきまの入った JSON を読み落としていた。
    先頭 head_bytes に依頼が無ければ、そこから先も max_bytes まで読む
    (道具の出力が先に並ぶ長い記録で、静かに「依頼なし」と返さないため。2026-09-18 実測)。
    """
    try:
        with open(path, "rb") as f:
            read, buf = 0, b""
            while read < max_bytes:
                chunk = f.read(min(head_bytes, max_bytes - read))
                if not chunk:
                    break
                read += len(chunk)
                parts = (buf + chunk).split(b"\n")
                buf = parts.pop()            # 最後は書きかけかもしれないので次へ回す
                for raw in parts:
                    for _, text in user_prompts_in(raw.decode("utf-8", errors="replace")):
                        return text
    except OSError:
        return ""
    return ""


def client_tag(t, color=True):
    """顧客タグ(絵文字+名前)。color=True なら ANSI 24bit で顧客色を付ける。"""
    c = t.get("client") if isinstance(t, dict) else t
    if not c:
        return ""
    label = f"{c.get('emoji', '')}{c['label']}"
    rgb = c.get("rgb")
    if color and rgb and not os.environ.get("NO_COLOR"):
        r, g, b = rgb
        return f"\033[1;38;2;{r};{g};{b}m{label}\033[0m"
    return label


# ---------------------------------------------------------------- 表示 ----
def width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def clip(s, w):
    """幅 w ちょうどに収める(全角混じりでも列がずれない)。

    以前は全角の手前で切ると out+"…" が w-1 桁になり、一覧の列が 1 桁ずれていた(2026-09-18 実測)。
    """
    out, cur = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if cur + cw > w - 1:
            out += "…"
            return out + " " * max(0, w - (cur + 1))
        out += ch
        cur += cw
    return out + " " * (w - cur)


def fmt_ago(sec):
    if sec is None:
        return "-"
    if sec < 3600:
        return f"{int(sec // 60)}分前"
    if sec < 86400:
        return f"{sec / 3600:.1f}時間前"
    return f"{sec / 86400:.1f}日前"


def show(tabs, only_dead=False):
    cols = os.get_terminal_size().columns if sys.stdout.isatty() else 160
    rest = max(40, cols - 82)
    doing_w = max(20, rest // 2)
    topic_w = max(20, rest - doing_w)
    rows = [t for t in tabs if not only_dead or t["mark"] in ("⚪", "🔴")]
    print(f"{'タブ':6}{'状態':18}{'AI':20}{'メモリ':>7}  {'更新':8}{'フォルダ':14}"
          f"{clip('いまやっていること', doing_w)}話題 ／ 依頼")
    for t in rows:
        folder = os.path.basename(t["cwd"].rstrip("/")) if t["cwd"] else ""
        mem = f"{t['mem'] / 1024:.0f}MB" if t["mem"] else "-"
        tag = client_tag(t, color=sys.stdout.isatty())
        ai = clip(ai_label(t, color=False), 20) if t.get("ai") else clip("", 20)
        if t.get("ai") and sys.stdout.isatty():
            ai = ai.replace(ai_label(t, color=False), ai_label(t, color=True), 1)
        topic = (f"{client_tag(t, color=False)} " if tag else "") + t["topic"]
        if tag:   # 色付きタグは幅計算が狂うので、幅は色なし文字列で取り、色は後から差し込む
            topic = clip(topic, topic_w).replace(client_tag(t, color=False), tag, 1)
        else:
            topic = clip(topic, topic_w)
        print(f"{t['win']}-{t['tab']:<4}{t['mark']} {clip(t['state'], 16)}{ai}{mem:>7}  "
              f"{clip(fmt_ago(t['ago']), 8)}{clip(folder, 14)}{clip(t.get('doing', ''), doing_w)}"
              f"{topic}")
    counts = {}
    for t in tabs:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    total = sum(t["mem"] for t in tabs) / 1048576
    print("\n" + " / ".join(f"{k} {v}" for k, v in counts.items()) +
          f"  ｜ タブ {len(tabs)}・この一覧のメモリ合計 {total:.1f}GB")
    dead = [t for t in tabs if t["mark"] in ("⚪", "🔴")]
    if dead and not only_dead:
        print(f"閉じてよい候補 {len(dead)} タブ → `cs dead` で確認、`cs close-dead` で閉じる")


# ---------------------------------------------------------------- 操作 ----
def go(target):
    m = re.fullmatch(r"(\d+)-(\d+)", target)
    if not m:
        sys.exit("使い方: cs go <ウィンドウ>-<タブ>  例: cs go 4-7")
    ok, out = go_tty(tty_of(target))
    if not ok:
        sys.exit(f"タブ {target} を開けない: {out}")


def close_dead(tabs):
    dead = [t for t in tabs if t["mark"] in ("⚪", "🔴")]
    if not dead:
        print("閉じる候補はありません。")
        return
    show(dead, only_dead=True)
    ans = input(f"\n上の {len(dead)} タブを閉じます。よろしいですか？ [y/N] ").strip().lower()
    if ans != "y":
        print("何もしませんでした。")
        return
    for t in dead:
        # 番号は閉じるたびにずれるので、tty で相手を特定し直す
        tty = "/dev/" + t["tty"]
        if t["mark"] == "🔴":
            # 「No, exit」にカーソルがある確認画面なので、Enter で終了させる
            osa(f'tell application "iTerm2"\n repeat with w in windows\n repeat with tb in tabs of w\n'
                f' set s to current session of tb\n if tty of s is "{tty}" then tell s to write text ""\n'
                f' end repeat\n end repeat\nend tell')
            time.sleep(1.5)
        osa(f'tell application "iTerm2"\n repeat with w in windows\n repeat with tb in tabs of w\n'
            f' set s to current session of tb\n if tty of s is "{tty}" then\n close tb\n return\n end if\n'
            f' end repeat\n end repeat\nend tell')
        print(f"閉じた: {t['win']}-{t['tab']}  {t['topic'][:40]}")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    if args and args[0] == "go":
        go(args[1] if len(args) > 1 else "")
        return
    if args and args[0] == "watch":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import overview_watch
        overview_watch.main(args[1:])
        return
    if args and args[0] == "web":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import overview_server
        overview_server.open_in_browser(args[1:])
        return
    if args and args[0] == "map":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import overview_tui
        overview_tui.main(args[1:])
        return
    tabs = classify(iterm_sessions(), processes())
    if not args:
        show(tabs)
    elif args[0] == "dead":
        show(tabs, only_dead=True)
    elif args[0] == "close-dead":
        close_dead(tabs)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
