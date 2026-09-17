"""AIBoard UAT(受け入れ試験)の自動実行。

  python3 scripts/uat/run_uat.py [--only ID,ID] [--out DIR]

本番(8791 番・~/.aiboard)には触れない:
  - 盤サーバは 8793 番、データ置き場は一時フォルダ(索引 index.db と端末台帳は読み取りで共有)
  - 送信・終了・再開・端末移動の API はブラウザ側で横取りし、実セッションへは何も送らない
  - 「終了」の実シグナル試験は、試験が自分で起こした使い捨てプロセスにだけ送る
結果(実セッションの題名などを含む)は --out(既定 ~/aiboard-private/uat/)に JSON と Markdown で書く。
"""
import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARD = os.path.join(ROOT, "board")
HOME = os.path.expanduser("~")
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}/"
LIVE_DATA = os.path.join(HOME, ".aiboard")

RESULTS = []


def case(cid, title, kind="auto"):
    def deco(fn):
        fn.cid, fn.title, fn.kind = cid, title, kind
        CASES.append(fn)
        return fn
    return deco


CASES = []


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def http(path, method="GET", body=None, headers=None, raw=False):
    h = {"X-Overview": "1"}
    h.update(headers or {})
    req = urllib.request.Request(BASE.rstrip("/") + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data or b"{}")), dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data or b"{}"), dict(e.headers)
        except ValueError:
            return e.code, data, dict(e.headers)


def env_for_test(data_dir):
    e = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data_dir, OVERVIEW_NO_INDEX="1")
    return e


# ------------------------------------------------------------------ 準備
def prepare(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    for n in ("index.db", "clients.json"):   # 読むだけのものは本物を指す(試験側は書かない)
        src, dst = os.path.join(LIVE_DATA, n), os.path.join(data_dir, n)
        if os.path.exists(src) and not os.path.exists(dst):
            os.symlink(src, dst)
    src = os.path.join(LIVE_DATA, "app_panes.json")   # 試験のアプリが書くのでコピー(リンクだと本番の台帳を上書きする)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(data_dir, "app_panes.json"))


def start_server(data_dir):
    r = subprocess.run([sys.executable, os.path.join(BOARD, "overview_server.py"), "--no-open"],
                       env=env_for_test(data_dir), capture_output=True, text=True, timeout=60, cwd=BOARD)
    return r.stdout + r.stderr


def snapshot():
    for _ in range(40):
        st, d, _ = http("/api/snapshot")
        if st == 200 and d.get("sessions") is not None:
            return d
        time.sleep(1)
    raise Fail("snapshot が取れない")


# ------------------------------------------------------------------ サーバ
@case("SV-01", "盤サーバが試験用ポートで起動し、コードの版を返す")
def sv01(ctx):
    sys.path.insert(0, BOARD)
    os.environ["OVERVIEW_PORT"] = str(PORT)
    st, d, _ = http("/api/version")
    check(st == 200 and d.get("stamp"), f"/api/version {st} {d}")
    import importlib
    import overview_server as osv
    importlib.reload(osv)
    check(d["stamp"] == osv.code_stamp(), f"版が手元と違う {d['stamp']} != {osv.code_stamp()}")
    return f"stamp={d['stamp']} pid={d['pid']}"


@case("SV-02", "Host が自分以外なら 403")
def sv02(ctx):
    st, d, _ = http("/api/snapshot", headers={"Host": "evil.example:8793"})
    check(st == 403, f"status {st}")
    return f"403 {d.get('reason')}"


@case("SV-03", "書き込み(POST)は X-Overview ヘッダーと自分の Origin が無ければ 403")
def sv03(ctx):
    req = urllib.request.Request(BASE + "api/send", method="POST", data=b"{}")
    try:
        urllib.request.urlopen(req, timeout=10); st = 200
    except urllib.error.HTTPError as e:
        st = e.code
    check(st == 403, f"ヘッダー無しで {st}")
    st2, d2, _ = http("/api/send", "POST", {}, headers={"Origin": "http://evil.example"})
    check(st2 == 403, f"他サイト Origin で {st2}")
    return "ヘッダー無し 403 / 他 Origin 403"


@case("SV-04", "盤のページに CSP(外部へ接続させない)が付く")
def sv04(ctx):
    st, _, h = http("/", raw=True)
    csp = h.get("Content-Security-Policy", "")
    check(st == 200 and "connect-src 'self'" in csp and "default-src 'self'" in csp, f"CSP={csp!r}")
    return csp[:80]


@case("SV-05", "コードが更新されたら、古いサーバを止めて入れ替える")
def sv05(ctx):
    _, v1, _ = http("/api/version")
    target = os.path.join(BOARD, "aiboard_paths.py")
    st = os.stat(target)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # 中身は変えず更新時刻だけ進める
    out = start_server(ctx["data"])
    _, v2, _ = http("/api/version")
    check("入れ替える" in out, f"入れ替えの表示が無い: {out.strip()[:200]}")
    check(v2["pid"] != v1["pid"] and v2["stamp"] != v1["stamp"], f"pid {v1['pid']}→{v2['pid']} stamp {v1['stamp']}→{v2['stamp']}")
    _, v3, _ = http("/api/version")
    return f"pid {v1['pid']}→{v2['pid']} / stamp {v1['stamp']}→{v2['stamp']}"


@case("SV-06", "試験サーバが本番の PID ファイル(~/.aiboard/server.pid)を書き換えない")
def sv06(ctx):
    p = os.path.join(LIVE_DATA, "server.pid")
    now = os.stat(p).st_mtime if os.path.exists(p) else None
    check(now == ctx["live_pid_mtime"], f"mtime {ctx['live_pid_mtime']} → {now}")
    check(os.path.exists(os.path.join(ctx["data"], f"server-{PORT}.pid")), "試験用 PID ファイルが無い")
    return "本番の PID ファイルは不変・試験用は server-8793.pid"


# ------------------------------------------------------------------ 判定ロジック(単体)
@case("LP-01", "skill / MCP / 予約 / loop の集計が、会話ログを別実装で全件数えた結果と一致する")
def lp01(ctx):
    import overview as o
    files = [f for f in glob.glob(HOME + "/.claude/projects/*/*.jsonl") if time.time() - os.path.getmtime(f) < 30 * 86400]
    files.sort(key=os.path.getmtime, reverse=True)
    picked = []
    for f in files:
        with open(f, "rb") as fh:
            blob = fh.read()
        if b'"name":"Skill"' in blob or b'"name":"mcp__' in blob or b'"name":"CronCreate"' in blob or b'"name":"ScheduleWakeup"' in blob:
            picked.append(f)
        if len(picked) >= 25:
            break
    check(picked, "試せる会話ログが無い")
    bad = []
    for f in picked:
        sk, mc, cr, wk = {}, {}, 0, "none"
        for line in open(f, encoding="utf-8", errors="replace"):
            if not line.endswith("\n"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            c = (d.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                n = b.get("name", "")
                if n == "Skill":
                    k = str((b.get("input") or {}).get("skill") or "?"); sk[k] = sk.get(k, 0) + 1
                elif n.startswith("mcp__"):
                    k = n.split("__")[1]; mc[k] = mc.get(k, 0) + 1
                elif n == "CronCreate":
                    cr += 1
                elif n == "ScheduleWakeup":
                    wk = "stop" if (b.get("input") or {}).get("stop") else "set"
        o._TOOLS.pop(f, None)
        got = o.session_tools(f)
        gw = "none" if got["wake"] is None else "set"
        exp_w = "none" if wk in ("none", "stop") else "set"
        if got["skills"] != sk or got["mcp"] != mc or len(got["crons"]) != cr or gw != exp_w:
            bad.append((os.path.basename(f), got["skills"], sk, got["mcp"], mc, len(got["crons"]), cr, gw, exp_w))
    check(not bad, f"{len(bad)}/{len(picked)} 件不一致: {bad[:2]}")
    return f"{len(picked)} 本の会話ログで全件一致"


@case("LP-02", "会話ログへの追記分だけを読み、書きかけの行は数えない")
def lp02(ctx):
    import overview as o
    tmp = os.path.join(ctx["data"], "append-test.jsonl")
    line = json.dumps({"type": "assistant", "timestamp": "2026-09-18T00:00:00Z", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Skill", "input": {"skill": "uat-skill"}}]}})
    with open(tmp, "w") as f:
        f.write(line + "\n")
    a = o.session_tools(tmp)["skills"].get("uat-skill", 0)
    with open(tmp, "a") as f:
        f.write(line)          # 改行なし = 書きかけ
    b = o.session_tools(tmp)["skills"].get("uat-skill", 0)
    with open(tmp, "a") as f:
        f.write("\n" + line + "\n")
    c = o.session_tools(tmp)["skills"].get("uat-skill", 0)
    check((a, b, c) == (1, 1, 3), f"回数 {a},{b},{c}(期待 1,1,3)")
    return "1 → 書きかけでも 1 → 確定後 3"


@case("LP-03", "cron の次の発火時刻")
def lp03(ctx):
    import overview as o
    import datetime as dt
    base = dt.datetime(2026, 9, 17, 10, 0).timestamp()   # 木曜 10:00
    f = lambda e: (lambda t: dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else None)(o.cron_next(e, base))
    cases = {"*/15 * * * *": "09-17 10:15", "0 9 * * 1": "09-21 09:00", "30 8 * * 1-5": "09-18 08:30",
             "0 12,18 * * *": "09-17 12:00", "8 15 17 9 *": "09-17 15:08", "0 9 16 9 *": None, "0 0 * * 0": "09-20 00:00", "0 0 * * 7": "09-20 00:00"}
    got = {e: f(e) for e in cases}
    bad = {e: (got[e], v) for e, v in cases.items() if got[e] != v}
    check(not bad, f"不一致 {bad}")
    return f"{len(cases)} 式すべて一致"


@case("LP-04", "/loop 待機中は「あなたの番」に数えない。判断待ちは loop 中でも数える")
def lp04(ctx):
    import overview as o
    base = {"state": "返答待ち", "state_for": 99999, "doing": ""}
    r = (len(o.attention([dict(base)])), len(o.attention([dict(base, loop={"wake": {"next_at": 1}})])),
         len(o.attention([dict(base, state="確認待ち", loop={"wake": {}})])))
    check(r == (1, 0, 1), f"{r}(期待 1,0,1)")
    return "放置の返答待ち 1 / loop 待機 0 / 判断待ち+loop 1"


@case("LP-05", "loop が止められた・次の起床から 15 分以上過ぎたら、ループ中と出さない")
def lp05(ctx):
    import overview as o
    now = time.time()
    fresh = o.loop_state({"wake": {"at": now - 60, "delay": 600, "reason": "r"}, "crons": []})
    stale = o.loop_state({"wake": {"at": now - 3600, "delay": 600, "reason": "r"}, "crons": []})
    stopped = o.loop_state({"wake": None, "crons": []})
    check(fresh and fresh.get("wake") and stale is None and stopped is None, f"{fresh} {stale} {stopped}")
    return "起床待ち=表示 / 45 分超過=非表示 / stop=非表示"


@case("LM-01", "上限の解除時刻の読み取り(3 形式)")
def lm01(ctx):
    import overview as o
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Tokyo")
    fmt = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M")
    got = [fmt(o.resets_epoch("4:10am (Asia/Tokyo)", "2026-09-17T15:00:00Z")),
           fmt(o.resets_epoch("Sep 14 at 6am (Asia/Tokyo)", "2026-09-10T00:00:00Z")),
           fmt(o.resets_epoch("Sep 22nd, 2026 4:49 PM (Asia/Tokyo)", ""))]
    exp = ["2026-09-18 04:10", "2026-09-14 06:00", "2026-09-22 16:49"]
    check(got == exp, f"{got} != {exp}")
    return " / ".join(got)


@case("LM-02", "実際の会話ログにある上限の記録を検出する")
def lm02(ctx):
    import overview as o
    found = None
    for f in sorted(glob.glob(HOME + "/.claude*/projects/*/*.jsonl") + glob.glob(HOME + "/.claude-profiles/*/projects/*/*.jsonl"), key=os.path.getmtime, reverse=True)[:400]:
        with open(f, "rb") as fh:
            fh.seek(max(0, os.path.getsize(f) - 400_000))
            if b'"error":"rate_limit"' in fh.read():
                found = f
                break
    if not found:
        return "SKIP: 直近 400 本に上限の記録が無い"
    lim = o.claude_limit(found)
    # 独立に: 最後の rate_limit 行の後に本物の返答があるか
    last_rl, after = None, False
    for line in open(found, encoding="utf-8", errors="replace"):
        if '"error":"rate_limit"' in line:
            last_rl, after = line, False
        elif last_rl and '"type":"assistant"' in line and '"model":"<synthetic>"' not in line and '"isApiErrorMessage":true' not in line:
            after = True
    exp_active = last_rl is not None and not after
    check((lim is not None) == exp_active, f"検出 {lim is not None} / 期待 {exp_active}")
    return f"検出={'あり' if lim else 'なし(その後に返答あり)'} kind={lim and lim['kind']} resets={lim and lim['resets']}"


@case("HK-01", "hook の設置: 既存の hook を残す・控えを取る・外すと自分の分だけ消える・壊れた JSON は触らない")
def hk01(ctx):
    d = tempfile.mkdtemp(dir=ctx["data"])
    p = os.path.join(d, "settings.json")
    other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}, "model": "x"}
    json.dump(other, open(p, "w"))
    run = lambda op: subprocess.run([sys.executable, os.path.join(BOARD, "install_hook.py"), op, p], capture_output=True, text=True)
    check(run("--check").returncode == 1, "未設置なのに installed")
    run("--install")
    s = json.load(open(p))
    check(run("--check").returncode == 0, "設置後に missing")
    check(any("echo other" in h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]), "既存 hook が消えた")
    check(s.get("model") == "x", "他の設定が消えた")
    check(glob.glob(p + ".aiboard-backup-*"), "控えが無い")
    run("--uninstall")
    s2 = json.load(open(p))
    check(s2 == other, f"外した後が元と違う: {s2}")
    open(p, "w").write("{broken")
    r = run("--install")
    check(r.returncode != 0 and open(p).read() == "{broken", "壊れた JSON を上書きした")
    return "5 条件すべて満たす"


@case("ST-03", "skill 数と MCP の接続状態が、独立に数えた値と一致する")
def st03(ctx):
    st, e, _ = http("/api/extensions?refresh=1")
    check(st == 200, f"status {st}")
    n_skill = sum(1 for d in glob.glob(HOME + "/.claude/skills/*") if os.path.exists(os.path.join(d, "SKILL.md")))
    check(len(e["skills"]) == n_skill, f"skill {len(e['skills'])} != {n_skill}")
    out = subprocess.run(["zsh", "-l", "-c", "command claude mcp list"], capture_output=True, text=True, timeout=90, cwd=HOME).stdout
    names = {}
    for line in out.splitlines():
        if ": " in line and (" - ✔" in line or " - ✘" in line or " - !" in line or " - ⊘" in line):
            nm = line.split(": ", 1)[0].strip()
            mark = line.split(" - ", 1)[1].strip()[:1] if " - " in line else ""
            names[nm] = {"✔": "ok", "✘": "failed", "!": "auth", "⊘": "disabled"}.get(mark)
    got = {k: v["status"] for k, v in e["health"].items()}
    diff = {k: (got.get(k), names.get(k)) for k in set(got) | set(names) if got.get(k) != names.get(k)}
    # 接続試験は毎回やり直すので、タイムアウト系の揺れ(ok↔auth)は 1 件まで許す
    check(len(diff) <= 1, f"不一致 {diff}")
    return f"skill {n_skill} / MCP {len(names)}(状態の揺れ {len(diff)} 件: {diff})"


# ------------------------------------------------------------------ 終了(止める)
@case("MM-04", "「終了」の送り先: 実セッションに対し試しのみ(dry)で、tty 上の claude/codex 本体 1 つに解決する")
def mm04(ctx):
    snap = snapshot()
    targets = [s for s in snap["sessions"] if s.get("sid") and s.get("ai") and s.get("tty") and not s["sid"].startswith("tty:")]
    check(targets, "対象になる実セッションが無い")
    ok_n, notes = 0, []
    for s in targets[:6]:
        st, d, _ = http("/api/stop", "POST", {"tab": s["tab"], "sid": s["sid"], "dry": True}, headers={"Origin": BASE.rstrip("/")})
        check(st == 200 and d.get("pid"), f"{s['tab']}: {st} {d}")
        ps = subprocess.run(["/bin/ps", "-o", "tty=,command=", "-p", str(d["pid"])], capture_output=True, text=True).stdout.strip()
        tty = s["tty"].replace("/dev/", "")
        check(ps.startswith(tty) and re.search(r"(^|\s|/)(claude|codex)(\s|$)|@openai/codex", ps), f"{s['tab']}: pid {d['pid']} は {ps[:80]}")
        ok_n += 1
    log = os.path.join(ctx["data"], "stop.log")
    check(os.path.exists(log) and '"dry": true' in open(log).read(), "stop.log に dry の記録が無い")
    return f"{ok_n} セッションで tty と本体が一致(シグナルは送っていない)"


@case("MM-05", "「終了」の実シグナル: 使い捨てプロセスに SIGTERM を送り、終わったことを確かめる")
def mm05(ctx):
    import overview_server as osv
    import cs
    fake_dir = tempfile.mkdtemp(dir=ctx["data"])
    fake = os.path.join(fake_dir, "claude")
    os.symlink("/bin/sleep", fake)
    p = subprocess.Popen([fake, "300"])
    real_procs = cs.processes
    try:
        osv.snapshot_cached = lambda max_age=None: {"sessions": [{"tab": "9-9", "sid": "uat-sid", "ai": "Claude", "tty": "/dev/ttys999"}]}
        def procs():
            d = real_procs()
            d[p.pid] = dict(d.get(p.pid, {"ppid": os.getpid(), "rss": 0}), tty="ttys999", cmd=f"{fake} 300")
            return d
        cs.processes = procs
        ok, msg, pid = osv.stop_session({"tab": "9-9", "sid": "uat-sid"})
        check(pid == p.pid, f"送り先 {pid} != {p.pid}")
        check(ok and "終了" in msg, f"{ok} {msg}")
        rc = p.wait(timeout=5)
        check(rc == -signal.SIGTERM, f"終了コード {rc}")
        ok2, msg2, _ = osv.stop_session({"tab": "9-9", "sid": "other-sid"})
        check(not ok2 and "止めなかった" in msg2, f"sid 不一致で {ok2} {msg2}")
        return f"SIGTERM で終了(rc={rc}) / sid 不一致は拒否"
    finally:
        cs.processes = real_procs
        if p.poll() is None:
            p.terminate()


# ------------------------------------------------------------------ 画面(ブラウザ)
def wait_js(pg, expr, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pg.evaluate(expr):
                return
        except Exception:
            pass
        pg.wait_for_timeout(300)
    raise Fail(f"待ったが成り立たない: {expr[:80]}")


def with_page(ctx, fn, url_q="", width=1440, height=900, route_extra=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": height})
        errs, blocked = [], []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        for pat in ("**/api/send", "**/api/stop", "**/api/resume", "**/api/go"):   # 実セッションへは何も送らない
            pg.route(pat, lambda route, req: (blocked.append((req.url.split("/api/")[1], req.post_data)), route.fulfill(status=200, content_type="application/json", body='{"ok": true, "reason": "uat-intercepted"}')))
        if route_extra:
            route_extra(pg)
        pg.goto(BASE + url_q)
        pg.evaluate("localStorage.setItem('tour_done','1')")
        pg.reload()
        wait_js(pg, "document.querySelectorAll('.card').length > 0", 60)
        pg.wait_for_timeout(1500)
        try:
            return fn(pg, errs, blocked)
        finally:
            b.close()


@case("I18N-01", "盤の固定文言(T('…'))すべてに英語訳がある")
def i18n01(ctx):
    r = subprocess.run(["node", os.path.join(ROOT, "scripts", "uat", "i18n_check.js"), os.path.join(BOARD, "overview.html")], capture_output=True, text=True)
    d = json.loads(r.stdout)
    check(r.returncode == 0, f"訳の無い文言 {len(d['missing'])} 件: {d['missing'][:6]}")
    return f"文言 {d['keys']} 件・訳の抜け 0 件"


@case("BD-01", "盤を開いてスクリプトエラーが出ない(日本語・英語・デモ)")
def bd01(ctx):
    out = []
    for q in ("?lang=ja", "?lang=en", "?demo=1&lang=en"):
        n = with_page(ctx, lambda pg, errs, bl: (pg.wait_for_timeout(3000), (errs, pg.evaluate("document.querySelectorAll('.card').length")))[1], q)
        check(not n[0], f"{q}: {n[0][:2]}")
        out.append(f"{q} cards={n[1]}")
    return " / ".join(out)


@case("BD-02", "「いま」のカードの数・状態の色が API の中身と一致する")
def bd02(ctx):
    def fn(pg, errs, bl):
        snap = pg.evaluate("board.snap()")
        cards = pg.evaluate("[...document.querySelectorAll('.card.live')].map(c => ({id: c.dataset.id, cls: c.className}))")
        live = [s for s in snap["sessions"] if s.get("sid") and s.get("ai") and not s["sid"].startswith("tty:")]   # 素のシェルは盤に出さない(AI のセッションだけ)
        exp = {}
        for s in live:
            lim = (s.get("limit") or {}).get("active")
            k = "limited" if lim else "turn" if s["state"] in ("確認待ち", "codex 停止") else ("loop" if (s.get("loop") or {}).get("wake") else "yourturn") if s["state"] in ("返答待ち", "codex 返答待ち") else "work"
            exp[s["sid"]] = k
        bad = [(c["id"][:8], c["cls"], exp.get(c["id"])) for c in cards if exp.get(c["id"]) and f"k-{exp[c['id']]}" not in c["cls"]]
        shown = {c["id"] for c in cards}
        missing = [sid[:8] for sid in exp if sid not in shown]
        return len(cards), len(exp), bad, missing
    n_cards, n_exp, bad, missing = with_page(ctx, fn, "?lang=ja")
    check(not bad, f"色の不一致 {bad[:3]}")
    check(not missing, f"カードに出ていないセッション {missing}")
    return f"カード {n_cards} 枚 / API のセッション {n_exp} 件 / 状態の色すべて一致"


@case("BD-03", "上のバーの数(あなたの番・作業中・メモリ合計)が API と一致する")
def bd03(ctx):
    def fn(pg, errs, bl):
        pg.wait_for_timeout(1000)
        snap = pg.evaluate("board.snap()")
        ui = pg.evaluate("[document.querySelector('#nTurn').textContent, document.querySelector('#nWork').textContent, document.querySelector('#nMem').textContent]")
        mb = sum(s.get("mem_mb") or 0 for s in snap["sessions"] if s.get("ai"))
        exp = [str(len(snap["attention"])), str(snap["counts"]["working"]), (f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB")]
        return ui, exp
    ui, exp = with_page(ctx, fn, "?lang=ja")
    check(ui == exp, f"画面 {ui} / API {exp}")
    return f"画面 {ui} = API {exp}"


@case("BD-04", "上のバーが 1024px 幅でもはみ出さない")
def bd04(ctx):
    res = []
    for w in (1440, 1280, 1024):
        r = with_page(ctx, lambda pg, errs, bl: pg.evaluate("(() => { const r = document.querySelector('#toolbar .row.main'); const last = [...r.children].filter(e => e.offsetParent).pop().getBoundingClientRect(); return [r.scrollWidth, r.clientWidth, Math.round(last.right), window.innerWidth]; })()"), "?lang=ja", width=w)
        check(r[0] <= r[1] + 1 and r[2] <= r[3], f"{w}px: scroll {r[0]} > client {r[1]} or right {r[2]} > {r[3]}")
        res.append(f"{w}px ok")
    return " / ".join(res)


@case("BD-05", "英語表示の固定文言に日本語が残っていない(利用者の依頼文は除く)")
def bd05(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnMem"); pg.wait_for_timeout(500)
        mem = pg.evaluate("[...document.querySelectorAll('#pBody .memsum, #pBody .legend, #pBody button, #pTitle')].map(e => e.textContent).join(' ')")
        pg.click("#btnSettings", timeout=5000); pg.wait_for_timeout(9000)   # メモリのパネルを開いたまま押せること
        sett = pg.evaluate("[...document.querySelectorAll('#pBody h3, #pBody .sub, #pBody button, #pBody .kvrow > span:first-child, #pBody .kvrow > span:last-child')].map(e => e.textContent).join(' ')")
        bar = pg.evaluate("document.querySelector('#toolbar .row.main').innerText + ' ' + [...document.querySelectorAll('#toolbar .row.main [title], #toolbar .row.main [placeholder]')].map(e => (e.title || '') + ' ' + (e.placeholder || '')).join(' ') + ' ' + [...document.querySelectorAll('.c-state, .c-when, .c-chip, .frame .fsum, #zoomctl')].map(e => e.textContent).join(' ')")
        return bar, mem, sett
    parts = with_page(ctx, fn, "?lang=en&demo=1")
    jp = re.compile(r"[぀-ヿ一-鿿]+")
    allowed = {"日本語"}   # 言語切替ボタンの言語名は、その言語で書く(慣例)
    found = {name: sorted(set(jp.findall(t)) - allowed)[:8] for name, t in zip(("toolbar+cards", "memory", "settings"), parts)}
    check(not any(found.values()), f"残っている日本語 {found}")
    return "上のバー・カード・メモリ・設定で 0 件"


@case("BD-06", "デモモードで、本物の顧客名・フォルダ名・ユーザー名・MCP 名が画面に出ない")
def bd06(ctx):
    secrets = set()
    try:
        for c in json.load(open(os.path.join(LIVE_DATA, "clients.json"))).get("clients", []):
            for k in ("label", "id"):
                if c.get(k) and len(str(c[k])) >= 3:
                    secrets.add(str(c[k]))
    except (OSError, ValueError):
        pass
    snap = snapshot()
    for s in snap["sessions"]:
        if s.get("project") and len(s["project"]) >= 4:
            secrets.add(s["project"])
    secrets.add(os.path.basename(HOME))
    st, e, _ = http("/api/extensions")
    for k in list((e.get("health") or {}).keys()) + [x["name"] for x in e.get("servers", [])]:
        if len(k) >= 4 and k not in ("github", "linear", "notion", "figma", "slack", "sentry", "stripe", "vercel", "docs", "drive"):
            secrets.add(k)
    def fn(pg, errs, bl):
        texts = [pg.evaluate("document.body.innerText + ' ' + [...document.querySelectorAll('[title]')].map(e => e.title).join(' ')")]
        pg.evaluate("(() => { const c = document.querySelector('.card.live'); if (c) board.select(c.dataset.id); })()"); pg.wait_for_timeout(2500)
        texts.append(pg.evaluate("document.querySelector('#panel').innerText"))
        pg.evaluate("board.closePanel()"); pg.click("#btnMem"); pg.wait_for_timeout(500)
        texts.append(pg.evaluate("document.querySelector('#panel').innerText"))
        pg.click("#btnSettings"); pg.wait_for_timeout(10000)
        texts.append(pg.evaluate("document.querySelector('#panel').innerText"))
        return "\n".join(texts)
    text = with_page(ctx, fn, "?demo=1&lang=ja")
    hits = sorted(s for s in secrets if s in text)
    check(not hits, f"出ている: {len(hits)} 件(例 {hits[:3]})")
    return f"照合 {len(secrets)} 語・一致 0 件(盤・会話・メモリ・設定)"


@case("BD-07", "「過去」に切り替えると 30 日分のカードが出る")
def bd07(ctx):
    def fn(pg, errs, bl):
        pg.click("[data-mode=history]")
        wait_js(pg, "document.querySelectorAll('.card.past').length > 0", 60)
        return pg.evaluate("document.querySelectorAll('.card.past').length"), errs
    n, errs = with_page(ctx, fn, "?lang=ja")
    check(n > 0 and not errs, f"past={n} errs={errs[:1]}")
    return f"過去カード {n} 枚"


@case("BD-08", "検索で絞ると、該当カードが残り他は薄くなる/消える")
def bd08(ctx):
    def fn(pg, errs, bl):
        cards = pg.evaluate("[...document.querySelectorAll('.card.live')].map(c => ({id: c.dataset.id, t: (c.querySelector('.c-title')||{}).textContent || ''}))")
        target = next((c for c in cards if len(c["t"]) >= 8), None)
        if not target:
            return None
        word = target["t"][:8]
        vis = "[...document.querySelectorAll('.card.live')].filter(c => !c.classList.contains('dimmed') && getComputedStyle(c).display !== 'none' && parseFloat(getComputedStyle(c).opacity) > 0.5).map(c => c.dataset.id)"
        before = pg.evaluate(vis)
        pg.fill("#q", word); pg.wait_for_timeout(1200)
        after = pg.evaluate(vis)
        return target["id"], word, len(before), after
    r = with_page(ctx, fn, "?lang=ja")
    if r is None:
        return "SKIP: 題名のあるカードが無い"
    tid, word, nb, after = r
    check(tid in after, f"検索語 {word!r} の元カードが残っていない")
    check(len(after) < nb or nb == 1, f"絞れていない {nb}→{len(after)}")
    return f"{nb} 枚 → {len(after)} 枚(元カードは残る)"


@case("CV-01", "カードを押すと会話ビューが開き、会話が表示される")
def cv01(ctx):
    def fn(pg, errs, bl):
        sid = pg.evaluate("(() => { const s = board.snap().sessions.find(x => x.sid && !x.sid.startsWith('tty:') && x.ai); return s && s.sid; })()")
        pg.evaluate(f"board.select({json.dumps(sid)})")
        wait_js(pg, "document.querySelectorAll('#cvLog .cv:not(.empty)').length > 0", 20)
        return pg.evaluate("[document.querySelectorAll('#cvLog .cv').length, !!document.querySelector('#sendIn'), document.querySelector('#goBtn').textContent]")
    n, has_in, label = with_page(ctx, fn, "?lang=ja")
    check(n > 0 and has_in, f"rows={n} input={has_in}")
    check(label in ("iTerm で開く", "右の端末で開く"), f"ボタン名 {label}")
    return f"会話 {n} 行・入力欄あり・ボタン「{label}」"


@case("CV-02", "日本語変換の確定 Enter では送らず、通常の Enter では 1 回だけ送る(送信は横取り)")
def cv02(ctx):
    def fn(pg, errs, bl):
        sid = pg.evaluate("(() => { const s = board.snap().sessions.find(x => x.sid && !x.sid.startsWith('tty:') && x.ai && !x.tab.startsWith('0-')); return s && s.sid; })()")
        pg.evaluate(f"board.select({json.dumps(sid)})")
        wait_js(pg, "!!document.querySelector('#sendIn')")
        js = """(kind) => { const i = document.querySelector('#sendIn'); i.value = 'uat テスト';
          if (kind === 'composing') i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true}));
          if (kind === 'webkit229') i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229, bubbles: true}));
          if (kind === 'afterend') { i.dispatchEvent(new CompositionEvent('compositionend', {data: 'テスト'})); i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }
          if (kind === 'plain') i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }"""
        res = {}
        for kind in ("composing", "webkit229", "afterend"):
            n0 = len(bl); pg.evaluate(js, kind); pg.wait_for_timeout(300); res[kind] = len(bl) - n0
        pg.wait_for_timeout(200)
        n0 = len(bl); pg.evaluate(js, "plain"); pg.wait_for_timeout(600); res["plain"] = len(bl) - n0
        return res, bl[-1] if bl else None, sid
    res, last, sid = with_page(ctx, fn, "?lang=ja")
    check(res == {"composing": 0, "webkit229": 0, "afterend": 0, "plain": 1}, f"送信回数 {res}")
    body = json.loads(last[1])
    check(body.get("text") == "uat テスト" and body.get("sid") == sid, f"送信内容 {body}")
    return f"確定 Enter 3 形式で 0 回 / 通常 Enter で 1 回(sid 一致・横取り済み)"


@case("MM-01", "メモリ一覧: 多い順に並び、合計が一致する")
def mm01(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnMem"); pg.wait_for_timeout(600)
        vals = pg.evaluate("[...document.querySelectorAll('.memrow .mr-main b')].map(b => parseInt(b.textContent.replace(/[^0-9]/g, '')))")
        tot = pg.evaluate("document.querySelector('.memsum b').textContent")
        snap = pg.evaluate("board.snap()")
        return vals, tot, snap
    vals, tot, snap = with_page(ctx, fn, "?lang=ja")
    check(vals == sorted(vals, reverse=True), f"並びが降順でない {vals}")
    mb = sum(s.get("mem_mb") or 0 for s in snap["sessions"] if s.get("ai") and s.get("sid"))
    exp = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"
    check(tot == exp, f"合計 {tot} != {exp}")
    return f"{len(vals)} 行・降順・合計 {tot}"


@case("MM-02", "「終了」は 1 回目では送らず、2 回目(4 秒以内)で 1 回だけ送る。4 秒過ぎると戻る")
def mm02(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnMem"); pg.wait_for_timeout(500)
        tab = pg.evaluate("document.querySelector('.memrow button[data-stop]').dataset.stop")
        sel = f".memrow button[data-stop='{tab}']"
        n0 = len(bl); pg.click(sel); pg.wait_for_timeout(300)
        first = (len(bl) - n0, pg.inner_text(sel))
        pg.wait_for_timeout(4400)
        expired = pg.inner_text(sel)
        pg.click(sel); pg.wait_for_timeout(200); pg.click(sel); pg.wait_for_timeout(800)
        second = len(bl) - n0
        return tab, first, expired, second, bl[-1] if bl else None, pg.evaluate("board.snap()")
    tab, first, expired, second, last, snap = with_page(ctx, fn, "?lang=ja")
    check(first[0] == 0 and "もう一度" in first[1], f"1 回目 {first}")
    check(expired == "終了", f"4 秒後 {expired}")
    check(second == 1 and last[0] == "stop", f"2 回目の送信 {second} {last}")
    body = json.loads(last[1])
    sid = next(s["sid"] for s in snap["sessions"] if s["tab"] == tab)
    check(body == {"tab": tab, "sid": sid}, f"送信内容 {body}")
    return "1 回目 0 件・4 秒で解除・2 回目 1 件(tab と sid 一致・横取り済み)"


@case("MM-03", "デモモードでは「終了」を押しても何も送らない")
def mm03(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnMem"); pg.wait_for_timeout(500)
        b = ".memrow button[data-stop]"
        n0 = len(bl); pg.click(b); pg.wait_for_timeout(200); pg.click(b); pg.wait_for_timeout(600)
        return len(bl) - n0, pg.inner_text("#memMsg")
    n, msg = with_page(ctx, fn, "?lang=ja&demo=1")
    check(n == 0, f"{n} 件送った")
    check(msg.strip() != "", "結果の表示が空(押した後に何も出ない)")
    return f"送信 0 件・表示「{msg}」"


@case("ST-01", "設定パネル: アカウント・hook・skill と MCP・盤の 4 区画が出て、アカウント数が API と一致")
def st01(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnSettings"); wait_js(pg, "document.querySelectorAll('#pBody .acct').length > 1", 60)
        wait_js(pg, "!/確認中/.test(document.querySelector('#extBox').innerText)", 60)
        return pg.evaluate("[[...document.querySelectorAll('#pBody h3')].map(h => h.textContent), document.querySelectorAll('.accts .acct').length]"), errs
    (heads, n), errs = with_page(ctx, fn, "?lang=ja")
    st, d, _ = http("/api/settings")
    check(len(heads) == 4 and n == len(d["logins"]) and not errs, f"見出し {heads} アカウント {n}/{len(d['logins'])} errs {errs[:1]}")
    return f"{heads} / アカウント {n}"


@case("LP-06", "ループ待機のカードは青で「次 HH:MM」を出す(応答を差し替えて確認)")
def lp06(ctx):
    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            for s in d.get("sessions", []):
                if s["state"] == "返答待ち" and not (s.get("limit") or {}).get("active"):
                    s["loop"] = {"wake": {"next_at": time.time() + 600, "reason": "uat"}}; s["_uat"] = 1; break
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)
    def fn(pg, errs, bl):
        pg.wait_for_timeout(1500)
        return pg.evaluate("[...document.querySelectorAll('.card.k-loop')].map(c => [c.querySelector('.c-state').textContent, c.querySelector('.c-when').textContent, c.classList.contains('yourturn')])")
    r = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    if not r:
        return "SKIP: 返答待ちのセッションが無い"
    st, when, yt = r[0]
    check(st == "ループ待機" and when.startswith("次 ") and not yt, f"{r[0]}")
    return f"「{st}」「{when}」・黄枠なし"


# ------------------------------------------------------------------ アプリ
@case("AP-01", "アプリがビルドできる(release)")
def ap01(ctx):
    r = subprocess.run(["swift", "build", "-c", "release"], cwd=ROOT, capture_output=True, text=True, timeout=900)
    check(r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
    return "Build complete"


def run_app_js(ctx, js, extra_env=None, wait="10"):
    out = os.path.join(ctx["data"], f"js-{time.time_ns()}.json")
    app_data = os.path.join(ctx["data"], "appdata")
    os.makedirs(app_data, exist_ok=True)
    for n in ("index.db", "clients.json"):
        if os.path.exists(os.path.join(LIVE_DATA, n)) and not os.path.exists(os.path.join(app_data, n)):
            os.symlink(os.path.join(LIVE_DATA, n), os.path.join(app_data, n))
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=ctx["data"], OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS=js, AIBOARD_JS_WAIT=wait, **(extra_env or {}))
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")], env=env, capture_output=True, text=True, timeout=120)
    check(os.path.exists(out), "アプリが結果を書かなかった")
    return json.load(open(out))


@case("AP-02", "アプリ内の確認ダイアログ: はい/いいえ・入力が JS に正しく返る(以前は黙って「いいえ」)")
def ap02(ctx):
    js = "return [confirm('UAT confirm'), prompt('UAT prompt', 'd'), window.AIBOARD === true, document.querySelectorAll('.card').length]"
    yes = run_app_js(ctx, js, {"AIBOARD_DIALOG_AUTO": "yes"})
    no = run_app_js(ctx, js, {"AIBOARD_DIALOG_AUTO": "no"})
    check(yes.get("ok") and yes["value"][:3] == [True, "d", True], f"yes: {yes}")
    check(no.get("ok") and no["value"][:2] == [False, None], f"no: {no}")
    check(yes["value"][3] > 0, f"アプリの盤にカードが無い {yes['value']}")
    log = open(os.path.join(ctx["data"], "dialog.log")).read()
    check("confirm answer=yes" in log and "confirm answer=no" in log, "dialog.log に記録が無い")
    return f"はい→{yes['value'][:2]} / いいえ→{no['value'][:2]} / 盤のカード {yes['value'][3]} 枚"


@case("AP-03", "アプリ内では、会話ビューの端末ボタンは常に「右の端末で開く」(iTerm を呼ばない)")
def ap03(ctx):
    js = """const s = board.snap().sessions.find(x => x.sid && !x.sid.startsWith('tty:') && x.ai);
      if (!s) return null; board.select(s.sid); await new Promise(r => setTimeout(r, 2500));
      const ctrl = [...document.querySelectorAll('#panel button, #panel .convhead, #panel .legend, #panel input')].map(e => (e.textContent || '') + ' ' + (e.placeholder || '') + ' ' + (e.title || '')).join(' ');
      return [s.tab, document.querySelector('#goBtn') && document.querySelector('#goBtn').textContent, ctrl.includes('iTerm')]"""
    r = run_app_js(ctx, js)
    check(r.get("ok") and r["value"], f"{r}")
    tab, label, iterm = r["value"]
    check(label == "右の端末で開く", f"tab {tab}: {label}")
    check(not iterm, "パネルの操作部分に iTerm の文字が出ている")
    return f"tab {tab} →「{label}」・操作部分に iTerm 表記なし"


@case("AP-04", "別の端末で動くセッションを右の端末で開く: 止まっていれば 終了→右で再開。作業中は確認→Esc→止まらなければ終了しない(送信は差し替え)")
def ap04(ctx):
    js = """const S = board.snap().sessions.filter(x => x.sid && !x.sid.startsWith('tty:') && x.ai && !x.tab.startsWith('0-'));
      const idle = S.find(x => !(x.mark === '🟢' || x.mark === '🟩')), busy = S.find(x => x.mark === '🟢' || x.mark === '🟩');
      const out = {}; const calls = [], sent = [];
      const realFetch = window.fetch;
      window.fetch = async (u, o) => {
        const url = String(u);
        if (url.includes('/api/stop') || url.includes('/api/send')) { calls.push([url.split('/api/')[1], JSON.parse(o.body)]); return new Response(JSON.stringify({ok: true, reason: 'uat'}), {headers: {'Content-Type': 'application/json'}}); }
        return realFetch(u, o); };
      board.setToApp(m => sent.push(m));
      if (idle) { await board.openInApp(idle); out.idle = {calls: calls.splice(0), sent: sent.splice(0), expect: {tab: idle.tab, sid: idle.sid, cwd: idle.cwd, ai: idle.ai}}; }
      if (busy) { await board.openInApp(busy); out.busy = {calls: calls.splice(0), sent: sent.splice(0), expect: {tab: busy.tab, sid: busy.sid}}; }
      return out;"""
    r = run_app_js(ctx, js, {"AIBOARD_DIALOG_AUTO": "yes"}, wait="10")
    check(r.get("ok"), f"{r}")
    v, notes = r["value"], []
    if "idle" in v:
        e = v["idle"]["expect"]
        check(v["idle"]["calls"] == [["stop", {"tab": e["tab"], "sid": e["sid"]}]], f"止まっている: 送信 {v['idle']['calls']}")
        rs = [m for m in v["idle"]["sent"] if m.get("type") == "resume"]
        exp_ai = "Codex" if e["ai"].startswith("Codex") else "Claude"
        check(len(rs) == 1 and rs[0]["id"] == e["sid"] and rs[0]["cwd"] == e["cwd"] and rs[0]["ai"] == exp_ai, f"止まっている: 再開 {rs}")
        notes.append("止まっている=終了 1→右で再開 1")
    if "busy" in v:
        e = v["busy"]["expect"]
        kinds = [c[0] for c in v["busy"]["calls"]]
        # 差し替えた応答では作業中のままなので、Esc は送るが終了も再開もしないのが正しい
        esc_ok = any(c[0] == "send" and c[1].get("key") == "esc" and c[1].get("sid") == e["sid"] for c in v["busy"]["calls"]) or any(m.get("type") == "send" and m.get("key") == "esc" for m in v["busy"]["sent"])
        check(esc_ok, f"作業中: Esc を送っていない {v['busy']}")
        check("stop" not in kinds and not any(m.get("type") == "resume" for m in v["busy"]["sent"]), f"作業中のまま終了/再開した {v['busy']}")
        notes.append("作業中=Esc 1・止まらないので終了も再開もしない")
    if not notes:
        return "SKIP: 別の端末で動くセッションが無い"
    return " / ".join(notes)


@case("AP-05", "起動すると右に端末が 1 枚開き、「右の端末で開く」の後はキーボードの入力先が端末になる")
def ap05(ctx):
    js = """document.querySelector('#q').focus(); await new Promise(r => setTimeout(r, 300));
      const before = document.activeElement && document.activeElement.id;
      window.webkit.messageHandlers.aiboard.postMessage({type: 'focus', tab: '0-1'}); await new Promise(r => setTimeout(r, 800));
      return before"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    check(len(r.get("panes", [])) >= 1 and r["panes"][0]["kind"] == "shell", f"端末 {r.get('panes')}")
    check(r.get("terminalVisible") is True, "右の端末が畳まれている")
    check(r.get("selected") == "0-1" and r.get("firstResponderIsTerminal") is True, f"選択 {r.get('selected')} / 入力先が端末 {r.get('firstResponderIsTerminal')}(直前の入力先 {r.get('value')})")
    return f"端末 {len(r['panes'])} 枚(shell)・表示中・盤の検索欄から端末へ入力先が移った"


@case("AP-06", "右の端末が生きていて打てる: 起動した端末に文字を送ると、その中のシェルが実行する")
def ap06(ctx):
    mark = os.path.join(ctx["data"], f"pane-mark-{time.time_ns()}.txt")
    js = """(async () => { const m = %s;
      window.webkit.messageHandlers.aiboard.postMessage({type: 'send', tab: '0-1', text: 'echo PANE_OK > ' + m, enter: true});
      await new Promise(r => setTimeout(r, 2500)); return m; })()""" % json.dumps(mark)
    r = run_app_js(ctx, "return " + js, wait="9")
    check(r.get("ok"), f"{r}")
    panes = r.get("panes") or []
    check(panes and panes[0].get("tty"), f"端末の tty が取れていない {panes}")
    check(os.path.exists(mark), f"端末の中のシェルが動いていない(印 {os.path.basename(mark)} ができない)。tty={panes[0].get('tty')}")
    check(open(mark).read().strip() == "PANE_OK", open(mark).read()[:80])
    return f"端末 {panes[0]['tty']} に送った echo が実行された(印のファイルができた)"


# ------------------------------------------------------------------ 実行
MANUAL = [
    ("MA-01", "日本語入力: アプリの会話ビューで「てすと」→変換→Enter で確定しても送られない。もう一度 Enter で送られる"),
    ("MA-02", "通知: 別アプリを前面にして、Claude が判断待ちになったら macOS 通知が出る。通知を押すとその端末が前に出る"),
    ("MA-03", "判断待ちのボタン: 実セッションが許可を求めた時、会話ビューの 1 / 2 / Esc が効く"),
    ("MA-04", "送信: 会話ビューから送った文が実セッションに届き、返答が会話に出る"),
    ("MA-05", "終了: 不要なセッションをメモリ一覧で 2 回押しして終了。端末は残り、盤から消え、「過去」から再開できる"),
    ("MA-06", "別アカウントで続き: 上限に当たったセッションで他アカウントのボタンを押し、続きが開く(ログイン済みアカウントで)"),
    ("MA-08", "端末へ移る: アプリの端末のカードで「右の端末で開く」を押した直後に、キー入力がそのまま右の端末に入る"),
    ("MA-09", "右の端末で開く(別の端末で動いていた会話): 押すと元の端末の AI が終わり、右の端末で同じ会話が続きから開いて、そのまま打てる"),
    ("MA-10", "起動: アプリを開くと右の端末にシェルが 1 枚あり、クリックせずにそのまま打てる(盤をクリックした後は、端末をクリックすれば打てる)"),
    ("MA-07", "アプリの更新: make_app.sh 後にアプリを再起動すると、盤サーバが新しい版に入れ替わる(/api/version の stamp が変わる)"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.join(HOME, "aiboard-private", "uat"))
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    data = tempfile.mkdtemp(prefix="aiboard-uat-")
    prepare(data)
    os.environ.update(OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1")
    sys.path.insert(0, BOARD)
    live_pid = os.path.join(LIVE_DATA, "server.pid")
    ctx = {"data": data, "live_pid_mtime": os.stat(live_pid).st_mtime if os.path.exists(live_pid) else None}
    print("data:", data)
    print(start_server(data).strip())
    snapshot()
    for fn in CASES:
        if only and fn.cid not in only:
            continue
        t0 = time.time()
        try:
            ev = fn(ctx) or ""
            status = "SKIP" if str(ev).startswith("SKIP") else "PASS"
        except Fail as e:
            status, ev = "FAIL", str(e)
        except Exception as e:
            status, ev = "ERROR", f"{type(e).__name__}: {e}"
        RESULTS.append({"id": fn.cid, "title": fn.title, "status": status, "evidence": str(ev)[:600], "sec": round(time.time() - t0, 1)})
        print(f"{status:5} {fn.cid} {fn.title} ({RESULTS[-1]['sec']}s)\n      {str(ev)[:300]}", flush=True)
    # 後片付け: 試験サーバは自分で止める(ポートで引いた overview_server.py --serve だけ)
    subprocess.run([sys.executable, os.path.join(BOARD, "overview_server.py"), "stop"], env=env_for_test(data), capture_output=True, text=True)
    os.makedirs(a.out, exist_ok=True)
    jpath = os.path.join(a.out, f"uat-{stamp}.json")
    json.dump({"at": stamp, "results": RESULTS, "manual": MANUAL, "data": data}, open(jpath, "w"), ensure_ascii=False, indent=2)
    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "ERROR", "SKIP")}
    md = [f"# AIBoard UAT {stamp}", "", f"自動 {len(RESULTS)} 件: " + " / ".join(f"{k} {v}" for k, v in counts.items()), "",
          "| ID | 確認すること | 結果 | 証跡 |", "|---|---|---|---|"]
    md += [f"| {r['id']} | {r['title']} | {r['status']} | {r['evidence'].replace('|', '/').replace(chr(10), ' ')[:220]} |" for r in RESULTS]
    md += ["", "## 手動(本人が実機で)", "", "| ID | 手順と期待 | 結果 |", "|---|---|---|"] + [f"| {i} | {t} | 未実施 |" for i, t in MANUAL]
    mpath = os.path.join(a.out, f"uat-{stamp}.md")
    open(mpath, "w").write("\n".join(md) + "\n")
    print("\n" + " / ".join(f"{k} {v}" for k, v in counts.items()))
    print("結果:", mpath)
    sys.exit(0 if counts["FAIL"] + counts["ERROR"] == 0 else 1)


if __name__ == "__main__":
    main()
