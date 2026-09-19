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
PIDFILE = aiboard_paths.data("server.pid" if PORT == 8791 else f"server-{PORT}.pid")   # 試験用の別ポートが本番の PID ファイルを上書きしないように


def code_stamp():
    """盤サーバのコードの版(.py の名前・サイズ・更新時刻)。アプリを入れ直した後に古いサーバを使い続けないために比べる。"""
    import hashlib
    h = hashlib.sha1()
    for root, _, files in os.walk(HERE):
        for n in sorted(files):
            if n.endswith(".py"):
                st = os.stat(os.path.join(root, n))
                h.update(f"{os.path.relpath(os.path.join(root, n), HERE)}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:12]


CODE_STAMP = code_stamp()
LOGFILE = aiboard_paths.data("server.log")
HTML = os.path.join(HERE, "overview.html")
INDEX_DAYS = 30
INDEX_REFRESH = 300

_lock = threading.Lock()
_snap = {"t": 0, "data": None}
_login = {}  # ログイン状態は CLI に聞くので 120 秒使い回す
_acct = {}   # アカウント一覧は 60 秒使い回す(記録を舐めるので)
_index = {"stats": None, "building": False, "error": ""}
_groups_seen = {}   # 直近 30 日に見えた持ち場/プロジェクトの件数(/api/index を引いたときに覚える)   # 索引そのものは持たない(index.db から期間で引く)


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


def _tmux_send(target, text, enter, key):
    """tmux のパネルへ送る(Linux の端末はこれ)。iTerm の AppleScript と同じ役割。"""
    if key == "esc":
        args = ["tmux", "send-keys", "-t", target, "Escape"]
    elif key == "enter":
        args = ["tmux", "send-keys", "-t", target, "Enter"]
    elif key in ("up", "down"):
        args = ["tmux", "send-keys", "-t", target, key.capitalize()]
    else:
        args = ["tmux", "send-keys", "-t", target, "-l", text]
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False, (r.stderr.strip() or "tmux send-keys が失敗")[:160]
    if not key and enter:
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True, text=True, timeout=10)
    return True, ""


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
    if str(tab).startswith("8-") or not cs.IS_MAC:
        # tmux のパネル(Linux の端末・mac でも tmux を使っている時)
        target = next((p.get("tmux") for p in cs.tmux_panes() if p["tty"] == now.get("tty", "")), None)
        if not target:
            return False, "tmux のパネルが見つからない(閉じられた)"
        ok, why = _tmux_send(target, text or "", bool(body.get("enter", True)), key)
        with open(SEND_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tab": tab, "sid": sid, "key": key,
                                "text": text if key is None else None, "via": "tmux", "target": target,
                                "rc": 0 if ok else 1, "err": why}, ensure_ascii=False) + "\n")
        return ok, (why or "送った")
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


STOP_LOG = aiboard_paths.data("stop.log")


def stop_session(body, wait=3.0):
    """盤から AI セッションを終わらせる。{tab, sid, dry?}

    送り先は盤が持っている pid を信じず、いまの ps からそのタブの tty 上の claude / codex 本体を引き直す。
    sid が入れ替わっていたら送らない。SIGTERM だけ送り、SIGKILL はしない(終わらなければそう返す)。
    """
    import signal
    tab, sid = str(body.get("tab", "")), str(body.get("sid", ""))
    now = next((x for x in snapshot_cached()["sessions"] if x["tab"] == tab), None)
    if not now:
        return False, f"タブ {tab} が無い(閉じられた)", None
    if not sid or now.get("sid") != sid or not now.get("ai"):
        return False, "中身が入れ替わっているか AI セッションではない(止めなかった)", None
    tty = (now.get("tty") or "").replace("/dev/", "")
    procs = cs.processes()
    cands = [p for p, v in procs.items() if tty and v["tty"] == tty and (cs.is_claude(v["cmd"]) or cs.is_codex(v["cmd"]))]
    roots = [p for p in cands if procs[p]["ppid"] not in cands]
    if len(roots) != 1:
        return False, f"止める相手を 1 つに絞れない(候補 {len(roots)} 個。止めなかった)", None
    pid = roots[0]
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tab": tab, "sid": sid, "pid": pid, "cmd": procs[pid]["cmd"][:120], "dry": bool(body.get("dry"))}
    if body.get("dry"):
        rec["result"] = "dry"
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            rec["result"] = "already gone"
        except PermissionError:
            rec["result"] = "permission denied"
        else:
            t0 = time.time()
            while time.time() - t0 < wait:
                st = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                if not st or st.startswith("Z"):   # 終わって親の回収待ち(ゾンビ)も終了とみなす。kill(pid, 0) はゾンビにも成功する
                    rec["result"] = "exited"; break
                time.sleep(0.2)
            else:
                rec["result"] = "still running"
    with open(STOP_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    msg = {"dry": f"試しのみ: pid {pid} に送る", "exited": "終了した", "already gone": "既に終わっていた",
           "still running": f"{wait:.0f} 秒待っても終わらない(強制終了はしていない)", "permission denied": "権限が無く送れなかった"}[rec["result"]]
    return rec["result"] in ("dry", "exited", "already gone"), msg, pid


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

    def _remote_ok(self, write):
        """自分の機械以外(同じ LAN)からの要求を、遠隔の設定と合言葉で選り分ける。"""
        addr = self.client_address[0] if self.client_address else ""
        path, q = self._query()
        key = self.headers.get("X-Key") or q.get("k", "")
        ok, why = overview.remote_allowed(path, write, addr, key)
        if not ok:
            self._json(403, {"ok": False, "reason": why})
        return ok

    def _guard(self, write):
        """ブラウザ経由の攻撃を塞ぐ(2026-09-17 メイン追加)。
        - Host が 127.0.0.1 / localhost 以外 → DNS リバインディング(攻撃者ドメインを 127.0.0.1 に向ける)で
          会話やターミナル画面を読まれるのを防ぐ
        - 書き込み系(POST)は Origin が自分自身で、かつ独自ヘッダー X-Overview: 1 必須 →
          外部サイトからのフォーム送信・fetch では付けられない(付けるとプリフライトになり、ここは応じない)"""
        if not self._remote_ok(write):
            return False
        addr = self.client_address[0] if self.client_address else ""
        host = (self.headers.get("Host") or "").lower()
        # 自分の機械からは 127.0.0.1 / localhost だけ。遠隔(同じ LAN)は合言葉で通したので Host は問わない
        if overview.is_loopback(addr) and host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._json(403, {"ok": False, "reason": "Host が不正"})
            return False
        if write:
            origin = self.headers.get("Origin")
            if overview.is_loopback(addr) and origin not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
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
            elif path == "/m":
                # 遠隔用の小さな画面(判断待ちに答えるだけ)。CSP は盤と同じで、外へはつながらない
                body = open(os.path.join(HERE, "mobile.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                                 "frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/actions":
                import autopilot
                self._json(200, {"ok": True, "policy": autopilot.policy(), "rows": autopilot.recent(60),
                                 "autoable": __import__("decide").AUTOABLE})
            elif path == "/api/keys":
                import keys as keysmod
                if not _login.get("data"):
                    _login.update(t=time.time(), data=overview.login_status())
                self._json(200, {"ok": True, "rows": keysmod.status(_login.get("data")),
                                 "note": "AIBoard は鍵の値を持ちません。入れる・出すは端末で行います"})
            elif path == "/api/usage":
                # アカウントごとの利用状況(自分のログから数えた量＋学習した母数に対する目安の %)
                import usage
                rows = {}
                for a in (_login.get("data") or overview.login_status()):
                    if a.get("ai") != "Claude" or not a.get("config_dir"):
                        continue
                    rows[a["profile"]] = {"config_dir": a["config_dir"], "usage": usage.status_async(a["config_dir"])}
                self._json(200, {"ok": True, "accounts": rows, "note": "公式の使用率ではありません。使った量は自分の会話ログから数えたもの、"
                                                                      "% は前に上限に当たった時の使用量を 100% とした目安です"})
            elif path == "/api/judge":
                import judge
                self._json(200, {"ok": True, **judge.status()})
            elif path == "/m.webmanifest":
                body = json.dumps({"name": "AIBoard", "short_name": "AIBoard", "start_url": "/m", "display": "standalone",
                                   "background_color": "#111317", "theme_color": "#111317",
                                   "icons": [{"src": "/m-icon.png", "sizes": "180x180", "type": "image/png"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/manifest+json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif path == "/m-icon.png":
                ico = os.path.join(HERE, "m-icon.png")
                if not os.path.exists(ico):
                    return self._json(404, {"ok": False})
                body = open(ico, "rb").read()
                self.send_response(200); self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "max-age=86400")
                self.end_headers(); self.wfile.write(body)
            elif path == "/api/remote":
                c = overview.remote_config()
                self._json(200, {"ok": True, "enabled": c["enabled"], "token": c["token"] if c["enabled"] else "",
                                 "urls": overview.remote_urls(PORT) if c["enabled"] else [],
                                 "addrs": overview.remote_addrs() if c["enabled"] else []})
            elif path == "/api/version":
                self._json(200, {"ok": True, "stamp": CODE_STAMP, "pid": os.getpid()})
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
            elif path == "/api/notes":
                key = q.get("key", "")
                self._json(200, {"ok": True, "key": key, "text": overview.read_notes(key) if key else "",
                                 "instructions": (overview.groups().get(key) or {}).get("instructions", "")})
            elif path == "/api/schedule":
                # 予約の一覧(次に走る時刻つき)。走らせるのはアプリ側
                rows = []
                for j in overview.read_schedule():
                    rows.append(dict(j, next_at=overview.job_next_at(j), due=overview.job_due(j), missed_at=overview.job_missed(j)))
                self._json(200, {"ok": True, "jobs": rows, "min_every": overview.SCHEDULE_MIN_EVERY})
            elif path == "/api/delegations":
                key = q.get("key", "")
                self._json(200, {"ok": True, "key": key, "rows": overview.read_delegations(key) if key else []})
            elif path == "/api/pick":
                now = time.time()
                if not _login.get("data") or now - _login.get("t", 0) > 120:
                    _login.update(t=now, data=overview.login_status())
                if not _acct.get("data") or now - _acct.get("t", 0) > 60:   # ログイン状態を合流させた一覧で選ぶ
                    _acct.update(t=now, data=overview.accounts_full(snapshot_cached()["sessions"], _login["data"]))
                self._json(200, {"ok": True, **overview.pick_ai(q.get("prefer", ""), _acct["data"])})
            elif path == "/api/groups":
                # 束ね方の候補(いま動いている分と、索引にある直近 30 日)と、いまの上書き
                snap = snapshot_cached()
                live = {}
                for x in snap["sessions"]:
                    k = x.get("project_hint") or x.get("project") or ""
                    if k and x.get("ai"):
                        live[k] = live.get(k, 0) + 1
                seen = dict(_groups_seen)
                self._json(200, {"ok": True, "groups": overview.groups(), "clients": overview.client_defs(),
                                 "detected": sorted(({"key": k, "live": live.get(k, 0), "recent": seen.get(k, 0)}
                                                     for k in set(live) | set(seen)),
                                                    key=lambda r: (-r["live"], -r["recent"], r["key"]))})
            elif path == "/api/extensions":
                self._json(200, {"ok": True, **overview.extensions_info(refresh=q.get("refresh") == "1")})
            elif path == "/api/accounts":
                now = time.time()
                if q.get("refresh") == "1" or not _login.get("data") or now - _login.get("t", 0) > 120:
                    _login.update(t=now, data=overview.login_status())
                if q.get("refresh") == "1" or not _acct.get("data") or now - _acct.get("t", 0) > 60:
                    _acct.update(t=now, data=overview.accounts_full(snapshot_cached()["sessions"], _login["data"]))
                self._json(200, {"ok": True, "accounts": _acct["data"], "fetched": _acct["t"], "auth_fetched": _login["t"],
                                 "clis": overview.ai_clis(force=q.get("refresh") == "1", logins=_login["data"])})
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
                base = {k: s.get(k) for k in ("tab", "sid", "state", "mark", "ai", "model_style", "client", "project", "doing", "task", "state_for", "limit", "trust_ask", "auth_lost", "account")}
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
                try:   # 束ね方の設定に出す候補として、直近で見えた持ち場/プロジェクトを覚えておく(数えるためだけ)
                    for rec in json.loads(r.stdout).get("records", []):
                        k = rec.get("project_hint") or rec.get("project") or ""
                        if k:
                            _groups_seen[k] = _groups_seen.get(k, 0) + 1
                except ValueError:
                    pass
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
        if path == "/api/notes":
            try:
                n = overview.save_notes(str(body.get("key", "")), str(body.get("text", "")))
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
            return self._json(200, {"ok": True, "chars": n})
        if path == "/api/actions":
            import autopilot
            return self._json(200, {"ok": True, "policy": autopilot.set_policy(body)})
        if path == "/api/keys":
            import keys as keysmod
            try:
                return self._json(200, {"ok": True, "rows": keysmod.save(body.get("rows") or [])})
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
        if path == "/api/git":
            import gitinfo
            return self._json(200, {"ok": True, **gitinfo.set_config(body)})
        if path == "/api/judge":
            import judge
            try:
                if body.get("op") == "try":
                    # 設定を試す: 決まった候補で 1 回だけ呼び、選べたか・何ミリ秒か・失敗の理由を返す(結果は使わない)
                    t0 = time.time()
                    res = judge.decide("priority", [{"id": "a", "text": "確認待ち 25分 案件A 本番の切替"},
                                                    {"id": "b", "text": "返答待ち 2分 案件B 索引の作り直し"}], {"count": 2})
                    return self._json(200, {"ok": True, "result": res, "ms": int((time.time() - t0) * 1000), **judge.status()})
                return self._json(200, {"ok": True, **judge.set_config(body), "last": judge.LAST})
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
        if path == "/api/sound":
            return self._json(200, {"ok": True, "sound": overview.sound_set(bool(body.get("on")))})
        if path == "/api/remote":
            # 遠隔の入切は自分の機械からだけ(遠隔からこの道は通らない)
            c = overview.remote_set(bool(body.get("enabled")))
            return self._json(200, {"ok": True, "enabled": c["enabled"], "token": c["token"] if c["enabled"] else "",
                                    "urls": overview.remote_urls(PORT) if c["enabled"] else [],
                                    "restart": "待ち受けの変更は盤サーバの入れ直しで効きます"})
        if path == "/api/plan":
            # まとめ役: 依頼を並行できる小さな仕事に分ける(案を返すだけ。動かすのは人が選んでから)
            pick = overview.pick_ai(str(body.get("prefer", "")), _acct.get("data"))
            tasks, why = overview.plan_tasks(str(body.get("text", "")), ai=pick.get("ai"), profile=pick.get("profile", ""))
            return self._json(200 if tasks else 400, {"ok": bool(tasks), "tasks": tasks, "reason": why,
                                                      "by": pick.get("ai", ""), "profile": pick.get("profile", "")})
        if path == "/api/schedule":
            op = str(body.get("op", "save"))
            try:
                if op == "delete":
                    return self._json(200, {"ok": True, "deleted": overview.delete_job(str(body.get("id", "")))})
                if op == "ran":
                    return self._json(200, {"ok": True, "marked": overview.mark_ran(str(body.get("id", "")))})
                return self._json(200, {"ok": True, "job": overview.save_job(body)})
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
        if path == "/api/delegations":
            try:
                if body.get("op") == "link":
                    return self._json(200, {"ok": True, "linked": overview.link_delegation(str(body.get("key", "")), str(body.get("id", "")), str(body.get("sid", "")))})
                rec = overview.add_delegation(str(body.get("key", "")), body)
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
            return self._json(200, {"ok": True, "row": rec})
        if path == "/api/groups":
            try:
                saved = overview.save_groups(body.get("groups") or {}, {c["id"] for c in overview.client_defs()})
            except ValueError as e:
                return self._json(400, {"ok": False, "reason": str(e)})
            return self._json(200, {"ok": True, "groups": saved})
        if path == "/api/stop":
            ok, reason, pid = stop_session(body)
            return self._json(200 if ok else 409, {"ok": ok, "reason": reason, "pid": pid})
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
    bind = "0.0.0.0" if overview.remote_config().get("enabled") else HOST   # 遠隔を入れた時だけ LAN に出す
    srv = ThreadingHTTPServer((bind, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"overview server http://{HOST}:{PORT}/ pid={os.getpid()} bind={bind}\n")
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(PIDFILE)
        except OSError:
            pass


def listener_pid():
    """このポートで実際に待ち受けている盤サーバの pid。PID ファイルは別の起動に上書きされ得るので使わない。"""
    # ポートだけで引く: 遠隔を入れると 0.0.0.0 で待ち受けるので、@127.0.0.1 で引くと自分のサーバを見失い、
    # 止めることも入れ替えることもできなくなる(2026-09-18 実測)
    r = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
    for tok in r.stdout.split():
        cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", tok], capture_output=True, text=True).stdout
        if "overview_server.py" in cmd and "--serve" in cmd:
            return int(tok)
    return None


def alive():
    """このポートで盤サーバが待ち受けていて、HTTP が返るか。"""
    if listener_pid() is None:
        return False
    try:   # 版だけ返す軽い口で見る(snapshot は iTerm が遅いと数十秒かかり、起動失敗と誤判定していた)
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/version", timeout=3) as r:
            if r.status == 200:
                return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/snapshot", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def stop():
    """`cs web stop`: PID ファイルに記録された「このサーバー自身」だけを止める(メモリを空けたい時用)。"""
    import signal
    pid = listener_pid()   # ポート {PORT} で待ち受けている overview_server.py --serve だけ
    if pid is None:
        print("動いていません。")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    try:
        if int(open(PIDFILE).read().strip()) == pid:
            os.remove(PIDFILE)
    except (OSError, ValueError):
        pass
    print(f"止めました（pid {pid}）。もう一度開くときは cs web")


def open_in_browser(args=()):
    if "stop" in args:
        return stop()
    if alive():
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/version", timeout=3) as r:
                running = json.loads(r.read()).get("stamp")
        except Exception:
            running = None   # 版を返さない = 更新前のサーバ
        if running != code_stamp():
            print(f"盤サーバのコードが更新されている(動作中 {running} / 手元 {code_stamp()})。入れ替える")
            stop()
            for _ in range(30):
                time.sleep(0.2)
                if not alive():
                    break
    if not alive():
        log = open(LOGFILE, "a")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):   # 初回の調べ物が重い時があるので長めに待つ
            time.sleep(0.5)
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
