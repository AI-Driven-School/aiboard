#!/usr/bin/env python3
"""overview_server.py — ブラウザ版「AI作業の全体像」。127.0.0.1 だけで待ち受ける(外部送信なし・標準ライブラリのみ)。

  GET  /                    1枚の HTML(overview.html。外部CDN不使用)
  GET  /api/snapshot        overview.snapshot() の JSON(2秒キャッシュ)
  GET  /api/detail?tab=1-3  そのタブの詳細(記録・直近の流れ・iTerm 画面の末尾40行)。伏せ字済み
  GET  /api/index?days=30&unattended=0   過去セッションの索引とエッジ(overview_index)
  GET  /api/session?id=...  過去セッション1件の会話(依頼/返答/操作)。伏せ字済み
  POST /api/go     {"tab":"1-3"}  そのタブを前面に(\\d+-\\d+ 以外は 400)
  POST /api/resume {"id":"..."}   新しい iTerm タブで元の cwd に cd → claude --resume / codex resume(索引に無い id は 400)

`cs web` = open_in_browser(): 未起動なら背景で起動(PIDファイルはこのフォルダ) → ブラウザで開く。二重起動しない。
索引は起動時と 5 分ごとに背景スレッドで増分更新する。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cs  # noqa: E402
import overview  # noqa: E402
import overview_index  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("OVERVIEW_PORT", "8791"))
import aiboard_paths  # noqa: E402
PIDFILE = aiboard_paths.data("server.pid")
LOGFILE = aiboard_paths.data("server.log")
HTML = os.path.join(HERE, "overview.html")
INDEX_DAYS = 30
INDEX_REFRESH = 300

_lock = threading.Lock()
_snap = {"t": 0, "data": None}
_login = {}  # ログイン状態は CLI に聞くので 120 秒使い回す
_acct = {}   # アカウント一覧は 60 秒使い回す(記録を舐めるので)
_index = {"stats": None, "building": False, "error": ""}   # 索引そのものは持たない(index.db から期間で引く)


def snapshot_cached(max_age=None):
    """最新の snapshot を待たせずに返す。作るのは snapshot_loop()(裏のスレッド)。

    以前はリクエストのたびにその場で全タブを調べ直していて、混んでいると1回 2〜13 秒かかり、
    同時に来た画面取得まで 4 秒待たされた(2026-09-17 実測)。
    """
    _snap["asked"] = time.time()
    with _lock:
        if _snap["data"]:
            return _snap["data"]
    data = overview.snapshot()          # 起動直後の1回だけ
    with _lock:
        _snap.update(t=time.time(), data=data)
    return data


def snapshot_loop():
    while True:
        t = time.time()
        if t - _snap.get("asked", 0) < 20:     # 盤を誰も開いていない間は調べない
            try:
                data = overview.snapshot()
                with _lock:
                    _snap.update(t=time.time(), data=data)
            except Exception as e:
                _snap["error"] = f"{type(e).__name__}: {e}"
        time.sleep(max(1.0, 2.5 - (time.time() - t)))


def index_loop():
    while True:
        # ビルドは全件を読むので子プロセスで走らせ、終わったらメモリごと返す。
        # 以前は結果の dict をこのプロセスが持ち続け、常駐が 319MB になっていた(2026-09-17 実測)
        _index["building"] = True
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, "overview_index.py"), str(INDEX_DAYS)],
                               capture_output=True, text=True, timeout=3600)
            if r.returncode == 0:
                _index.update(stats=json.loads(r.stdout.strip().splitlines()[-1]), error="")
            else:
                _index["error"] = f"索引ビルド失敗 rc={r.returncode}: {r.stderr.strip()[-300:]}"
        except Exception as e:  # 索引が壊れても本体は動かす
            _index["error"] = f"{type(e).__name__}: {e}"
        _index["building"] = False
        time.sleep(INDEX_REFRESH)


def live_ids():
    snap = _snap["data"] or snapshot_cached()
    return [s["sid"] for s in snap["sessions"] if s.get("sid")]


def session_detail(sid):
    rec = overview_index.get_record(sid)
    if not rec:
        return None
    if rec["ai"] == "Codex":
        tl = overview.timeline_codex(rec["path"], limit=400, with_text=True)
    else:
        tl = overview.timeline_claude(rec["path"], limit=400, tail_bytes=6_000_000, with_text=True,
                                      sidechain_ok=(rec["kind"] == "subagent"))
    row = {k: v for k, v in rec.items() if k != "cont_prompts"}
    row["first_prompt"] = overview.redact(row.get("first_prompt", ""))
    row["last_prompt"] = overview.redact(row.get("last_prompt", ""))
    edges = overview_index.edges_of(sid)
    return {"ok": True, "record": row, "timeline": tl, "edges": edges,
            "live_tab": next((s["tab"] for s in (_snap["data"] or {}).get("sessions", []) if s.get("sid") == sid), None)}


SEND_KEYS = {"enter": "\r", "esc": "\x1b", "ctrl-c": "\x03", "up": "\x1b[A", "down": "\x1b[B",
             "tab": "\t", "shift-tab": "\x1b[Z", "backspace": "\x7f"}
SEND_LOG = aiboard_paths.data("send.log")


def send_to_tab(body):
    """盤から iTerm のタブへ文字かキーを送る。{tab, sid, text?|key?, enter?}

    タブ番号は閉じれば詰まる。画面を描いた時と宛先が入れ替わっていたら送らない:
    いまそのタブに居るセッションの sid が、盤が持っている sid と一致する時だけ送る。送った内容は記録する。
    """
    tab, sid = str(body.get("tab", "")), str(body.get("sid", ""))
    m = re.fullmatch(r"(\d+)-(\d+)", tab)
    if not m:
        return False, "tab は <win>-<tab>"
    text, key = body.get("text"), body.get("key")
    if key is not None and key not in SEND_KEYS:
        return False, f"送れるキー: {', '.join(SEND_KEYS)}"
    if key is None and (not isinstance(text, str) or not text or len(text) > 4000):
        return False, "text が空か長すぎる(4000字まで)"
    now = next((x for x in snapshot_cached()["sessions"] if x["tab"] == tab), None)
    if not now:
        return False, f"タブ {tab} が無い(閉じられた)"
    if not sid or not now.get("sid") or sid.startswith("tty:") or not now.get("ai"):
        return False, "AI セッションのタブにだけ送れる(素のシェルには送らない)"
    if now["sid"] != sid:
        return False, f"タブ {tab} の中身が入れ替わっている(送らなかった)。盤を更新してやり直す"
    payload = SEND_KEYS[key] if key is not None else text
    # 宛先は tty で指す(タブ番号は位置ずれする)。見つからなければ送らない
    chars = ", ".join(str(ord(c)) for c in payload)
    nl = "YES" if (key is None and body.get("enter", True)) else "NO"
    ok, out = cs.on_tty(now.get("tty", ""), f'tell s to write text payload newline {nl}\n return "OK"',
                        pre=f"set payload to (character id {{{chars}}})")
    r = type("R", (), {"returncode": 0 if ok else 1, "stderr": "" if ok else out})()
    if not ok and out == "NOTFOUND":
        return False, f"タブ {tab} のセッションが見つからない(閉じられた。送らなかった)"
    with open(SEND_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tab": tab, "sid": sid, "key": key,
                            "text": text if key is None else None, "enter": nl, "rc": r.returncode,
                            "err": r.stderr.strip()[:200]}, ensure_ascii=False) + "\n")
    return (r.returncode == 0), (r.stderr.strip()[:200] or "送った")


def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def resume_in_iterm(rec):
    """新しい iTerm タブを開き、元の cwd に cd してから再開コマンドを打つ。"""
    cwd = rec.get("cwd") or os.path.expanduser("~")
    if rec["ai"] == "Codex":
        cmd = f"cd {shell_quote(cwd)} && codex resume {rec['id']}"
    else:
        # claude は zshrc の関数(フォルダでアカウントを切り替える設定があり得る)に任せるので素の `claude`
        cmd = f"cd {shell_quote(cwd)} && claude --resume {rec['id']}"
    script = ('tell application "iTerm2"\n activate\n'
              ' if (count of windows) is 0 then create window with default profile\n'
              ' tell current window\n  create tab with default profile\n'
              f'  tell current session to write text {applescript_str(cmd)}\n end tell\nend tell')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip(), cmd


class Handler(BaseHTTPRequestHandler):
    server_version = "overview/1"

    def log_message(self, fmt, *args):  # 1行ログ(標準の verbose を抑える)
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, code, obj):
        self._raw(code, json.dumps(obj, ensure_ascii=False).encode())

    def _raw(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, write):
        """ブラウザ経由の攻撃を塞ぐ(2026-09-17 メイン追加)。
        - Host が 127.0.0.1 / localhost 以外 → DNS リバインディング(攻撃者ドメインを 127.0.0.1 に向ける)で
          会話やターミナル画面を読まれるのを防ぐ
        - 書き込み系(POST)は Origin が自分自身で、かつ独自ヘッダー X-Overview: 1 必須 →
          外部サイトからのフォーム送信・fetch では付けられない(付けるとプリフライトになり、ここは応じない)"""
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._json(403, {"ok": False, "reason": "Host が不正"})
            return False
        if write:
            origin = self.headers.get("Origin")
            if origin not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                self._json(403, {"ok": False, "reason": "Origin が不正"})
                return False
            if self.headers.get("X-Overview") != "1":
                self._json(403, {"ok": False, "reason": "X-Overview ヘッダーが無い"})
                return False
        return True

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_GET(self):
        if not self._guard(write=False):
            return
        path, q = self._query()
        try:
            if path == "/":
                body = open(HTML, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # 盤のページは自分(127.0.0.1)以外と通信できない。ブラウザが強制する(README「Local only」の根拠)
                self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                                 "frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/snapshot":
                self._json(200, snapshot_cached())
            elif path == "/api/detail":
                tab = q.get("tab", "")
                if not re.fullmatch(r"\d+-\d+", tab):
                    return self._json(400, {"ok": False, "reason": "tab は <win>-<tab>"})
                self._json(200, overview.detail(tab, sess=snapshot_cached()["sessions"]))
            elif path == "/api/settings":
                now = time.time()
                if q.get("refresh") == "1" or not _login.get("data") or now - _login.get("t", 0) > 120:
                    _login.update(t=now, data=overview.login_status())
                self._json(200, {"ok": True, "logins": _login["data"], "settings": overview.settings_info(), "fetched": _login["t"]})
            elif path == "/api/extensions":
                self._json(200, {"ok": True, **overview.extensions_info(refresh=q.get("refresh") == "1")})
            elif path == "/api/accounts":
                now = time.time()
                if not _acct.get("data") or now - _acct.get("t", 0) > 60:
                    _acct.update(t=now, data=overview.accounts())
                self._json(200, {"ok": True, "accounts": _acct["data"], "fetched": _acct["t"]})
            elif path == "/api/conv":
                # 会話ビュー用: 依頼・返答・操作を時系列で(最大 150 件)。記録が変わっていなければ 304 相当で軽く返す
                tab = q.get("tab", "")
                s = next((x for x in snapshot_cached()["sessions"] if x["tab"] == tab), None)
                if not s:
                    return self._json(404, {"ok": False, "reason": f"タブ {tab} が無い"})
                tr = s.get("transcript") or ""
                try:
                    st = os.stat(tr); etag = f"{st.st_size}-{int(st.st_mtime)}"
                except OSError:
                    etag = ""
                base = {k: s.get(k) for k in ("tab", "sid", "state", "mark", "ai", "model_style", "client", "project", "doing", "task", "state_for", "limit")}
                if etag and q.get("etag") == etag:
                    return self._json(200, dict(base, ok=True, same=True, etag=etag))
                tl = []
                if tr:
                    tl = overview.timeline_codex(tr, limit=150, with_text=True) if (s.get("ai") or "").startswith("Codex") \
                        else overview.timeline_claude(tr, limit=150, tail_bytes=4_000_000, with_text=True)
                self._json(200, dict(base, ok=True, etag=etag, timeline=tl))
            elif path == "/api/screen":
                tab = q.get("tab", "")
                if not re.fullmatch(r"\d+-\d+", tab):
                    return self._json(400, {"ok": False, "reason": "tab は <win>-<tab>"})
                tty = cs.tty_of(tab, snapshot_cached()["sessions"])
                if not tty:
                    return self._json(404, {"ok": False, "reason": f"タブ {tab} が無い"})
                self._json(200, {"ok": True, "tab": tab, "screen": overview.screen_tail(tab, lines=int(q.get("lines", "60")), tty=tty)})
            elif path == "/api/index":
                days = q.get("days", "30")
                days = None if days in ("all", "0") else int(days)
                # 結果(約13MB)の組み立ては子プロセスで。ここはバイト列を中継するだけ
                arg = json.dumps({"days": days, "unattended": q.get("unattended") == "1", "live_ids": live_ids(),
                                  "children": q.get("children") == "1",
                                  "extra": {"stats": _index["stats"], "building": _index["building"],
                                            "error": _index["error"]}})
                r = subprocess.run([sys.executable, os.path.join(HERE, "overview_index.py"), "--query", arg],
                                   capture_output=True, timeout=120)
                if r.returncode != 0:
                    return self._json(500, {"ok": False, "reason": r.stderr.decode("utf-8", "replace")[-300:]})
                self._raw(200, r.stdout)
            elif path == "/api/session":
                sid = q.get("id", "")
                if not re.fullmatch(r"[0-9a-f\-]{8,}(/agent-[0-9a-f]+)?", sid):
                    return self._json(400, {"ok": False, "reason": "id の形が違う"})
                d = session_detail(sid)
                self._json(200 if d else 404, d or {"ok": False, "reason": "索引に無い"})
            else:
                self._json(404, {"ok": False, "reason": "not found"})
        except Exception as e:
            self._json(500, {"ok": False, "reason": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._guard(write=True):
            return
        path, _ = self._query()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "reason": "JSON ではない"})
        if path == "/api/go":
            tab = str(body.get("tab", ""))
            if not re.fullmatch(r"\d+-\d+", tab):
                return self._json(400, {"ok": False, "reason": "tab は <win>-<tab>(例 1-3)"})
            if tab.startswith("0-"):
                return self._json(409, {"ok": False, "tab": tab, "reason": "AIBoard.app の端末はアプリの中で選ぶ(ブラウザからは前面に出せない)"})
            ok, out = cs.go_tty(cs.tty_of(tab, snapshot_cached()["sessions"]))
            return self._json(200 if ok else 409, {"ok": ok, "tab": tab, "reason": "" if ok else f"タブ {tab} を開けない: {out}"})
        if path == "/api/send":
            ok, reason = send_to_tab(body)
            return self._json(200 if ok else 409, {"ok": ok, "reason": reason})
        if path == "/api/resume":
            sid = str(body.get("id", ""))
            if not re.fullmatch(r"[0-9a-f\-]{16,}", sid):
                return self._json(400, {"ok": False, "reason": "id の形が違う(UUID)"})
            rec = overview_index.get_record(sid)
            if not rec or rec["kind"] == "subagent":
                return self._json(400, {"ok": False, "reason": "索引に無い id(サブエージェントは再開できない)"})
            ok, err, cmd = resume_in_iterm(rec)
            return self._json(200 if ok else 500, {"ok": ok, "reason": err, "cmd": cmd})
        self._json(404, {"ok": False, "reason": "not found"})


def serve():
    import signal
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # kill で止めても PID ファイルを片付ける
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    threading.Thread(target=snapshot_loop, daemon=True).start()
    if not os.environ.get("OVERVIEW_NO_INDEX"):   # 検証中など、別プロセスが索引を作っている時は止める
        threading.Thread(target=index_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"overview server http://{HOST}:{PORT}/ pid={os.getpid()}\n")
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(PIDFILE)
        except OSError:
            pass


def alive():
    """PID ファイルのプロセスが生きていて、かつ HTTP が返るか。"""
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)   # シグナル 0 は存在確認だけ(何も送らない)
    except (OSError, ValueError):
        return False
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/snapshot", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def stop():
    """`cs web stop`: PID ファイルに記録された「このサーバー自身」だけを止める(メモリを空けたい時用)。"""
    import signal
    try:
        pid = int(open(PIDFILE).read().strip())
    except (OSError, ValueError):
        print("動いていません。")
        return
    cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    if "overview_server.py" not in cmd or "--serve" not in cmd:
        print(f"PID {pid} は全体地図のサーバーではないので触りません。")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)
    print(f"止めました（pid {pid}）。もう一度開くときは cs web")


def open_in_browser(args=()):
    if "stop" in args:
        return stop()
    if not alive():
        log = open(LOGFILE, "a")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(30):
            time.sleep(0.3)
            if alive():
                break
        else:
            sys.exit(f"サーバーが起動しない。ログ: {LOGFILE}")
        print(f"起動した: http://{HOST}:{PORT}/ (ログ {LOGFILE})")
    else:
        print(f"起動済み: http://{HOST}:{PORT}/")
    if "--no-open" not in args:
        subprocess.run(["open", f"http://{HOST}:{PORT}/"])


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        open_in_browser(sys.argv[1:])
