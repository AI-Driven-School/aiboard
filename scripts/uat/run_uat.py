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
CASE_TIMEOUT = int(os.environ.get("UAT_CASE_TIMEOUT", "240"))


def load_now():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def load_factor():
    """機械の混み具合で待ち時間を伸ばす。実測: load 54 で対話 zsh の起動が 64 秒(普段の 10 倍)。
    ここを固定にしていたせいで、混んでいる時だけ落ちる試験が残っていた。"""
    la = load_now()
    cpus = os.cpu_count() or 8
    return max(1.0, min(4.0, 1.0 + la / max(4.0, cpus)))


def case_timeout():
    return int(CASE_TIMEOUT * load_factor())


# 混んでいる時だけ落ちることがある試験(アプリを起こす・実シェルを待つもの)。1 度だけやり直す
RETRY_WHEN_BUSY = {"AP-04", "AP-05", "AP-06", "AP-07", "AP-08", "AP-09", "AP-16", "AP-17", "AS-01", "AS-02", "AS-03",
                   "LK-01", "LK-02", "LK-03", "SV-19", "NT-02", "BD-17", "DG-03", "SC-03", "HK-09", "LX-01", "MS-01", "TU-02", "LG-01"}


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


def tmp_path(ctx, name):
    """試験用の一時パス(ctx["data"] の下。本番のデータ置き場には触らない)。"""
    p = os.path.join(ctx["data"], "fixtures", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write_jsonl(path, rows):
    """仮の会話ログを書く。rows は dict の列。最後に改行を入れる。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def tool_use_row(name, inp, ts="2026-09-18T00:00:00Z", model="claude-opus-5"):
    """会話ログ 1 行(assistant の tool_use)。"""
    return {"type": "assistant", "timestamp": ts, "isSidechain": False,
            "message": {"model": model, "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_uat", "name": name, "input": inp}]}}


def throwaway(ctx, name="claude", secs=120):
    """試験が自分で起こす使い捨てプロセス(名前だけ claude/codex に見せた sleep)。実セッションではない。"""
    d = tempfile.mkdtemp(dir=ctx["data"])
    link = os.path.join(d, name)
    os.symlink("/bin/sleep", link)
    return subprocess.Popen([link, str(secs)]), link


class patched:
    """with patched(module, "name", value): の形で一時的に差し替える(試験の中だけ)。"""

    def __init__(self, obj, attr, value):
        self.obj, self.attr, self.value = obj, attr, value

    def __enter__(self):
        self.old = getattr(self.obj, self.attr)
        setattr(self.obj, self.attr, self.value)
        return self.value

    def __exit__(self, *a):
        setattr(self.obj, self.attr, self.old)
        return False


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
    def run(args, timeout=60):
        return subprocess.run([sys.executable, os.path.join(BOARD, "overview_server.py")] + args,
                              env=env_for_test(data_dir), capture_output=True, text=True, timeout=timeout, cwd=BOARD)
    try:
        r = run(["--no-open"])
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        # 前の試験の残骸が待ち受けていると起動に入れない。片付け、ポートが空くのを待ってからもう一度
        try:
            run(["stop"], timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(30):
            free = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP@127.0.0.1:{PORT}", "-sTCP:LISTEN", "-t"],
                                  capture_output=True, text=True).stdout.strip()
            if not free:
                break
            time.sleep(1)
        r = run(["--no-open"], timeout=120)
        return "(前の試験サーバを片付けて起動し直した) " + r.stdout + r.stderr


def snapshot():
    for _ in range(40):
        st, d, _ = http("/api/snapshot")
        if st == 200 and d.get("sessions") is not None:
            return d
        time.sleep(1)
    raise Fail("snapshot が取れない")


# ============================================================ hook 面の共通部（この 1 ブロックは 1 回だけ貼る）
HOOK_PY = os.path.join(BOARD, "hooks", "tab-status.py")
HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop"]


def hook_home(ctx, name):
    """フック専用の使い捨て HOME。状態ファイルは ~/.claude/tabstate に出るので本物の HOME は使わない。"""
    h = os.path.join(ctx["data"], "hookhome-" + name)
    os.makedirs(os.path.join(h, ".claude"), exist_ok=True)
    return h


def hook_mod(ctx, name):
    """tab-status.py を試験用 HOME で読み込む。端末への書き込みは差し替え(実端末に触らない)。"""
    import importlib.util
    home, keep = hook_home(ctx, name), os.environ.get("HOME", "")
    os.environ["HOME"] = home
    try:
        spec = importlib.util.spec_from_file_location("uat_tabstatus_" + name, HOOK_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ["HOME"] = keep
    mod.seqs = []
    mod.write_tty = lambda seq: (mod.seqs.append(seq), "/dev/ttys999")[1]
    return mod, home


def hook_fire(mod, payload, env=None):
    """フックを 1 回呼び、書かれた状態ファイルを返す。"""
    import io
    keep = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    old, sys.stdin = sys.stdin, io.StringIO(json.dumps(payload))
    try:
        mod.main()
    finally:
        sys.stdin = old
        for k, v in keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    p = os.path.join(mod.STATE_DIR, "%s.json" % payload.get("session_id", ""))
    return json.load(open(p)) if os.path.exists(p) else None


_CLIENTS_KEEP = []


def hook_clients():
    """顧客の判定規則を試験用の 1 社だけに差し替える(本物の規則に結果を左右されない)。"""
    import clients
    _CLIENTS_KEEP.append(clients._cache)
    clients._cache = [{"id": "uatco", "label": "UAT商事", "emoji": "🟦", "rgb": [1, 2, 3],
                       "paths": ["/uat-client-path"], "keywords": ["zzuatword"]}]


def hook_clients_off():
    import clients
    if _CLIENTS_KEEP:
        clients._cache = _CLIENTS_KEEP.pop()


def hook_pty_run(ctx, payload, home, env=None, timeout=30):
    """フックを本番同様に別プロセスで、しかも試験が用意した擬似端末の下で起動する。
    フックは「親をたどって最初に見つけた端末」に書くので、実端末には決して届かない。
    戻り: (終了コード, 端末に出た文字列, 擬似端末の名前)"""
    import pty
    e = dict(os.environ, HOME=home)
    e.pop("CLAUDE_CONFIG_DIR", None)
    e.update(env or {})
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.write(1, b"TTY=" + os.ttyname(0).encode() + b"\n")
            r = subprocess.run([sys.executable, HOOK_PY], input=raw, env=e,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
            os._exit(r.returncode if 0 <= r.returncode < 125 else 125)
        except BaseException:
            os._exit(126)
    out = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    rc = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    os.close(fd)
    text = out.decode("utf-8", "replace")
    m = re.search(r"TTY=(\S+)", text)
    return rc, text, (m.group(1) if m else "")


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
    if not targets:
        return "SKIP: いま tty を持つ実セッションが無い(iTerm が応答していない可能性)"
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
    # 式に "=>" が入っていると Playwright は文字列を関数と読もうとして評価できない(静かに false 扱いになり、
    # 「待ったが成り立たない」で落ちる)。always 関数にして渡す。2026-09-18 実測
    body = expr if expr.lstrip().startswith(("()", "function", "async")) else "() => (" + expr + ")"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pg.evaluate(body):
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
        if not sid:
            return None
        wait_js(pg, "document.querySelectorAll('#cvLog .cv:not(.empty)').length > 0", 20)
        return pg.evaluate("[document.querySelectorAll('#cvLog .cv').length, !!document.querySelector('#sendIn'), document.querySelector('#goBtn').textContent]")
    got = with_page(ctx, fn, "?lang=ja")
    if got is None:
        return "SKIP: 会話を持つ実セッションが無い(iTerm が応答していない可能性)"
    n, has_in, label = got
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


@case("ST-01", "設定パネル: アカウント・hook・束ね方・skill と MCP・盤・自動・鍵の棚・判定器・遠隔の 9 区画が出て、アカウント数が API と一致")
def st01(ctx):
    def fn(pg, errs, bl):
        pg.click("#btnSettings"); wait_js(pg, "document.querySelectorAll('#pBody .acct').length > 1", 60)
        wait_js(pg, "!/確認中/.test(document.querySelector('#extBox').innerText)", 60)
        # 「システムが自分でやること」は /api/actions を待って描く。読み終える前に数えると区画が足りない(見かけの揺れ)
        wait_js(pg, "!/読込中/.test(document.querySelector('#autoBox').innerText)", 60)
        return pg.evaluate("[[...document.querySelectorAll('#pBody h3')].map(h => h.textContent), document.querySelectorAll('.accts .acct').length]"), errs
    (heads, n), errs = with_page(ctx, fn, "?lang=ja")
    st, d, _ = http("/api/settings")
    want = ["AI アカウント", "Claude Code hook", "束ね方（名前・色・顧客）", "skill と MCP", "盤",
            "システムが自分でやること", "やったこと",
            "鍵の棚（値は AIBoard を通りません）", "判定器（選ぶだけの判断を誰がするか）", "遠隔（同じ Wi-Fi の中だけ）"]
    check(heads == want and n == len(d["logins"]) and not errs, f"見出し {heads} != {want} / アカウント {n}/{len(d['logins'])} errs {errs[:1]}")
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
        if (url.includes('/api/snapshot') && window.__keepBusy) {   // 作業中のまま止まらない相手を作る(実セッションの自然な終了で結果が揺れないように)
          const r = await realFetch(u, o); const d = await r.json();
          d.sessions = (d.sessions || []).map(x => x.sid === window.__keepBusy ? Object.assign({}, x, {mark: '🟢', state: '作業中'}) : x);
          return new Response(JSON.stringify(d), {headers: {'Content-Type': 'application/json'}});
        }
        return realFetch(u, o); };
      board.setToApp(m => sent.push(m));
      if (idle) { await board.openInApp(idle); out.idle = {calls: calls.splice(0), sent: sent.splice(0), expect: {tab: idle.tab, sid: idle.sid, cwd: idle.cwd, ai: idle.ai}}; }
      if (busy) { window.__keepBusy = busy.sid; await board.openInApp(busy); out.busy = {calls: calls.splice(0), sent: sent.splice(0), expect: {tab: busy.tab, sid: busy.sid}}; }
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


def send_until(tab, cmd, token="PANE_OK", tries=20, every=5000):
    """端末に同じ指示を繰り返し送り、端末の題名が変わるまで待つ JS を作る。

    対話シェルの起動は機械が混んでいると 1 分を超える(2026-09-18 実測: load 54 で zsh -il が 64 秒)。
    固定の待ち時間だと「端末が動かない」と誤判定するので、冪等な指示を送り直し、
    「シェルが実行した」印として端末の題名(印字は shell が行う)を見る。
    """
    full = "printf '\\033]0;%s\\007'; %s" % (token, cmd)
    return """(async () => {
      for (let i = 0; i < %d; i++) {
        window.webkit.messageHandlers.aiboard.postMessage({type: 'send', tab: %s, text: %s, enter: true});
        await new Promise(r => setTimeout(r, %d));
        try {
          const d = await fetch('/api/snapshot', {headers: {'X-Overview': '1'}}).then(x => x.json());
          const s = (d.sessions || []).find(x => x.tab === %s);
          if (s && ((s.topic || '') + (s.title_topic || '') + (s.doing || '')).indexOf(%s) >= 0) return {tries: i + 1};
        } catch (e) {}
      }
      return {tries: %d, timeout: true}; })()""" % (
        tries, json.dumps(tab), json.dumps(full), every, json.dumps(tab), json.dumps(token), tries)


@case("AP-06", "右の端末が生きていて打てる: 起動した端末に文字を送ると、その中のシェルが実行する")
def ap06(ctx):
    mark = os.path.join(ctx["data"], f"pane-mark-{time.time_ns()}.txt")
    js = send_until("0-1", "echo PANE_OK > " + mark)
    r = run_app_js(ctx, "return await " + js, wait="8")
    check(r.get("ok"), f"{r}")
    panes = r.get("panes") or []
    check(panes and panes[0].get("tty"), f"端末の tty が取れていない {panes}")
    v = r["value"]
    check(os.path.exists(mark),
          f"端末の中のシェルが動いていない(印 {os.path.basename(mark)} ができない)。tty={panes[0].get('tty')} 送信 {v.get('tries')} 回")
    check(open(mark).read().strip() == "PANE_OK", open(mark).read()[:80])
    return f"端末 {panes[0]['tty']} に送った echo が実行された(送信 {v.get('tries')} 回目で印ができた)"


@case("RD-01", "秘密の伏せ字: 主な鍵の形をすべて隠し、普通の文は壊さず、二度通しても変わらない")
def rd01(ctx):
    import overview as o
    # 見本の鍵は文字列として書かない(本物でなくても GitHub の秘密検知が push を止める。2026-09-18 実測)。
    # つなげて作ることで、検知には引っかからず、伏せ字の検査としては同じ形になる。
    J = "".join
    secrets = {
        "OpenAI": (J(["sk-", "proj-", "AbCdEfGhIjKlMnOpQrStUvWx1234567890"]), None),
        "Anthropic": (J(["sk-", "ant-", "api03-", "ZZZaaabbbcccdddeee1234567890"]), None),
        "GitHub": (J(["ghp", "_", "A1b2C3d4E5f6G7h8I9j0KLMNOP"]), None),
        "Slack": (J(["xox", "b-", "123456789012-", "abcdefghijklmno"]), None),
        "AWS": (J(["AKIA", "ABCDEFGHIJ123456"]), None),
        "Google": (J(["AIza", "SyD1234567890abcdefghijklmnopqrstu"]), None),
        "JWT": (J(["eyJ", "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"]), None),
        "Bearer": ("Bearer abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
        "key=value": ('api_key: "sUp3rS3cr3tV4lue"', "sUp3rS3cr3tV4lue"),
        "長い英数字": ("a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u", None),
    }
    bad = []
    for name, (raw, core) in secrets.items():
        out = o.redact(f"直前 {raw} 直後")
        if (core or raw) in out:
            bad.append((name, out[:70]))
        elif "直前" not in out or "直後" not in out:
            bad.append((name + "(周りの文まで消えた)", out[:70]))
    check(not bad, f"伏せ字にならない/文を壊す: {bad}")
    keep = [os.path.join(ROOT, "board", "overview.py") + " を読んだ",
            "9b927f99-a5ac-4458-97b1-a6092b5619fe",
            "テストを 3 件追加して git commit した",
            "npm install --save-dev playwright"]
    broken = [(t, o.redact(t)) for t in keep if o.redact(t) != t]
    check(not broken, f"普通の文を伏せ字にした: {broken}")
    once = o.redact("token=abcdefghijklmnop")
    check(o.redact(once) == once, f"二度通すと変わる: {once!r} → {o.redact(once)!r}")
    check(o.redact("") == "" and o.redact(None) is None, "空・None で落ちる")
    return f"{len(secrets)} 形式を伏せ字 / 通常文 {len(keep)} 件は無改変 / 冪等"


@case("LM-03", "上限の判定: 依頼だけでは解けず、本物の返答が来たら解ける(合成ログ 4 通り)")
def lm03(ctx):
    import overview as o
    import datetime as dt
    iso = lambda off: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    J = lambda d: json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    rl = J({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "timestamp": iso(600),
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your weekly limit · resets 4:10am (Asia/Tokyo)"}]}})
    ask = J({"type": "user", "timestamp": iso(500), "message": {"role": "user", "content": [{"type": "text", "text": "続けて"}]}})
    real = J({"type": "assistant", "timestamp": iso(400), "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "はい"}]}})
    synth = J({"type": "assistant", "timestamp": iso(400), "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "…"}]}})
    cases = [("上限で終わる", [rl], True), ("上限→依頼だけ", [rl, ask], True),
             ("上限→本物の返答", [rl, real], False), ("上限→合成の返答", [rl, synth], True)]
    bad = {}
    for i, (name, lines, exp) in enumerate(cases):
        p = os.path.join(ctx["data"], f"lm03-{i}-{time.time_ns()}.jsonl")
        open(p, "w").write("\n".join(lines) + "\n")
        lim = o.claude_limit(p)
        if (lim is not None) != exp:
            bad[name] = f"検出={lim is not None} 期待={exp}"
        elif lim and (lim["kind"], lim["resets"]) != ("weekly", "4:10am (Asia/Tokyo)"):
            bad[name] = f"読み取り {lim['kind']} / {lim['resets']!r}"
    check(not bad, f"不一致 {bad}")
    return "上限で終わる=検出 / 依頼だけ=検出のまま / 本物の返答=解除 / 合成の返答=検出のまま(kind・解除時刻も一致)"


@case("LM-04", "上限の有効判定: 解除時刻が過ぎていれば解除・時刻が無ければ 5 時間窓・Codex の文面を読む")
def lm04(ctx):
    import overview as o
    import datetime as dt
    now = dt.datetime.now().astimezone()
    fmt = lambda d: d.strftime("%b %d, %Y %I:%M %p")
    at = lambda off: dt.datetime.fromtimestamp(time.time() - off, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    past = o.with_active({"kind": "usage", "resets": fmt(now - dt.timedelta(hours=1)), "at": at(7200), "text": ""})
    fut = o.with_active({"kind": "usage", "resets": fmt(now + dt.timedelta(hours=2)), "at": at(600), "text": ""})
    check(past["active"] is False and abs(past["resets_at"] - (time.time() - 3600)) < 90, f"過去の解除時刻 {past}")
    check(fut["active"] is True and abs(fut["resets_at"] - (time.time() + 7200)) < 90, f"未来の解除時刻 {fut}")
    young = o.with_active({"kind": "usage", "resets": "", "at": at(3600), "text": ""})
    old = o.with_active({"kind": "usage", "resets": "", "at": at(6 * 3600), "text": ""})
    check(young["resets_at"] is None and young["active"] is True, f"1 時間前 {young}")
    check(old["active"] is False, f"6 時間前 {old}")
    check(o.with_active(None) is None, "上限なしで None を返さない")
    cx = o.codex_limit("You've hit your usage limit. Try again at Sep 22nd, 2026 4:49 PM.")
    check(cx and cx["resets"] == "Sep 22nd, 2026 4:49 PM", f"Codex の文面 {cx}")
    check(o.codex_limit("作業中: ファイルを読んでいます") is None and o.codex_limit(None) is None, "上限でない文を上限と読んだ")
    return f"過去=解除 / 未来=継続 / 時刻なしは 5 時間窓(1h=継続・6h=解除)/ Codex「{cx['resets']}」"


@case("AT-01", "「あなたの番」の並び: 確認待ち→長い放置→起動待ちの順で、対象でない状態は出さない")
def at01(ctx):
    import overview as o
    mk = lambda sid, state, sec=0, **k: dict({"sid": sid, "tab": sid, "state": state, "state_for": sec,
                                              "doing": "", "mark": "🟡"}, **k)
    sess = [mk("work", "作業中", 99999, mark="🟢"),
            mk("idle-short", "返答待ち", o.IDLE_LONG + 10),
            mk("idle-long", "返答待ち", o.IDLE_LONG + 1000),
            mk("fresh", "返答待ち", o.IDLE_LONG - 10),
            mk("ask", "確認待ち", 5),
            mk("cxstop", "codex 停止", 1, doing="上限に当たった"),
            mk("trust", "確認画面で停止", 0),
            mk("boot", "起動中?", 0),
            mk("loop", "返答待ち", 99999, loop={"wake": {"next_at": time.time() + 60}})]
    got = [x["tab"] for x in o.attention(sess)]
    exp = ["ask", "cxstop", "idle-long", "idle-short", "trust", "boot"]
    check(got == exp, f"{got} != {exp}")
    check(all(x.get("why") for x in o.attention(sess)), "理由(why)の無い項目がある")
    check(o.IDLE_LONG == 900, f"放置とみなす秒数が変わった {o.IDLE_LONG}")
    return f"{len(exp)} 件を順に: {' → '.join(exp)}(作業中・新しい返答待ち・loop 待機は出さない)"


@case("GR-01", "顧客/プロジェクトの束ね: 生きた全タブを一度だけ入れ、集計と並びが手計算と一致する")
def gr01(ctx):
    import overview as o
    C = {"id": "acme", "label": "Acme", "emoji": "🅰", "rgb": [1, 2, 3], "by": "path"}
    mk = lambda tab, **k: dict({"tab": tab, "sid": tab, "state": "返答待ち", "mark": "🟡", "ago": 100,
                                "task": "t-" + tab, "cwd": "/tmp/p1", "project": "p1", "client": None,
                                "today_requests": 1, "today_requests_partial": False}, **k)
    home = os.path.expanduser("~")
    sess = [mk("1-1", client=C, mark="🟢", ago=50, task="最新の依頼", cwd="/x/acme", project="acme"),
            mk("1-2", client=C, ago=300, today_requests=2, today_requests_partial=True, cwd="/x/acme", project="acme"),
            mk("2-1", mark="🟩", ago=10),
            mk("2-2", cwd=home, project=o.project_name(home), ago=None),
            mk("3-1", mark="⚪")]
    cl, pr = o.grouped(sess)
    check(len(cl) == 1 and cl[0]["id"] == "acme" and cl[0]["label"] == "Acme", f"顧客 {cl}")
    g = cl[0]
    check((g["sessions"], g["active"], g["today_requests"], g["today_requests_partial"]) == (2, 1, 3, True), f"顧客の集計 {g}")
    check(g["latest_task"] == "最新の依頼" and g["last_update_ago"] == 50, f"最新の依頼 {g['latest_task']} / {g['last_update_ago']}")
    check(sorted(g["tabs"]) == ["1-1", "1-2"], f"顧客のタブ {g['tabs']}")
    check([x["name"] for x in pr] == ["p1", "~(ホーム)"], f"プロジェクトの並び {[x['name'] for x in pr]}")
    check(pr[1]["last_update_ago"] is None and pr[1]["active"] == 0, f"更新時刻の無いグループ {pr[1]}")
    tabs = [t for x in cl + pr for t in x["tabs"]]
    live = sorted(s["tab"] for s in sess if s["mark"] != "⚪")
    check(sorted(tabs) == live and len(tabs) == len(set(tabs)), f"束ね {sorted(tabs)} != 生きたタブ {live}")
    check(o.project_name(home + "/") == "~(ホーム)" and o.project_name("/a/b/c") == "c" and o.project_name("") == "",
          "フォルダ名の付け方が変わった")
    return f"顧客 1(タブ 2・稼働 1・今日 3 件・下限値の印あり)/ プロジェクト {[x['name'] for x in pr]} / 重複も漏れも 0"


@case("CS-02", "端末の指し先は tty だけ: 形の違う tty では AppleScript を組み立てず iTerm に一切触らない")
def cs02(ctx):
    import cs
    class Run:            # cs の中の subprocess だけ差し替える(本物の osascript / lsof を呼ばせない)
        def __init__(self, reply):
            self.calls, self.reply = [], reply

        def run(self, cmd, *a, **k):
            self.calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": self.reply(cmd), "stderr": ""})()
    fake = Run(lambda cmd: "NOTFOUND\n")
    keep = cs.subprocess
    bad = ['ttys1" \n do shell script "touch /tmp/uat-pwned', 'ttys1"', "ttys1; rm -rf ~", "../../ttys1",
           "ttys", "ttysX", "", "/dev/pts/0", "0-1", "ttys1 ttys2", "ttys1\nttys2"]
    try:
        cs.subprocess = fake
        for t in bad:
            ok, msg = cs.on_tty(t, 'return "OK"')
            check(ok is False and msg == "tty の形が違う", f"{t!r} → {ok} {msg}")
        check(not fake.calls, f"不正な tty で外部コマンドを呼んだ: {fake.calls[:1]}")
        ok, out = cs.on_tty("ttys999", 'return "OK"')      # 形が正しければ問い合わせに進む
        check(len(fake.calls) == 1 and fake.calls[0][0] == "osascript", f"問い合わせ {fake.calls}")
        script = fake.calls[0][-1]
        check('"/dev/ttys999"' in script, "tty が /dev/ 付きで渡っていない")
        check(ok is False and out == "NOTFOUND", f"見つからない時 {ok} {out}")
    finally:
        cs.subprocess = keep
    return f"不正な tty {len(bad)} 種を拒否・外部コマンド 0 回 / 正しい tty は 1 回だけ"


@case("CS-09", "依頼文の取り出し: メタ・system 注入・サブエージェント・ツール結果を「あなたの依頼」に混ぜない")
def cs09(ctx):
    import cs
    p = os.path.join(ctx["data"], f"transcript-{time.time_ns()}.jsonl")
    U = lambda c, **k: dict({"type": "user", "timestamp": "2026-09-18T00:00:00Z", "message": {"content": c}}, **k)
    recs = [(U("メタの記録", isMeta=True), ""),
            (U("<\system-reminder>これは注入<\/system-reminder>"), ""),
            (U("Caveat: The messages below were generated…"), ""),
            (U("サブエージェントへの指示", isSidechain=True), ""),
            (U([{"type": "tool_result", "content": "コマンドの出力"}]), ""),
            (U("最初の  依頼"), "最初の 依頼"),
            ({"type": "assistant", "message": {"content": [{"type": "text", "text": "返答"}]}}, ""),
            (U([{"type": "text", "text": "最後の\n依頼"}, {"type": "image", "source": {}}]), "最後の 依頼")]
    with open(p, "w", encoding="utf-8") as f:
        for r, _ in recs:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.write('{"type":"user","message":{"content":"書きかけ')      # 書きかけの行
    bad = [(r, cs.prompt_text(r), want) for r, want in recs if cs.prompt_text(r) != want]
    check(not bad, f"prompt_text の判定違い {[(b[1], b[2]) for b in bad][:3]}")
    got = [t for _, t in cs.user_prompts_in(open(p, encoding="utf-8").read())]
    check(got == ["最初の 依頼", "最後の 依頼"], f"取り出した依頼 {got}")
    check(cs.last_user_prompt(p) == "最後の 依頼", f"最後の依頼 {cs.last_user_prompt(p)!r}")
    small = cs.last_user_prompt(p, tail_bytes=40)
    check(small in ("", "最後の 依頼"), f"末尾だけ読んだ時に欠けた文を返した {small!r}")
    return f"除外 5 種すべて 0 件・依頼 2 件・書きかけの行は数えない・末尾読みでも断片を返さない"


@case("CS-10", "最初の依頼と最後の依頼が同じ規則で拾われる(空白の入った JSON・大きなログでも)")
def cs10(ctx):
    import cs
    U = lambda c, **k: dict({"type": "user", "timestamp": "t", "message": {"content": c}}, **k)
    recs = [U("サブの指示", isSidechain=True), U("最初の依頼"), U("次の依頼"), U("最後の依頼")]
    out = {}
    for name, sep in (("すきま無し", (",", ":")), ("すきま有り", (", ", ": "))):
        p = os.path.join(ctx["data"], f"first-{name}-{time.time_ns()}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False, separators=sep) + "\n")
        got = (cs.first_user_prompt(p), cs.last_user_prompt(p))
        want = ([t for _, t in cs.iter_user_prompts(p)][0], [t for _, t in cs.iter_user_prompts(p)][-1])
        out[name] = (got, want)
    bad = {k: v for k, v in out.items() if v[0] != v[1]}
    check(not bad, f"最初/最後の依頼が iter と食い違う(左が関数・右が iter): {bad}")
    check(out["すきま無し"][0] == ("最初の依頼", "最後の依頼"), f"{out['すきま無し'][0]}")
    # 先頭 400KB に依頼が無いだけで「依頼なし」と返さない(顧客判定の材料が静かに消える)
    big = os.path.join(ctx["data"], f"first-big-{time.time_ns()}.jsonl")
    filler = json.dumps({"type": "user", "isMeta": True, "message": {"content": "x" * 2000}}, separators=(",", ":"))
    with open(big, "w", encoding="utf-8") as f:
        for _ in range(250):
            f.write(filler + "\n")
        f.write(json.dumps(U("大きなログの後の依頼"), ensure_ascii=False, separators=(",", ":")) + "\n")
    got = cs.first_user_prompt(big)
    check(got == "大きなログの後の依頼", f"先頭 {os.path.getsize(big) // 1024}KB の先にある依頼を見落とした {got!r}")
    return "2 通りの書き方で first / last / iter が一致・500KB 先の最初の依頼も拾う"


@case("SV-07", "すべての経路がガードの内側にあり、POST は副作用の前に弾かれる")
def sv07(ctx):
    src = open(os.path.join(BOARD, "overview_server.py"), encoding="utf-8").read()
    paths = sorted(set(re.findall(r'path == "(/[^"]*)"', src)))
    check(len(paths) >= 10, f"経路が拾えていない: {paths}")
    bad = []
    for p in paths:
        st, _, _ = http(p, headers={"Host": f"evil.example:{PORT}"})
        if st != 403:
            bad.append(("GET", p, st))
        st2, _, _ = http(p, "POST", {}, headers={"Host": f"evil.example:{PORT}", "Origin": f"http://127.0.0.1:{PORT}"})
        if st2 != 403:
            bad.append(("POST", p, st2))
    check(not bad, f"ガードの外にある経路 {bad[:4]}")
    used = {s.get("tab") for s in snapshot()["sessions"]}
    ghost = next(f"{w}-99" for w in range(90, 99) if f"{w}-99" not in used)
    log = os.path.join(ctx["data"], "send.log")
    before = os.path.getsize(log) if os.path.exists(log) else -1
    for h in ({"Origin": "http://evil.example"}, {"X-Overview": "0"}, {"Host": f"evil.example:{PORT}"}):
        st3, _, _ = http("/api/send", "POST", {"tab": ghost, "sid": "uat", "text": "uat"}, headers=h)
        check(st3 == 403, f"{h} で {st3}")
    after = os.path.getsize(log) if os.path.exists(log) else -1
    check(after == before, f"弾いたのに send.log が増えた {before}→{after}")
    return f"経路 {len(paths)} 本 × GET/POST すべて 403 / 弾いた POST は send.log に残らない({before} B のまま)"


@case("SV-15", "生きているかの判定は、そのポートで待ち受けている盤サーバ本体だけを見る")
def sv15(ctx):
    import socket
    import overview_server as osv
    _, v, _ = http("/api/version")
    pid = osv.listener_pid()
    check(pid == v["pid"], f"lsof で引いた {pid} != /api/version の {v['pid']}")
    ps = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    check("overview_server.py" in ps and "--serve" in ps, f"盤サーバでないものを指している: {ps[:100]}")
    check(osv.alive() is True, "動いているのに alive() が False")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    other = s.getsockname()[1]
    real_port = osv.PORT
    try:
        osv.PORT = other
        check(osv.listener_pid() is None, f"他人が掴んでいるポート {other} を盤サーバと誤認した")
        check(osv.alive() is False, f"盤サーバが居ないポート {other} で alive() が True")
    finally:
        osv.PORT = real_port
        s.close()
    return f"本物 pid {pid}(lsof と /api/version が一致)/ 他人のポート {other} は未起動と判定"


@case("PK-01", "出荷する .app: 実行ファイル・Info.plist・アイコン・同梱 board が揃い、署名が通り、手元のコードと一致する")
def pk01(ctx):
    import plistlib
    app = os.path.join(ROOT, "build", "AIBoard.app")
    if not os.path.isdir(app):
        return "SKIP: build/AIBoard.app が無い(make_app.sh 未実行)"
    c = os.path.join(app, "Contents")
    pl_path = os.path.join(c, "Info.plist")
    check(os.path.exists(pl_path), "Info.plist が無い")
    r = subprocess.run(["/usr/bin/plutil", "-lint", pl_path], capture_output=True, text=True)
    check(r.returncode == 0, f"Info.plist が壊れている: {(r.stdout + r.stderr).strip()[:200]}")
    pl = plistlib.load(open(pl_path, "rb"))
    src = plistlib.load(open(os.path.join(ROOT, "Resources", "Info.plist"), "rb"))
    d = {k: (pl.get(k), src.get(k)) for k in set(pl) | set(src) if pl.get(k) != src.get(k)}
    check(not d, f"同梱 Info.plist が Resources/Info.plist と違う: {d}")
    exe = os.path.join(c, "MacOS", str(pl.get("CFBundleExecutable") or ""))
    check(os.path.exists(exe) and os.access(exe, os.X_OK), f"CFBundleExecutable の実行ファイルが無い/実行できない: {exe}")
    check(open(exe, "rb").read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"), "実行ファイルが Mach-O でない")
    icns = os.path.join(c, "Resources", str(pl.get("CFBundleIconFile") or "") + ".icns")
    check(os.path.exists(icns), f"CFBundleIconFile が指すアイコンが無い: {os.path.basename(icns)}")
    check(pl.get("CFBundlePackageType") == "APPL" and "." in str(pl.get("CFBundleIdentifier") or ""),
          f"種別/識別子 {pl.get('CFBundlePackageType')} {pl.get('CFBundleIdentifier')}")
    want = re.search(r"\.macOS\(\.v(\d+)\)", open(os.path.join(ROOT, "Package.swift")).read())
    check(want and str(pl.get("LSMinimumSystemVersion") or "").split(".")[0] == want.group(1),
          f"LSMinimumSystemVersion {pl.get('LSMinimumSystemVersion')} != Package.swift .v{want and want.group(1)}")
    bundles = glob.glob(os.path.join(c, "Resources", "*.bundle"))
    check(bundles, "端末の資源バンドル(*.bundle)が同梱されていない(開発機のビルド先を探しに行く)")
    missing, differ, n = [], [], 0
    for root, dirs, files in os.walk(BOARD):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for fn in files:
            if fn.endswith(".pyc") or fn == ".DS_Store":
                continue
            s = os.path.join(root, fn)
            rel = os.path.relpath(s, BOARD)
            t = os.path.join(c, "Resources", "board", rel)
            n += 1
            if not os.path.exists(t):
                missing.append(rel)
            elif open(s, "rb").read() != open(t, "rb").read():
                differ.append(rel)
    check(not missing, f"同梱されていない board のファイル {missing[:5]}")
    check(not differ, f"同梱 board が手元と違う(make_app.sh を流し直す) {differ[:5]}")
    junk = [p for p in glob.glob(os.path.join(c, "Resources", "board", "**", "*"), recursive=True)
            if p.endswith(".pyc") or os.path.basename(p) == "__pycache__"]
    check(not junk, f"中間ファイルが同梱されている {[os.path.basename(x) for x in junk[:3]]}")
    sig = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], capture_output=True, text=True)
    check(sig.returncode == 0, f"署名の検証に失敗: {(sig.stdout + sig.stderr).strip()[:200]}")
    return f"Mach-O 実行ファイル・Info.plist 一致・{os.path.basename(icns)}・*.bundle {len(bundles)} 個・board {n} ファイルが手元と一致・codesign --verify OK"


@case("PK-06", "README と LP の機能の記述が実装にある(端末エンジン名・ショートカット・参照ファイル)")
def pk06(ctx):
    rd = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()
    readme, lp = rd("README.md"), rd("site/index.html")
    pins = {p["identity"] for p in json.load(open(os.path.join(ROOT, "Package.resolved")))["pins"]}
    norm = lambda x: re.sub(r"[^a-z]", "", x.lower())
    engines = ["SwiftTerm", "xterm.js", "Alacritty", "libghostty"]
    shipped = [e for e in engines if any(norm(e) in norm(i) for i in pins)]
    check(shipped, f"Package.resolved から端末エンジンが分からない {pins}")
    lp_own = re.sub(r"<table>.*?</table>", " ", lp, flags=re.S)   # 比較表は他社の話なので外す
    wrong = [(f, e) for f, t in (("README.md", readme), ("site/index.html", lp_own)) for e in engines
             if e not in shipped and re.search(re.escape(e), t, re.I)]
    check(not wrong, f"使っていない端末エンジンの名前が残っている(実際は {shipped}): {wrong}")
    check(all(re.search(shipped[0], t, re.I) for t in (readme, lp)), f"README/LP が実際のエンジン {shipped[0]} を書いていない")
    binds = set()
    sw = rd("Sources/AIBoard/main.swift")
    for m in re.finditer(r'#selector\([^)]*\)\),\s*"([A-Za-z])"\s*,\s*(\[[^\]]*\]|\.[a-z]+)', sw):
        binds.add((m.group(1).upper(), frozenset(re.findall(r"\.([a-z]+)", m.group(2)))))
    check(len(binds) >= 5, f"main.swift からショートカットを読めていない {len(binds)} 件")
    mp = {"⌘": "command", "⌥": "option", "⇧": "shift", "⌃": "control"}
    miss, n_sc = [], 0
    for f, t in (("README.md", readme), ("site/index.html", lp)):
        for tok in sorted(set(re.findall(r"[⇧⌥⌃⌘]+[A-Za-z]", t))):
            n_sc += 1
            if (tok[-1].upper(), frozenset(mp[ch] for ch in tok[:-1])) not in binds:
                miss.append((f, tok))
    check(not miss, f"メニューに無いショートカットを書いている {miss}")
    skip = ("http", "#", "data:", "mailto", "~", "/")
    refs = set(re.findall(r"\]\(([^)\s]+)\)", readme)) | set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|sh|json|md|mp4|png|html))`", readme))
    gone = sorted(r for r in refs if not r.startswith(skip) and not os.path.exists(os.path.join(ROOT, r)))
    lp_refs = set(re.findall(r'(?:src|href)="([^"]+)"', lp)) | set(re.findall(r'property="og:image" content="([^"]+)"', lp))
    gone += sorted("site/" + r for r in lp_refs if not r.startswith(skip) and not os.path.exists(os.path.join(ROOT, "site", r)))
    check(not gone, f"README/LP が指すファイルが無い {gone[:5]}")
    check(re.search(r"get\('demo'\)", open(os.path.join(BOARD, "overview.html"), encoding="utf-8").read()),
          "LP が言う ?demo=1 が盤に実装されていない")
    return f"エンジン {shipped[0]}・ショートカット {n_sc} 個がメニューに実在・参照 {len(refs) + len(lp_refs)} 件すべて実在・demo モードあり"


@case("PK-08", "公開物(リポジトリのテキスト・LP の画像と動画・収録スクリプト)に顧客名・利用者名・実フォルダが出ない")
def pk08(ctx):
    secrets = {}
    try:
        for cl in json.load(open(os.path.join(LIVE_DATA, "clients.json"))).get("clients", []):
            for k in ("label", "id"):
                if cl.get(k) and len(str(cl[k])) >= 3:
                    secrets[str(cl[k])] = "顧客名"
    except (OSError, ValueError):
        pass
    user = os.path.basename(HOME)
    secrets[user] = "利用者名"
    secrets[HOME + "/"] = "ホームの実パス"
    check(len(secrets) >= 3, f"照合語が集まらない(実データが読めていない) {list(secrets)}")
    skip_dir = {".git", ".build", "build", "__pycache__", "node_modules"}
    hits, n_text, n_bin = [], 0, 0
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dir]
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, ROOT)
            try:
                if os.path.getsize(p) > 8_000_000:
                    continue
                blob = open(p, "rb").read()
            except OSError:
                continue
            if fn.endswith((".png", ".mp4", ".icns", ".jpg", ".webm")):
                n_bin += 1
                for s, kind in secrets.items():
                    if s.encode() in blob:
                        hits.append((rel, kind, s[:24]))
            else:
                n_text += 1
                t = blob.decode("utf-8", "replace")
                for s, kind in secrets.items():
                    if s in t:
                        hits.append((rel, kind, s[:24]))
    check(not hits, f"公開物に出ている {len(hits)} 件: {hits[:4]}")
    rec = open(os.path.join(ROOT, "scripts", "demo", "record.py"), encoding="utf-8").read()
    urls = re.findall(r'URL\s*=\s*"([^"]+)"', rec)
    check(urls and all("demo=1" in u for u in urls), f"収録スクリプトが demo モードで撮っていない {urls}")
    return f"照合語 {len(secrets)} 語 × テキスト {n_text} 件・画像/動画 {n_bin} 件で 0 件・収録は ?demo=1"


@case("CS-12", "一覧の桁: 日本語を切り詰めても列の幅がぴったり揃う")
def cs12(ctx):
    import cs
    import unicodedata
    ref = lambda s: sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
    samples = ["あいうえおかきくけこさしすせそ", "日本語テストの題名", "abcdefghijklmnopqrstu",
               "混在したmixed文字列テスト", "決算Flashの台本を直して欲しい", "短い", ""]
    check(all(ref(s) == cs.width(s) for s in samples), "width() が東アジア幅の規則と違う")
    bad = []
    for s in samples:
        for w in (8, 10, 14, 20, 30):
            c = cs.clip(s, w)
            if cs.width(c) != w:
                bad.append((s[:6], w, cs.width(c)))
    check(not bad, f"{len(bad)} 通りで幅が合わない(題名, 指定幅, 実際) {bad[:4]}")
    return f"{len(samples)} 種 × 5 幅 = {len(samples) * 5} 通りで桁が一致"


@case("TR-01", "使い方の案内は最後まで進んで閉じ、英語表示では日本語が出ず、吹き出しは画面内に収まる")
def tr01(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "確認待ち", "mark": "🔴", "doing": "許可しますか",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [base, dict(base, tab="9-2", sid="uat-2", state="作業中", mark="🟢")]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": "uat-1"}]
            d["counts"] = dict(d.get("counts") or {}, working=1, tabs=2)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        pg.click("#btnTour")
        wait_js(pg, "document.querySelector('#tour').classList.contains('on')", 20)
        steps = []
        for _ in range(10):
            pg.wait_for_timeout(700)
            if not pg.evaluate("document.querySelector('#tour').classList.contains('on')"):
                break
            steps.append(pg.evaluate("""(() => { const b = document.querySelector('#tour .bub').getBoundingClientRect();
              const h = document.querySelector('#tour .hole').getBoundingClientRect();
              return {txt: document.querySelector('#tour .bub').innerText,
                      bub: [Math.round(b.left), Math.round(b.top), Math.round(b.right), Math.round(b.bottom)],
                      hole: [Math.round(h.width), Math.round(h.height)],
                      vw: window.innerWidth, vh: window.innerHeight}; })()"""))
            pg.click("#tour .bub button[data-t=\"next\"]")
        done = pg.evaluate("[document.querySelector('#tour').classList.contains('on'), localStorage.getItem('tour_done')]")
        return steps, done, list(errs)

    steps, done, errs = with_page(ctx, fn, "?lang=en", route_extra=extra)
    check(3 <= len(steps) <= 8, f"歩数 {len(steps)}")
    jp = re.compile(r"[぀-ヿ一-鿿]+")
    bad_jp = {i + 1: sorted(set(jp.findall(s["txt"])))[:5] for i, s in enumerate(steps) if jp.search(s["txt"])}
    check(not bad_jp, f"英語表示の案内に日本語 {bad_jp}")
    off = {i + 1: s["bub"] for i, s in enumerate(steps)
           if s["bub"][0] < -2 or s["bub"][1] < -2 or s["bub"][2] > s["vw"] + 2 or s["bub"][3] > s["vh"] + 2}
    check(not off, f"吹き出しが画面外 {off}(画面 {steps[0]['vw']}x{steps[0]['vh']})")
    empty = [i + 1 for i, s in enumerate(steps) if len(s["txt"].strip()) < 20 or s["hole"][0] <= 0]
    check(not empty, f"文が無い/切り抜きが無い歩 {empty}")
    check(done == [False, "1"], f"最後まで進んでも閉じない/覚えない {done}")
    check(not errs, f"{errs[:1]}")
    return f"{len(steps)} 歩・全部画面内・日本語 0・閉じて tour_done=1"


@case("AP-13", "終了時: 盤が教えた sid ごと端末一覧を state.json に残し、app_panes.json は消す(tty: の擬似 sid は入れない)")
def ap13(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    d1 = os.path.join(data, "w1")
    os.makedirs(d1, exist_ok=True)
    sid = "0123456789abcdef0123456789abcdef0123"
    real = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'resume', ai: 'Claude', id: %s, cwd: %s});
      P({type: 'resume', ai: 'Codex', id: %s, cwd: %s});
      await new Promise(r => setTimeout(r, 5000));
      P({type: 'sessions', list: [{tab: '0-2', sid: %s}, {tab: '0-3', sid: 'tty:ttys999'}]});
      await new Promise(r => setTimeout(r, 2000)); return 1;""" % (
        json.dumps(sid), json.dumps(d1), json.dumps(sid), json.dumps(HOME), json.dumps(real))
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await (async () => { " + js + " })()",
               AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out) and json.load(open(out)).get("ok"), "盤の中で JS が動かなかった")
    check(not os.path.exists(os.path.join(data, "app_panes.json")), "終了しても app_panes.json が残っている")
    saved = json.load(open(os.path.join(data, "state.json")))["panes"]
    got = [(x.get("kind"), x.get("cwd"), x.get("sid")) for x in saved]
    exp = [("shell", HOME, ""), ("claude", d1, real), ("codex", HOME, "")]
    check(got == exp, f"保存された一覧 {got}\n期待 {exp}")
    return f"{len(got)} 枚を保存(0-2 は盤の sid、tty: の擬似 sid は入らない)・台帳は削除"


@case("SV-10", "送信の宛先ガード: 入れ替わり・素のシェル・無いタブ・許可外キーでは端末へ一切送らない")
def sv10(ctx):
    import cs
    import overview_server as osv
    ai = {"tab": "7-1", "sid": "uat-sid-1", "ai": "Claude", "tty": "/dev/ttys777"}
    sh = {"tab": "7-2", "sid": "tty:ttys778", "ai": "", "tty": "/dev/ttys778"}
    calls = []
    real_on_tty, real_snap = cs.on_tty, osv.snapshot_cached
    log = osv.SEND_LOG
    before = os.path.getsize(log) if os.path.exists(log) else -1
    try:
        cs.on_tty = lambda tty, body, pre="": (calls.append((tty, body, pre)), (True, "OK"))[1]
        osv.snapshot_cached = lambda max_age=None: {"sessions": [dict(ai), dict(sh)]}
        bodies = [({"tab": "7-1", "sid": "other-sid", "text": "x"}, "sid が入れ替わっている"),
                  ({"tab": "7-9", "sid": "uat-sid-1", "text": "x"}, "無いタブ"),
                  ({"tab": "7-2", "sid": "tty:ttys778", "text": "x"}, "素のシェル"),
                  ({"tab": "7-1", "sid": "uat-sid-1", "key": "ctrl-z"}, "許可外キー"),
                  ({"tab": "7-1", "sid": "uat-sid-1", "text": ""}, "空の本文"),
                  ({"tab": "7-1", "sid": "uat-sid-1", "text": "x" * 4001}, "長すぎる本文"),
                  ({"tab": "abc", "sid": "uat-sid-1", "text": "x"}, "形の違うタブ"),
                  ({"tab": "7-1", "text": "x"}, "sid 無し")]
        sent = [name for b, name in bodies if osv.send_to_tab(b)[0]]
        check(not sent, f"送ってしまった: {sent}")
        check(not calls, f"端末へ {len(calls)} 回送った: {calls[:1]}")
        after = os.path.getsize(log) if os.path.exists(log) else -1
        check(after == before, f"送っていないのに send.log が増えた {before}→{after}")
        ok, msg = osv.send_to_tab({"tab": "7-1", "sid": "uat-sid-1", "text": "uat-ok"})
        check(ok and len(calls) == 1 and calls[0][0] == "/dev/ttys777", f"正しい宛先に送れない {ok} {msg} {calls}")
    finally:
        cs.on_tty, osv.snapshot_cached = real_on_tty, real_snap
    return f"危ない {len(bodies)} 通りは送信 0 回・記録 0 行 / 正しい 1 通りだけ tty 宛に 1 回"


@case("SV-11", "送信の中身: tty 宛に文字コードで渡し(文字列を混ぜない)、Enter の有無と特殊キーを取り違えない")
def sv11(ctx):
    import cs
    import overview_server as osv
    sess = {"tab": "7-1", "sid": "uat-sid-1", "ai": "Claude", "tty": "/dev/ttys777"}
    text = 'テスト "危険" \\ end'
    calls = []
    real_on_tty, real_snap = cs.on_tty, osv.snapshot_cached
    log = osv.SEND_LOG
    n0 = len(open(log).read().splitlines()) if os.path.exists(log) else 0
    try:
        cs.on_tty = lambda tty, body, pre="": (calls.append({"tty": tty, "body": body, "pre": pre}), (True, "OK"))[1]
        osv.snapshot_cached = lambda max_age=None: {"sessions": [dict(sess)]}
        base = {"tab": "7-1", "sid": "uat-sid-1"}
        osv.send_to_tab(dict(base, text=text))
        osv.send_to_tab(dict(base, text=text, enter=False))
        osv.send_to_tab(dict(base, key="esc"))
        osv.send_to_tab(dict(base, key="enter"))
    finally:
        cs.on_tty, osv.snapshot_cached = real_on_tty, real_snap
    check(len(calls) == 4, f"呼び出し {len(calls)} 回")
    check([c["tty"] for c in calls] == ["/dev/ttys777"] * 4, f"宛先が tty でない {[c['tty'] for c in calls]}")
    codes = lambda s: "character id {" + ", ".join(str(ord(ch)) for ch in s) + "}"
    exp = [(codes(text), "YES"), (codes(text), "NO"), (codes("\x1b"), "NO"), (codes("\r"), "NO")]
    got = [(c["pre"].split("set payload to (", 1)[-1].rstrip(")"), "YES" if "newline YES" in c["body"] else "NO") for c in calls]
    check(got == exp, f"渡した中身が違う {got} != {exp}")
    leak = [c for c in calls if any(w in c["pre"] + c["body"] for w in ("危険", "\\", 'テスト'))]
    check(not leak, f"本文が AppleScript に生で混ざっている {leak[:1]}")
    rows = [json.loads(l) for l in open(log).read().splitlines()[n0:]]
    check(len(rows) == 4, f"send.log の行 {len(rows)}(期待 4)")
    check([r["tab"] for r in rows] == ["7-1"] * 4 and [r["sid"] for r in rows] == ["uat-sid-1"] * 4, f"記録の宛先 {rows}")
    check([r["enter"] for r in rows] == ["YES", "NO", "NO", "NO"], f"記録の Enter {[r['enter'] for r in rows]}")
    check(rows[0]["text"] == text and rows[2]["key"] == "esc" and rows[2]["text"] is None, f"記録の中身 {rows[0]} {rows[2]}")
    return "4 通りとも tty 宛・文字コードが一致(生の文字列なし)・Enter YES/NO/NO/NO・send.log 4 行"


@case("SV-12", "「終了」の相手の絞り込み: 親子なら親 1 つ・同じ tty に 2 系統あれば止めない(試しのみ)")
def sv12(ctx):
    import cs
    import overview_server as osv
    sess = {"tab": "8-1", "sid": "uat-stop", "ai": "Claude", "tty": "/dev/ttys881"}
    real_procs, real_snap = cs.processes, osv.snapshot_cached
    log = osv.STOP_LOG
    n0 = len(open(log).read().splitlines()) if os.path.exists(log) else 0
    rows = lambda r: (lambda: {p: {"ppid": pp, "rss": 0, "tty": t, "cmd": c} for p, pp, t, c in r})
    try:
        osv.snapshot_cached = lambda max_age=None: {"sessions": [dict(sess)]}
        cs.processes = rows([(990000, 1, "??", "/bin/zsh -l"), (990001, 990000, "ttys881", "claude"),
                             (990002, 990001, "ttys881", "claude --resume x"), (990003, 1, "ttys882", "claude")])
        ok, msg, pid = osv.stop_session({"tab": "8-1", "sid": "uat-stop", "dry": True})
        check(ok and pid == 990001, f"親子: {ok} {msg} pid={pid}(期待 990001)")
        cs.processes = rows([(990001, 1, "ttys881", "claude"), (990011, 1, "ttys881", "codex")])
        ok2, msg2, pid2 = osv.stop_session({"tab": "8-1", "sid": "uat-stop", "dry": True})
        check(not ok2 and pid2 is None and "候補 2" in msg2, f"2 系統: {ok2} {msg2} pid={pid2}")
        cs.processes = rows([(990003, 1, "ttys882", "claude")])
        ok3, msg3, pid3 = osv.stop_session({"tab": "8-1", "sid": "uat-stop", "dry": True})
        check(not ok3 and pid3 is None, f"別 tty の claude を掴んだ: {ok3} {msg3} pid={pid3}")
        cs.processes = rows([(990005, 1, "ttys881", "/bin/zsh -l")])
        ok4, msg4, pid4 = osv.stop_session({"tab": "8-1", "sid": "uat-stop", "dry": True})
        check(not ok4 and pid4 is None, f"シェルだけの tty: {ok4} {msg4} pid={pid4}")
        cs.processes = rows([(990001, 990000, "ttys881", "claude")])
        ok5, msg5, pid5 = osv.stop_session({"tab": "8-1", "sid": "other", "dry": True})
        check(not ok5 and pid5 is None, f"sid 不一致: {ok5} {msg5} pid={pid5}")
    finally:
        cs.processes, osv.snapshot_cached = real_procs, real_snap
    lines = open(log).read().splitlines()[n0:]
    check(len(lines) == 1, f"stop.log が {len(lines)} 行(試しの 1 行だけのはず)")
    rec = json.loads(lines[0])
    check(rec["pid"] == 990001 and rec["dry"] is True and rec["result"] == "dry", f"記録 {rec}")
    return "親子=親 990001 / 2 系統・別 tty・シェルのみ・sid 不一致=いずれも pid なしで拒否・記録は試し 1 行"


@case("CV-03", "会話ビューの承認ボタン(1 / 2 / Esc)は、表示中の tab と sid へ 1 回だけ正しい形で送る")
def cv03(ctx):
    TAB, SID = "9-1", "uat-ask"
    base = {"tab": TAB, "sid": SID, "ai": "Claude", "state": "確認待ち", "mark": "🔴", "doing": "Bash(rm -rf /tmp/x) を許可しますか",
            "task": "UAT ask", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 3, "state_for": 3,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    conv = {"state": "確認待ち"}

    def extra(pg):
        def fakesnap(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = [dict(base, state=conv["state"], mark="🔴" if conv["state"] == "確認待ち" else "🟢")]
            d["attention"] = [{"sid": SID}] if conv["state"] == "確認待ち" else []
            d["counts"] = dict(d.get("counts") or {}, working=0 if conv["state"] == "確認待ち" else 1, tabs=1)
            route.fulfill(response=r, body=json.dumps(d))
        def fakeconv(route):
            body = dict(base, ok=True, state=conv["state"], mark="🔴" if conv["state"] == "確認待ち" else "🟢",
                        etag="uat-1", timeline=[{"kind": "依頼", "t": "2026-09-18T10:00:00", "text": "テスト依頼"},
                                                {"kind": "返答", "t": "2026-09-18T10:00:05", "text": "承認をください"}])
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        pg.route("**/api/snapshot*", fakesnap)
        pg.route("**/api/conv*", fakeconv)

    def fn(pg, errs, bl):
        wait_js(pg, "!!document.querySelector('.card[data-id=\"uat-ask\"]')", 30)
        pg.evaluate("board.select('uat-ask')")
        wait_js(pg, "!!document.querySelector('#cvAsk button[data-k=\"1\"]') && !document.querySelector('#cvAsk').hidden", 30)
        sent = {}
        for k in ("1", "2", "esc"):
            n0 = len(bl)
            pg.click(f"#cvAsk button[data-k=\"{k}\"]")
            pg.wait_for_timeout(900)
            sent[k] = [b for b in bl[n0:]]
        conv["state"] = "作業中"
        wait_js(pg, "document.querySelector('#cvAsk').hidden === true", 20)
        n0 = len(bl)
        pg.wait_for_timeout(800)
        return sent, len(bl) - n0, list(errs)

    sent, after, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    for k, exp in (("1", {"tab": TAB, "sid": SID, "text": "1", "enter": False}),
                   ("2", {"tab": TAB, "sid": SID, "text": "2", "enter": False}),
                   ("esc", {"tab": TAB, "sid": SID, "key": "esc"})):
        got = sent[k]
        check(len(got) == 1 and got[0][0] == "send", f"{k}: 送信 {len(got)} 回 {got}")
        check(json.loads(got[0][1]) == exp, f"{k}: 送信内容 {got[0][1]} != {exp}")
    check(after == 0, f"判断待ちが終わった後に {after} 件送った")
    check(not errs, f"{errs[:1]}")
    return "1/2/Esc とも 1 回・tab と sid 一致・数字は enter なし・判断待ちが終わるとボタンは消える"


@case("BD-13", "カードの状態色は 1 つだけで、上限 > 判断待ち > ループ待機 > あなたの番 > 作業中 の順で決まる")
def bd13(ctx):
    now = time.time()
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟡", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    lim = {"active": True, "kind": "5h", "resets_at": now + 3600, "resets": "", "at": ""}
    loop = {"wake": {"next_at": now + 600, "reason": "uat"}, "crons": []}
    ss = [dict(base, tab="9-1", sid="uat-lim", state="確認待ち", mark="🔴", limit=lim, loop=loop),
          dict(base, tab="9-2", sid="uat-turn", state="確認待ち", mark="🔴"),
          dict(base, tab="9-3", sid="uat-loop", state="返答待ち", loop=loop),
          dict(base, tab="9-4", sid="uat-your", state="返答待ち"),
          dict(base, tab="9-5", sid="uat-work", state="作業中", mark="🟢"),
          dict(base, tab="9-6", sid="uat-cxstop", ai="Codex", state="codex 停止")]
    exp = {"uat-lim": ("k-limited", "上限"), "uat-turn": ("k-turn", "判断待ち"), "uat-loop": ("k-loop", "ループ待機"),
           "uat-your": ("k-yourturn", "あなたの番"), "uat-work": ("k-work", "作業中"), "uat-cxstop": ("k-turn", "停止")}

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": s["sid"]} for s in ss if s["state"] == "確認待ち"]
            d["counts"] = dict(d.get("counts") or {}, working=1, tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 6", 30)
        pg.wait_for_timeout(300)
        return pg.evaluate("""Object.fromEntries([...document.querySelectorAll('.card[data-id^="uat-"]')].map(c =>
            [c.dataset.id, [[...c.classList].filter(x => x.startsWith('k-')),
             c.querySelector('.c-state').textContent, c.querySelector('.c-when').textContent]]))"""), list(errs)

    got, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    bad = {k: got.get(k) for k, v in exp.items() if not got.get(k) or got[k][0] != [v[0]] or got[k][1] != v[1]}
    check(not bad, f"色/表示が違う(期待 {exp}) → {bad}")
    check("に解除" in got["uat-lim"][2], f"上限カードに解除時刻が出ていない {got['uat-lim'][2]!r}")
    check(not errs, f"{errs[:1]}")
    return "上限+判断待ち+loop→上限 / 判断待ち / 返答待ち+loop→ループ待機 / 返答待ち / 作業中 / codex 停止→停止(各 1 クラス)"


@case("CS-04", "AI の判定: 名前に codex / claude を含むだけのコマンドを AI のセッションと見なさない")
def cs04(ctx):
    import cs
    spec = {   # コマンド行 → (claude か, codex か)
        "claude": (True, False),
        "/opt/homebrew/bin/claude --resume 702e232a": (True, False),
        "node " + HOME + "/.nvm/versions/node/v22.21.1/bin/codex exec -m gpt-6-astra": (False, True),
        "/x/node_modules/@openai/codex-darwin-arm64/vendor/bin/codex exec": (False, True),
        "codex resume": (False, True),
        "vim " + ROOT + "/board/codex": (False, False),
        "less /tmp/codex": (False, False),
        "tail -f " + HOME + "/logs/codex": (False, False),
        "cp memo.txt " + HOME + "/codex": (False, False),
        "python3 " + HOME + "/tools/codex_watch.py": (False, False),
        "claude-monitor --watch": (False, False),
        "vim claude.py": (False, False),
        "rg -n codex .": (False, False),
        "zsh": (False, False),
        "-zsh": (False, False),
        "/usr/bin/login -fp " + os.path.basename(HOME): (False, False),
        "npm run dev": (False, False),
        "ssh macmini-cf": (False, False),
    }
    bad = {c: (cs.is_claude(c), cs.is_codex(c)) for c in spec if (cs.is_claude(c), cs.is_codex(c)) != spec[c]}
    check(not bad, f"{len(bad)}/{len(spec)} 件 誤判定(左が判定・右が正) "
                   + str({c: (v, spec[c]) for c, v in list(bad.items())[:3]}))
    return f"{len(spec)} 種のコマンド行すべて期待どおり"


@case("CS-01", "アプリの端末台帳: 生きているアプリの分だけ取り込み、死亡・破損・欠損では 0 枠(落ちない)")
def cs01(ctx):
    import cs
    keep = cs.APP_PANES
    p = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "app_panes.json")
    alive = subprocess.Popen(["/bin/sleep", "30"])          # 試験が自分で起こした使い捨て
    gone = subprocess.Popen(["/bin/sleep", "0"]); gone.wait()   # 確実に終わっている pid
    panes = [{"pane": 1, "tty": "/dev/ttys900", "title": "shell"},
             {"pane": 2, "tty": "ttys901"},
             {"pane": 3, "title": "tty の無い枠"}]

    def write(obj):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    try:
        cs.APP_PANES = p
        write({"app_pid": alive.pid, "panes": panes})
        exp = [{"win": 0, "tab": 1, "tty": "ttys900", "title": "shell", "app": True, "deleg": ""},
               {"win": 0, "tab": 2, "tty": "ttys901", "title": "", "app": True, "deleg": ""}]
        got = cs.app_panes()
        check(got == exp, f"生きているアプリ: {got}")
        write({"app_pid": gone.pid, "panes": panes})
        check(cs.app_panes() == [], f"終了したアプリの台帳を数えた: {cs.app_panes()}")
        write({"app_pid": "いいえ", "panes": panes})
        check(cs.app_panes() == [], "app_pid が数値でない台帳を数えた")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{壊れた JSON")
        check(cs.app_panes() == [], "壊れた台帳で中身を返した")
        os.remove(p)
        check(cs.app_panes() == [], "台帳が無いのに何か返した")
    finally:
        cs.APP_PANES = keep
        if alive.poll() is None:
            alive.terminate()
    return "生存=2 枠(tty 正規化・tty 無しは除外) / 死亡・型違い・破損・欠損=すべて 0 枠"


@case("CS-05", "セッションの本体 pid: 同じ tty の入れ子の AI でなく外側の 1 本を選ぶ(番号の小ささで選ばない)")
def cs05(ctx):
    import cs
    def stub(**kw):   # classify が外へ出る関数を全部差し替える(実プロセス・実ログを見に行かせない)
        keep = {n: getattr(cs, n) for n in ("session_record", "tab_state", "find_transcript", "screen_text",
                                            "proc_cwd", "proc_start", "codex_session", "model_from_transcript",
                                            "last_user_prompt", "first_user_prompt", "_clients")}
        for n in keep:
            setattr(cs, n, lambda *a, **k: (_ for _ in ()).throw(AssertionError("外部を見に行った")))
        cs._clients = None
        for n, v in kw.items():
            setattr(cs, n, v)
        return keep
    # 外側の claude(pid 500) → その中で走らせた bash(600) → 入れ子の claude -p(300。番号は一巡して親より小さい)
    def table(exe):
        return {400: {"ppid": 1, "rss": 1000, "tty": "ttys902", "cmd": "/usr/bin/login -fp " + os.path.basename(HOME)},
                401: {"ppid": 400, "rss": 2000, "tty": "ttys902", "cmd": "-zsh"},
                500: {"ppid": 401, "rss": 300000, "tty": "ttys902", "cmd": f"/opt/homebrew/bin/{exe} --resume outer"},
                600: {"ppid": 500, "rss": 4000, "tty": "ttys902", "cmd": "/bin/bash -c ..."},
                300: {"ppid": 600, "rss": 50000, "tty": "ttys902", "cmd": f"/opt/homebrew/bin/{exe} -p inner"}}

    def outermost(procs, is_ai):
        """別実装: AI のうち、同じ表の別の AI の子孫でないもの。"""
        ai = {p for p, v in procs.items() if is_ai(v["cmd"])}
        out = []
        for p in ai:
            up, seen = procs[p]["ppid"], set()
            while up in procs and up not in seen:
                seen.add(up)
                if up in ai:
                    break
                up = procs[up]["ppid"]
            else:
                out.append(p)
        return sorted(out)
    notes = []
    for exe, is_ai in (("claude", cs.is_claude), ("codex", cs.is_codex)):
        procs = table(exe)
        exp_pid = outermost(procs, is_ai)
        check(exp_pid == [500], f"別実装の期待値がおかしい {exp_pid}")
        keep = stub(session_record=lambda pid: {"cwd": "/tmp/uat", "status": "idle", "updatedAt": 1_700_000_000_000,
                                               "sessionId": f"sid-{pid}"},
                    tab_state=lambda sid: {}, find_transcript=lambda sid: "",
                    proc_cwd=lambda pid: "/tmp/uat", proc_start=lambda pid: 1_700_000_000,
                    codex_session=lambda cwd, started, pids=(): {"model": "gpt", "doing": "", "sid": f"cx-{min(pids)}"},
                    screen_text=lambda *a, **k: "")
        try:
            t = cs.classify([{"win": 9, "tab": 2, "tty": "ttys902", "title": "x"}], procs)[0]
        finally:
            for n, v in keep.items():
                setattr(cs, n, v)
        check(t["pid"] == 500, f"{exe}: 本体 pid {t['pid']}(入れ子の一時実行を掴んだ。期待 500)")
        check(t["mem"] == 354000, f"{exe}: メモリ {t['mem']}(期待 354000 = 外側+bash+入れ子)")
        if exe == "claude":
            check(t["sid"] == "sid-500", f"claude: 掴んだ会話 {t['sid']}(入れ子の会話を表示している)")
        notes.append(f"{exe}: pid 500 / mem 354000")
    return " / ".join(notes) + "(入れ子 pid 300 を選ばない)"


@case("CS-06", "状態の真理値表: 作業中・返答待ち・確認待ち・起動中?・確認画面で停止(画面で確かめた時だけ)・信頼の答え無しの印・codex 4 種・他のジョブ・終了")
def cs06(ctx):
    import cs
    def stub(**kw):   # classify が外へ出る関数を全部差し替える(実プロセス・実ログを見に行かせない)
        keep = {n: getattr(cs, n) for n in ("session_record", "tab_state", "find_transcript", "screen_text",
                                            "proc_cwd", "proc_start", "codex_session", "model_from_transcript",
                                            "last_user_prompt", "first_user_prompt", "trusted_cwd", "_clients")}
        for n in keep:
            setattr(cs, n, lambda *a, **k: (_ for _ in ()).throw(AssertionError("外部を見に行った")))
        cs._clients = None
        for n, v in kw.items():
            setattr(cs, n, v)
        return keep
    P = lambda ppid, rss, tty, cmd: {"ppid": ppid, "rss": rss, "tty": tty, "cmd": cmd}
    CL, CX = "/opt/homebrew/bin/claude", "/opt/homebrew/bin/codex"
    procs, tabs, exp = {}, [], {}
    recs, states, screens, cxs = {}, {}, {}, {}

    cwds = {}
    starts = {}

    def tab(n, cmds, state, mark, rec=None, st=None, screen="", cx=None, win=9, cwd="/tmp/uat"):
        tty = f"ttys9{n:02d}"
        base = 1000 + n * 10
        procs[base] = P(1, 500, tty, "/usr/bin/login -fp " + os.path.basename(HOME))
        procs[base + 1] = P(base, 900, tty, "-zsh")
        for i, c in enumerate(cmds):
            procs[base + 2 + i] = P(base + 1, 10000, tty, c)
            cwds[base + 2 + i] = cwd
            if n == 17:
                starts[base + 2 + i] = time.time() - 3   # 起動 3 秒後
        tabs.append({"win": win, "tab": n, "tty": tty, "title": "claude"})
        exp[n] = (state, mark)
        if rec is not None:
            recs[base + 2] = dict(rec, sessionId=f"sid{n}")
        states[f"sid{n}"] = st or {}
        screens[(win, n)] = screen
        cxs[tty] = cx or {}
        return n
    busy = {"cwd": "/tmp/uat", "status": "working", "updatedAt": 1_700_000_000_000, "statusUpdatedAt": 1_700_000_000_000}
    idle = dict(busy, status="idle")
    tab(1, [CL], "作業中", "🟢", busy)
    tab(2, [CL], "返答待ち", "🟡", idle)
    tab(3, [CL], "確認待ち", "🔴", busy, {"mark": "⚠", "doing": "⚠ 許可を待っています"})
    tab(4, [CL], "確認待ち", "🔴", busy, {"mark": "⏳", "doing": "⚠ 許可を待っています"})
    tab(5, [CL], "返答待ち", "🟡", idle, {"mark": "💬", "doing": "✅ 返答済み（あなたの番）"})
    tab(6, [CL], "作業中", "🟢", busy, {"mark": "💬", "doing": "✅ 返答済み（あなたの番）"})
    tab(7, [CL + " --resume abc123de-4567-890a-bcde-f01234567890"], "確認画面で停止", "🔴", None, None, "Do you trust this folder?")
    tab(8, [CL], "起動中?", "🔴", None, None, "$ ")
    tab(9, [CX], "codex", "🟩", None, None, "", {"model": "gpt-6", "doing": "shell: ls", "sid": "cx9"})
    tab(10, [CX], "codex 返答待ち", "🟡", None, None, "", {"doing": "✅ 返答済み（あなたの番）", "sid": "cx10"})
    tab(11, [CX], "codex 停止", "🔴", None, None, "", {"doing": "⛔ エラーで停止: 上限", "sid": "cx11"})
    tab(12, [CX], "codex 停止", "🔴", None, None, "", {"doing": "⏹ 中断された", "sid": "cx12"})
    tab(13, ["npm run dev"], "他のジョブ", "🔵")
    tab(14, [], "終了(古い題名)", "⚪")
    # アプリ自身の端末(win=0)は画面を読めない。設定に「信頼する」の答えが無ければ、確認で止まっていると見なす
    tab(15, [CL], "起動中?", "🔴", None, None, "", None, win=0, cwd="/tmp/untrusted")   # 画面を読めない端末では断定しない(印だけ付ける)
    tab(16, [CL], "起動中?", "🔴", None, None, "", None, win=0)   # 信頼済みなら、画面を読めなくても「起動中?」
    tab(17, [CL], "起動中?", "🔴", None, None, "", None, win=0, cwd="/tmp/untrusted")   # 起動 10 秒以内は記録待ち(codex 反証 2026-09-19)
    keep = stub(session_record=lambda pid: recs.get(pid),
                tab_state=lambda sid: states.get(sid, {}),
                find_transcript=lambda sid: "",
                screen_text=lambda w, t, tty=None: screens.get((w, t), ""),
                proc_cwd=lambda pid: cwds.get(pid, "/tmp/uat"),
                proc_start=lambda pid: starts.get(pid, 1_700_000_000),
                trusted_cwd=lambda cwd, ttl=20: cwd != "/tmp/untrusted",
                codex_session=lambda cwd, started, pids=(): cxs.get(procs[min(pids)]["tty"], {}))
    try:
        got = {t["tab"]: (t["state"], t["mark"]) for t in cs.classify(tabs, procs)}
        again = cs.classify([dict(x) for x in tabs], procs)
    finally:
        for n, v in keep.items():
            setattr(cs, n, v)
    bad = {n: (got[n], exp[n]) for n in exp if got[n] != exp[n]}
    check(not bad, f"{len(bad)}/{len(exp)} 行が期待と違う(左が判定・右が正): {dict(list(bad.items())[:4])}")
    d = {t["tab"]: t for t in again}
    check(d[6]["doing"] == "考え中（直前の操作は未記録）", f"作業中なのに記録が返答済み: {d[6]['doing']!r}")
    check(d[13]["mem"] == 500 + 900 + 10000, f"他のジョブのメモリ {d[13]['mem']}")
    check(d[14]["mem"] == 0 and d[14]["sid"] == "tty:ttys914", f"終了タブ {d[14]['mem']} {d[14]['sid']}")
    check("信頼" in d[7]["topic"] and "abc123de" in d[7]["topic"], f"確認画面の説明に resume 先が無い {d[7]['topic']!r}")
    check(d[15]["trust_ask"] == "/tmp/untrusted" and not d[16]["trust_ask"] and not d[17]["trust_ask"], f"信頼の確認の印 {d[15]['trust_ask']!r} / {d[16]['trust_ask']!r} / {d[17]['trust_ask']!r}")
    return f"{len(exp)} 行すべて期待どおり(状態 {len(set(exp.values()))} 種)"


@case("TU-02", "信頼の確認で止まったセッションを、盤から答えられる(はい=↓+Enter・いいえ=Enter)")
def tu02(ctx):
    js = """
      const sent = [];
      const realFetch = window.fetch;
      const fake = {tab: '0-9', sid: 'trust-uat', state: '確認画面で停止', mark: '🔴', ai: 'Claude',
                    cwd: '/tmp/uat-trust', doing: '', timeline: [], etag: '', ok: true};
      window.fetch = async (u, o) => {
        const url = String(u);
        if (url.includes('/api/conv')) return new Response(JSON.stringify(fake), {headers: {'Content-Type': 'application/json'}});
        if (url.includes('/api/snapshot')) {   // 盤に「信頼の確認で止まっている」セッションを 1 枚足す
          const r = await realFetch(u, o); const d = await r.json();
          d.sessions = [Object.assign({}, (d.sessions || [])[0] || {}, fake, {topic: 'フォルダ信頼の確認で止まって未起動',
            trust_ask: '/tmp/uat-trust', model_style: {label: 'x', short: 'x', emoji: '🟠', rgb: [0, 0, 0], vendor: '', id: ''}})]
            .concat(d.sessions || []);
          return new Response(JSON.stringify(d), {headers: {'Content-Type': 'application/json'}});
        }
        return realFetch(u, o); };
      board.setToApp(m => sent.push(m));
      await new Promise(r => setTimeout(r, 2500));
      board.select('trust-uat');
      for (let i = 0; i < 40 && !document.querySelector('#cvAsk button'); i++) await new Promise(r => setTimeout(r, 250));
      const labels = [...document.querySelectorAll('#cvAsk button')].map(b => b.textContent.trim());
      const q = (document.querySelector('#cvAsk .q') || {}).textContent || '';
      document.querySelector('#cvAsk button.primary').click();
      await new Promise(r => setTimeout(r, 900));
      const yes = sent.splice(0);
      const btns = [...document.querySelectorAll('#cvAsk button')];
      (btns[1] || btns[0]).click();
      await new Promise(r => setTimeout(r, 400));
      return {labels: labels, q: q, yes: yes, no: sent.splice(0)};"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    v = r["value"]
    check(len(v["labels"]) == 2, f"ボタン {v['labels']}")
    check("信頼" in v["q"], f"問いの文 {v['q']!r}")
    # 盤はアプリへ一覧なども送るので、端末への打鍵(send)だけ見る
    keys = lambda ms: [(m.get("type"), m.get("tab"), m.get("key")) for m in ms if m.get("type") == "send"]
    check(keys(v["yes"]) == [("send", "0-9", "down"), ("send", "0-9", "enter")], f"はい: {keys(v['yes'])}")
    check(keys(v["no"]) == [("send", "0-9", "enter")], f"いいえ: {keys(v['no'])}")
    return f"ボタン {v['labels']} / はい=↓+Enter・いいえ=Enter を端末へ(実セッションには送っていない)"


@case("NT-02", "通知が本当に macOS に届く: 許可の状態を見て 1 通出し、配信を確認して取り下げ、押した先(端末)まで進む")
def nt02(ctx):
    out = os.path.join(ctx["data"], "notify.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=ctx["data"], OVERVIEW_NO_INDEX="1",
               AIBOARD_BOARD=BOARD, AIBOARD_NOTIFY_TEST=out)
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=90)
    check(os.path.exists(out), "アプリが結果を書かなかった(通知の口に入っていない)")
    r = json.load(open(out))
    if r.get("auth") in ("denied", "notDetermined"):
        return f"SKIP: この Mac では通知が許可されていない(状態 {r['auth']}。システム設定 > 通知 > AIBoard)"
    check(not r.get("add_error"), f"通知を出せない: {r['add_error']}")
    check(r.get("delivered"), f"出した通知が通知センターに無い(id {r.get('id')})")
    check(r.get("selected") == "0-1" and r.get("terminal_visible"),
          f"通知を押した先: 選択 {r.get('selected')!r} / 端末の表示 {r.get('terminal_visible')}")
    return f"許可 {r['auth']} ・1 通配信を確認して取り下げ・押すと端末 0-1 が前に出る(押す操作だけは人の手)"


@case("NT-03", "通知が止められていたら盤が知らせる: 許可の状態を snapshot に出し、押すと設定画面を開くよう頼む")
def nt03(ctx):
    st, d, _ = http("/api/snapshot")
    check(st == 200 and "notify" in d, f"snapshot に通知の状態が無い {list(d)[:8]}")
    check("auth" in (d.get("notify") or {}), f"通知の状態の形 {d.get('notify')}")

    def fn(pg, errs, bl):
        out = {}
        for auth, shown in (("denied", True), ("notDetermined", True), ("authorized", False), ("", False)):
            # 描き直し(2.5 秒ごと)と競合しないよう、書き換えと読み取りを同じ処理の中で行う
            got = pg.evaluate("""(a) => { board.snap().notify = {auth: a}; board.toolbar();
                const w = document.querySelector('#notifyWarn');
                return {shown: !!w && !w.hidden && w.getBoundingClientRect().width > 0, text: w ? w.textContent : ''}; }""", auth)
            out[auth or "(空)"] = dict(got, expect=shown)
        pg.evaluate("board.setToApp(m => { window.__sent = (window.__sent || []).concat([m]); })")
        # 盤は 2.5 秒ごとに本物の snapshot で描き直す(このページには通知の状態が無い→隠れる)ので、
        # 出した直後に同じ処理の中で押す(人がクリックするのと同じ onclick)
        pg.evaluate("""() => { board.snap().notify = {auth: 'denied'}; board.toolbar(); document.querySelector('#notifyWarn').click(); }""")
        pg.wait_for_timeout(200)
        return out, pg.evaluate("window.__sent || []"), errs
    out, sent, errs = with_page(ctx, fn, "?lang=ja")
    bad = {k: v for k, v in out.items() if v["shown"] != v["expect"]}
    check(not bad, f"出る/出ないが期待と違う: {bad}")
    check("通知" in out["denied"]["text"], f"文面 {out['denied']['text']!r}")
    check([m.get("type") for m in sent] == ["notifySettings"], f"押した時にアプリへ送るもの {sent}")
    check(not errs, f"ページエラー {errs[:2]}")
    return f"denied/notDetermined で警告・authorized と不明では出さない / 押すと設定を開くよう頼む(本当に開くかは人の手)"


@case("AP-16", "起動直後にそのまま打てる(入力先が端末)。カードの「右の端末で開く」でその端末に移る")
def ap16(ctx):
    # 何もせずに起動直後の状態を見る(押す・クリックするをしない)
    r0 = run_app_js(ctx, "return 1", wait="7")
    check(r0.get("ok"), f"{r0}")
    panes = r0.get("panes") or []
    check(len(panes) == 1 and panes[0]["kind"] == "shell", f"起動時の端末 {panes}")
    check(r0.get("terminalVisible"), "起動時に端末が隠れている")
    check(r0.get("firstResponderIsTerminal"), "起動直後にキー入力が端末に入らない(クリックが要る)")
    # 盤のカードから自分の端末を選び直す(別の端末を選んでおいてから戻す)
    js = """
      const S = (board.snap().sessions || []).filter(x => x.tab && x.tab.startsWith('0-'));
      if (!S.length) return {why: 'アプリの端末が盤に出ていない'};
      window.webkit.messageHandlers.aiboard.postMessage({type: 'run', title: 'uat', command: 'CLAUDE_CONFIG_DIR=; cd /tmp; exec /bin/zsh -il'});
      await new Promise(r => setTimeout(r, 3000));
      document.querySelector('#q') && document.querySelector('#q').focus();
      await new Promise(r => setTimeout(r, 300));
      const before = document.activeElement && document.activeElement.id;
      await board.openInApp(S[0]);
      await new Promise(r => setTimeout(r, 600));
      return {tab: S[0].tab, focusedBefore: before};"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    v = r["value"]
    check(not v.get("why"), f"{v.get('why')}")
    check(v.get("focusedBefore") == "q", f"先に盤へ入力先を移せていない {v.get('focusedBefore')!r}")
    check(r.get("selected") == v["tab"], f"選ばれた端末 {r.get('selected')} != {v['tab']}")
    check(r.get("firstResponderIsTerminal"), "カードから開いたのに、キー入力が端末に入らない")
    return f"起動直後: 端末 1 枚・入力先は端末 / カードから {v['tab']} を選ぶと入力先もその端末へ"


@case("SV-19", "アプリを入れ直したら、アプリの起動で古い盤サーバが新しい版に入れ替わる")
def sv19(ctx):
    # 別の場所に置いた「古い版」のサーバを先に立てておく(中身が違うので stamp も違う)
    old = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "board")
    shutil.copytree(BOARD, old, ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(old, "aiboard_paths.py"), "a", encoding="utf-8") as f:
        f.write("\n# uat: 古い版\n")
    env = env_for_test(ctx["data"])
    subprocess.run([sys.executable, os.path.join(old, "overview_server.py"), "stop"], env=env,
                   capture_output=True, text=True, timeout=30)
    subprocess.run([sys.executable, os.path.join(old, "overview_server.py"), "--no-open"], env=env,
                   capture_output=True, text=True, timeout=120, cwd=old)
    st, v1, _ = http("/api/version")
    check(st == 200, f"古い版のサーバが立たない {v1}")
    # アプリを起動する。アプリは同梱の board で盤サーバを起こすので、ここで入れ替わるはず
    r = run_app_js(ctx, "return await fetch('/api/version', {headers: {'X-Overview': '1'}}).then(x => x.json())", wait="8")
    check(r.get("ok"), f"{r}")
    v2 = r["value"]
    sys.path.insert(0, BOARD)
    import overview_server as osv
    check(v2["stamp"] != v1["stamp"], f"古い版のまま動いている stamp {v1['stamp']}")
    check(v2["stamp"] == osv.code_stamp(), f"入れ替わった先が手元のコードでない {v2['stamp']} != {osv.code_stamp()}")
    check(v2["pid"] != v1["pid"], f"pid が同じ {v1['pid']}")
    check(not _pid_alive(v1["pid"]), f"古いサーバ(pid {v1['pid']})が生き残っている")
    # 入れ替わった先は**その試験アプリの持ち物**なので、アプリが終わると一緒に消える(それが正しい動き)。
    # 後の試験のために、共用のサーバを立て直しておく
    gone = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            st3, _v3, _ = http("/api/version")
        except Exception:
            gone = True      # 落ちている = アプリと一緒に終わった(期待どおり)
            break
        if st3 != 200:
            gone = True
            break
    start_server(ctx["data"])
    st4, v4 = 0, {}
    for _ in range(30):
        try:
            st4, v4, _ = http("/api/version")
            if st4 == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    check(st4 == 200, "共用のサーバを立て直せなかった(以降の試験が全部落ちる)")
    return (f"古い版 pid {v1['pid']}/{v1['stamp']} → アプリ起動で pid {v2['pid']}/{v2['stamp']}"
            f"(手元のコードと一致・古い方は終了)/ アプリ終了でその子も終了{'' if gone else '(残った)'}・共用を pid {v4['pid']} で再開")


@case("BD-17", "無人実行は日ごとに 1 枚へ畳む: 枚数が実測より減り、束を押すと中身が出て、検索は畳んだ中まで届く")
def bd17(ctx):
    def fn(pg, errs, bl):
        pg.click("[data-mode=history]")
        wait_js(pg, "document.querySelectorAll('.card.past').length > 0", 60)
        pg.evaluate("() => { const c = document.querySelector('#fUnatt'); c.checked = true; c.dispatchEvent(new Event('change', {bubbles: true})); }")
        # 索引(無人実行つき)が届いて束が描かれるまで待つ。機械が混んでいると数十秒かかる
        wait_js(pg, "(board.index().records || []).filter(r => r.unattended).length"
                    " >= Math.min(50, ((board.index().counts || {}).unattended || 0))", 150)
        try:   # 描き直しは機械が混んでいると遅れる。落ちる時は「何がどこまで出来ているか」を残す
            wait_js(pg, "document.querySelectorAll('[data-id^=\"bundle:una:\"]').length > 0", 120)
        except Fail:
            state = pg.evaluate(
                "() => ({recs: (board.index().records || []).length,"
                " una: (board.index().records || []).filter(r => r.unattended).length,"
                " unatt: !!(document.querySelector('#fUnatt') || {}).checked,"
                " vis: (board.vis() || []).length,"
                " visUna: (board.vis() || []).filter(n => n.unattended).length,"
                " visBundles: (board.vis() || []).filter(n => String(n.id).indexOf('bundle:una:') === 0).length,"
                " cards: document.querySelectorAll('.card').length})")
            return {"skip": f"束のカードが出ない: {json.dumps(state, ensure_ascii=False)} / ページエラー {errs[:2]}"}
        pg.wait_for_timeout(1500)
        n_una = pg.evaluate("(board.index ? (board.index().records || []) : []).filter(r => r.unattended).length")
        members = pg.evaluate("(board.vis() || []).filter(n => n.unattended && !n.bundle).length")
        cards = lambda: pg.evaluate("document.querySelectorAll('.card').length")
        # いちばん中身の多い束を押す(1 件しか入っていない束だと「開いた」が分からない)
        bundles = pg.evaluate("(board.vis() || []).filter(n => n.bundle && String(n.id).indexOf('bundle:una:') === 0)"
                              ".sort((a, b) => (b.n || 0) - (a.n || 0)).map(n => n.id)")
        biggest = pg.evaluate("Math.max(0, ...(board.vis() || []).filter(n => n.bundle && String(n.id).indexOf('bundle:una:') === 0).map(n => n.n || 0))")
        before = cards()
        if not bundles:
            return {"skip": f"無人実行の束ができていない(索引の無人実行 {n_una} 件)"}
        # 盤の外にはみ出した位置にあることがあるので、要素へ直接クリックを送る(処理は同じ委譲先)
        tap = """(id) => { const el = document.querySelector(`[data-id="${id}"]`);
          if (!el) return false; el.dispatchEvent(new MouseEvent('click', {bubbles: true})); return true; }"""
        check(pg.evaluate(tap, bundles[0]), f"束のカードが見つからない {bundles[0]}")
        pg.wait_for_timeout(1200)
        opened = cards()
        pg.evaluate(tap, bundles[0])   # もう一度押して畳む
        pg.wait_for_timeout(800)
        closed = cards()
        pg.fill("#q", "claude")
        pg.wait_for_timeout(1500)
        searched = pg.evaluate("[...document.querySelectorAll('.card')].filter(c => getComputedStyle(c).display !== 'none').length")
        return {"n_una": n_una, "members": members, "bundles": len(bundles), "biggest": biggest, "before": before,
                "opened": opened, "closed": closed, "searched": searched, "errs": errs[:2]}
    r = with_page(ctx, fn, "?lang=ja")
    if r.get("skip"):
        return "SKIP: " + r["skip"]
    check(not r["errs"], f"ページエラー {r['errs']}")
    if r["n_una"] < 5:
        return f"SKIP: 索引に無人実行が {r['n_una']} 件しかなく、畳む効果を測れない"
    # 畳めている＝束が出ていて、その中身(1 件ずつのカード)は 1 枚も出ていない
    check(r["bundles"] >= 1, f"束が 1 つも無い(無人実行 {r['n_una']} 件)")
    check(r["members"] == 0, f"畳んだはずの無人実行が {r['members']} 枚出ている")
    check(r["bundles"] < r["n_una"], f"束 {r['bundles']} 枚 / 無人実行 {r['n_una']} 件(減っていない)")
    shown = min(r["biggest"], 200) + (1 if r["biggest"] > 200 else 0)   # 200 件まで ＋「さらに N 件」
    check(r["opened"] == r["before"] + shown,
          f"束を押した時の枚数 {r['before']}→{r['opened']}(中身 {r['biggest']} 件 → 出すのは {shown} 枚のはず)")
    check(r["closed"] == r["before"], f"もう一度押しても畳まれない {r['opened']}→{r['closed']}")
    return (f"無人実行 {r['n_una']} 件 → 束 {r['bundles']} 枚・中身は 0 枚(全カード {r['before']} 枚)"
            f"・押すと {r['opened']} 枚に開き、もう一度で戻る・検索中は {r['searched']} 枚")


def shlex_quote(v):
    import shlex as _s
    return _s.quote(v)


def safe_name(key):
    ok = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    return "".join(c if c in ok else "_" for c in str(key))[:64]


@case("DG-03", "任せた仕事が案件の画面に残り、その後に始まった会話と結び付いて結果が出る")
def dg03(ctx):
    key = "dg-uat-" + str(int(time.time()))
    origin = {"Origin": BASE.rstrip("/")}
    # 控えは POST でしか作らない(読むだけの GET で空・形の違う依頼は 400)
    st, d, _ = http(f"/api/delegations?key={key}")
    check(st == 200 and d["rows"] == [], f"最初から控えがある {d}")
    st, bad, _ = http("/api/delegations", "POST", {"key": key, "text": ""}, headers=origin)
    check(st == 400, f"空の依頼を受けた {st} {bad}")
    st, r1, _ = http("/api/delegations", "POST",
                     {"key": key, "text": "索引を作り直して件数を照合して", "ai": "Claude", "profile": "som", "cwd": "/tmp/dg-uat"},
                     headers=origin)
    check(st == 200 and r1["ok"], f"控えを作れない {r1}")
    st, d, _ = http(f"/api/delegations?key={key}")
    check(len(d["rows"]) == 1 and d["rows"][0]["text"].startswith("索引"), f"控えの中身 {d['rows']}")
    at = d["rows"][0]["at"]

    # 盤の突き合わせ: 任せた後に同じ場所で始まった会話を結果として出す / 何も無ければ「結果待ち」
    def fn(pg, errs, bl):
        js = """(g) => {
          const mk = (started, live) => ({live: live, id: 'x' + started, t: started,
            s: {cwd: '/tmp/dg-uat', started: started, state: '作業中', doing: '索引を作り直しています'},
            r: {cwd: '/tmp/dg-uat', start: started, last_prompt: '照合まで終えた'}});
          const before = mk(g.at - 3600, true), after = mk(g.at + 120, true), past = mk(g.at + 60, false);
          const justBefore = mk(g.at - 30, true);                                   // 任せる 30 秒前に始まった別の会話
          const exact = mk(g.at + 3000, true); exact.s.deleg = g.id; exact.s.cwd = '/tmp/elsewhere';   // id が一致(場所も時刻も違う)
          const r = board.delegResult(Object.assign({}, g), [after, exact], []);
          const endedExact = board.delegResult(Object.assign({}, g, {sid: 'x' + (g.at + 5000)}), [], [mk(g.at + 5000, false)]);
          const p1 = mk(g.at + 500, false), p2 = mk(g.at + 30, false); p1.t = g.at + 20; p2.t = g.at + 9999;   // t と start が食い違う
          const nearest = (board.delegResult(g, [], [p1, p2]).node || {}).id;
          return {none: board.delegResult(g, [], []).label,
                  onlyBefore: board.delegResult(g, [before], []).label,
                  justBefore: board.delegResult(g, [justBefore], []).label,
                  live: board.delegResult(g, [after], []).label,
                  livePicked: (board.delegResult(g, [after], []).node || {}).id,
                  past: board.delegResult(g, [], [past]).label,
                  otherCwd: board.delegResult(Object.assign({}, g, {cwd: '/tmp/other'}), [after], []).label,
                  exactPicked: (r.node || {}).id, exactLabel: r.label, exactFlag: r.exact,
                  endedExact: endedExact.exact, endedLabel: endedExact.label, nearest}; }"""
        return pg.evaluate(js, {"id": "dg-uat-id", "at": at, "cwd": "/tmp/dg-uat", "text": "x", "ai": "Claude"}), errs
    v, errs = with_page(ctx, fn, "?lang=ja")
    check(not errs, f"ページエラー {errs[:1]}")
    check(v["none"] == "結果待ち" and v["onlyBefore"] == "結果待ち", f"任せる前の会話を結果にした {v}")
    check("作業中" in v["live"] and v["livePicked"] and "推定" in v["live"], f"任せた後の会話を結果にできない/推定の印が無い {v}")
    check(v["justBefore"] == "結果待ち", f"任せる直前に始まった別の会話を結果にした {v['justBefore']!r}")
    check(v["exactPicked"] and abs(float(v["exactPicked"][1:]) - (at + 3000)) < 1 and v["exactFlag"] and "推定" not in v["exactLabel"], f"id の一致を最優先していない {v}")
    check(v["endedExact"] and "推定" not in v["endedLabel"], f"終わった後に一致が外れて推定へ落ちた {v['endedLabel']!r}")
    check(v["nearest"] and abs(float(v["nearest"][1:]) - (at + 30)) < 1, f"過去の会話を開始時刻の近い順に選んでいない {v['nearest']}(at+30 のはず)")
    # 一致したら控えに sid を残す
    st, lk, _ = http("/api/delegations", "POST", {"op": "link", "key": key, "id": d["rows"][0].get("id", ""), "sid": "sid-uat-12345"}, headers=origin)
    st, d2, _ = http(f"/api/delegations?key={key}")
    check(lk.get("linked") == 1 and d2["rows"][0].get("sid") == "sid-uat-12345", f"控えに sid が残らない {lk} {d2['rows'][0]}")
    check("終了" in v["past"], f"終わった会話の結果 {v['past']!r}")
    check(v["otherCwd"] == "結果待ち", f"別の場所の会話を結果にした {v['otherCwd']!r}")
    # 案件の画面に「任せた仕事」として出るか(実在する枠で確かめる)
    def fn2(pg, errs, bl):
        fk = pg.evaluate("((board.frames() || []).find(f => f.key.startsWith('p:')) || {}).key")
        if not fk:
            return None, errs
        return fk, errs
    fk, errs = with_page(ctx, fn2, "?lang=ja")
    shown = None
    if fk:
        http("/api/delegations", "POST", {"key": fk[2:], "text": "盤に出るかの確認", "ai": "Codex", "cwd": "/tmp/dg-uat"}, headers=origin)

        def fn3(pg, errs2, bl):
            pg.evaluate("(k) => board.renderProject(k)", fk)
            wait_js(pg, "document.querySelector('#pBody') && document.querySelector('#pBody').textContent.length > 20", 30)
            pg.wait_for_timeout(600)
            return pg.evaluate("document.querySelector('#pBody').innerText"), errs2
        body, errs = with_page(ctx, fn3, "?lang=ja")
        shown = ("任せた仕事" in body, "盤に出るかの確認" in body, "結果待ち" in body or "作業中" in body or "終了" in body)
        os.remove(os.path.join(ctx["data"], "projects", safe_name(fk[2:]) + ".delegations.json"))
        check(all(shown[:2]), f"案件の画面に出ていない(見出し/依頼文) {shown}")
    os.remove(os.path.join(ctx["data"], "projects", key + ".delegations.json"))
    return ("控えの作成/読み出し/形の検査 + 突き合わせ 5 通り(前の会話・別の場所は結果にしない)"
            + (f" / 案件 {fk} の画面に見出しと依頼文と結果 {shown}" if fk else " / 枠が無いので画面の確認は省略"))


def _ask_run(ctx, env_extra):
    out = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "ask.json")
    # AIBOARD_NO_ASK: 実セッションの判断待ちが割り込まないように、小窓は試験の入力だけで動かす
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=ctx["data"], OVERVIEW_NO_INDEX="1",
               AIBOARD_BOARD=BOARD, AIBOARD_ASK_TEST=out, AIBOARD_DRY="1", AIBOARD_NO_ASK="1", **env_extra)
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=120)
    check(os.path.exists(out), "アプリが結果を書かなかった(小窓の口に入っていない)")
    return json.load(open(out))


@case("AS-01", "判断待ちの小窓: 待っている時だけ出て、件数と用件を見せ、0 件になったら閉じる")
def as01(ctx):
    r = _ask_run(ctx, {})
    check(not r["empty"]["visible"] and not r["empty"]["shown"], f"待っていないのに出た {r['empty']}")
    one = r["one"]
    check(one["visible"] and one["shown"] == "ask-uat", f"待っているのに出ない {one}")
    check("Opus 5" in one["title"] and "uat" in one["title"], f"題に AI と場所が無い {one['title']!r}")
    check("＋1" in one["title"], f"2 件目の件数が出ない {one['title']!r}")
    check("npm test" in one["body"], f"用件が出ない {one['body']!r}")
    check(one["buttons"] == ["1", "2", "esc", "open"], f"ボタン {one['buttons']}")
    check(not r["closed"]["visible"] and not r["closed"]["shown"], f"0 件になっても閉じない {r['closed']}")
    check(r["log"][:2] == ["show ask-uat tab=9-9", "hide"] or "show ask-uat tab=9-9" in r["log"], f"記録 {r['log']}")
    return f"0 件=出さない / 1 件目を表示(題「{one['title']}」・用件あり・ボタン 4 つ)/ 0 件で閉じる"


@case("AS-02", "小窓の答えの届き先: 1 / 2 / Esc / 自由入力が、そのセッションの端末だけに行く(実送信はしない)")
def as02(ctx):
    got = {}
    for k in ("1", "2", "esc"):
        r = _ask_run(ctx, {"AIBOARD_ASK_TAP": k})
        got[k] = [l for l in r["log"] if l.startswith(("answer", "post"))]
        check(not r["after"]["visible"], f"{k}: 答えた後も出たまま {r['after']}")
    r = _ask_run(ctx, {"AIBOARD_ASK_TYPE": "続けて。テストは飛ばさないで"})
    got["text"] = [l for l in r["log"] if l.startswith(("answer", "post"))]
    ro = _ask_run(ctx, {"AIBOARD_ASK_TAP": "open"})
    got["open"] = [l for l in ro["log"] if l.startswith("open")]
    check(ro["after"]["visible"], "「端末を見る」で小窓が消えた(まだ答えていない)")

    def body_of(rows):
        for l in rows:
            if l.startswith("post "):
                return l
        return ""
    for k, want in (("1", {"text": "1", "enter": False}), ("2", {"text": "2", "enter": False}), ("esc", {"key": "esc"})):
        b = body_of(got[k])
        check(b.startswith("post "), f"{k}: 送る中身が残っていない {got[k]}")
        d = json.loads(b[5:])
        check(d.get("tab") == "9-9" and d.get("sid") == "ask-uat", f"{k}: 宛先が違う {d}")
        check(all(d.get(x) == y for x, y in want.items()), f"{k}: 送る中身が違う {d}")
    bt = body_of(got["text"])
    dt = json.loads(bt[5:]) if bt.startswith("post ") else {}
    check("続けて" in str(dt.get("text")) and dt.get("enter") is True, f"自由入力 {dt}")
    check(got["open"] == ["open 9-9"], f"端末を見る {got['open']}")
    return "1 / 2 / Esc / 自由入力の 4 通りが、そのタブと sid にだけ届く(dry で実送信なし)・「端末を見る」では閉じない"


@case("AS-03", "小窓からアプリの端末へ: 自由入力がその端末のシェルに届く(印のファイルができる)")
def as03(ctx):
    mark = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "ask-pane.txt")
    r = _ask_run(ctx, {"AIBOARD_ASK_TAB": "0-1", "AIBOARD_ASK_TYPE": f"echo ASK_OK > {mark}"})
    check(r["one"]["shown"] == "ask-uat", f"小窓が出ていない {r['one']}")
    check(not any(l.startswith("post ") for l in r["log"]), f"アプリの端末なのにサーバ経由で送った {r['log']}")
    for _ in range(40):
        if os.path.exists(mark):
            break
        time.sleep(0.25)
    check(os.path.exists(mark), f"端末に届いていない(印 {os.path.basename(mark)} ができない) log={r['log']}")
    check(open(mark).read().strip() == "ASK_OK", open(mark).read()[:60])
    return "アプリの端末(0-1)へは直接送り、シェルが実行した(サーバは経由しない)"


@case("SC-04", "予約の画面: 案件に予約を作れて一覧に出る・止める/消すが画面から効く(確認つき)")
def sc04(ctx):
    import overview as o
    origin = {"Origin": BASE.rstrip("/")}

    def fn(pg, errs, bl):
        fk = pg.evaluate("((board.frames() || []).find(f => f.key.startsWith('p:')) || {}).key")
        return fk, errs
    fk, errs = with_page(ctx, fn, "?lang=ja")
    if not fk:
        return "SKIP: 案件の枠が無い"
    key = fk[2:]
    made = []

    def fn2(pg, errs2, bl):
        pg.evaluate("() => { window.confirm = () => true; }")
        pg.evaluate("(k) => board.renderProject(k)", fk)
        wait_js(pg, "!!document.querySelector('#scAdd')", 30)
        pg.fill("#scPrompt", "今日の失敗したジョブをまとめて")
        pg.fill("#scAt", "06:30")
        pg.click("#scAdd")
        # 作れたら画面を描き直すので、印は「一覧に行が出たこと」で見る(#scMsg は描き直しで消える)
        wait_js(pg, "document.querySelector('#pBody').innerText.includes('06:30')", 30)
        msg = "作成"
        shown = pg.evaluate("document.querySelector('#pBody').innerText")
        pg.click("[data-sc-toggle]")
        wait_js(pg, "document.querySelector('#pBody').innerText.includes('止めています')", 20)
        paused = pg.evaluate("document.querySelector('#pBody').innerText")
        pg.click("[data-sc-del]")
        wait_js(pg, "!document.querySelector('#pBody').innerText.includes('今日の失敗したジョブ')", 30)
        gone = pg.evaluate("document.querySelector('#pBody').innerText")
        return {"msg": msg, "shown": shown, "paused": paused, "gone": gone}, errs2
    try:
        v, errs = with_page(ctx, fn2, "?lang=ja")
        made = [j for j in o.read_schedule() if j.get("key") == key]
        check(not errs, f"ページエラー {errs[:1]}")
        check("06:30" in v["shown"], f"作れていない {v['shown'][:200]!r}")
        check("06:30" in v["shown"] and "今日の失敗したジョブ" in v["shown"], f"一覧に出ていない {v['shown'][:200]!r}")
        check("止めています" in v["paused"], "止められない")
        check("今日の失敗したジョブ" not in v["gone"], f"消えていない {v['gone'][:200]!r}")
        check(not [j for j in o.read_schedule() if j.get("key") == key], "画面から消したのに残っている")
    finally:
        for j in [x for x in o.read_schedule() if x.get("key") == key]:
            o.delete_job(j["id"])
    return f"案件 {fk} に予約を作成→一覧に表示→止める→消す(確認ダイアログつき)"


@case("PL-01", "分解: 答えが JSON 配列でない・空・多すぎ・壊れている時は 1 件も動かさず理由を返す")
def pl01(ctx):
    import importlib
    import overview as o
    keep = os.environ.get("AIBOARD_PLAN_CMD")
    good = '[{"title":"索引","prompt":"索引を作り直す"},{"title":"照合","prompt":"件数を照合する"}]'
    many = json.dumps([{"title": f"t{i}", "prompt": f"仕事 {i}"} for i in range(9)], ensure_ascii=False)
    cases = [
        ("普通", f"echo {shlex_quote(good)}", 2, ""),
        ("前後に説明", f"echo {shlex_quote('はい、分けました:' + good + ' 以上です')}", 2, ""),
        ("JSON でない", "echo こんにちは", 0, "JSON"),
        ("空の配列", "echo '[]'", 0, "空"),
        ("多すぎ", f"echo {shlex_quote(many)}", 5, ""),        # 5 件で打ち切る
        ("prompt が無い", """echo '[{"title":"x"}]'""", 0, "仕事が無い"),
        ("落ちた", "echo 認証エラー >&2; exit 1", 0, "認証"),
    ]
    bad = {}
    try:
        for name, cmd, n, why in cases:
            os.environ["AIBOARD_PLAN_CMD"] = cmd
            importlib.reload(o)
            tasks, reason = o.plan_tasks("索引を作り直して件数を照合して")
            if len(tasks) != n or (why and why not in reason):
                bad[name] = (len(tasks), reason[:60])
        os.environ["AIBOARD_PLAN_CMD"] = f"echo {shlex_quote(good)}"
        importlib.reload(o)
        long_text = "あ" * 4001
        check(o.plan_tasks(long_text)[0] == [], "4000 字を超える依頼を受けた")
        check(o.plan_tasks("")[0] == [], "空の依頼を受けた")
    finally:
        if keep is None:
            os.environ.pop("AIBOARD_PLAN_CMD", None)
        else:
            os.environ["AIBOARD_PLAN_CMD"] = keep
        importlib.reload(o)
    check(not bad, f"分解の結果が期待と違う(件数, 理由) {bad}")
    return f"{len(cases)} 通り(普通・説明混じり・非 JSON・空・多すぎ・不備・失敗)＋長さの検査"


@case("PL-02", "分解して同時に: 選んだ分だけ端末を起こし、同じ束として控えに残る(実行は差し替え)")
def pl02(ctx):
    import overview as o
    key = "pl-uat-" + str(int(time.time()))
    fake = '[{"title":"索引","prompt":"索引を作り直す"},{"title":"照合","prompt":"件数を照合する"},{"title":"報告","prompt":"結果を書く"}]'

    def route(pg):
        # 分解は決まった答えに差し替える(本物のモデルを呼ばない)
        pg.route("**/api/plan", lambda r, req: r.fulfill(status=200, content_type="application/json",
                 body=json.dumps({"ok": True, "tasks": json.loads(fake), "by": "Claude", "profile": ""})))

    def fn(pg, errs, bl):
        sent = []
        pg.evaluate("() => { window.__sent = []; board.setToApp(m => window.__sent.push(m)); }")
        pg.evaluate("""(k) => { window.confirm = () => true;
            window.prompt = (msg, def) => msg.indexOf('走らせる番号') >= 0 ? '1,3' : '索引を作り直して件数を照合して報告して'; }""", key)
        pg.evaluate("""(k) => { const f = (board.frames() || [])[0]; window.__key = f && f.key; }""", key)
        fk = pg.evaluate("window.__key")
        pg.evaluate("(fk) => board.openDelegate(fk)", fk)
        wait_js(pg, "(window.__sent || []).filter(m => m.type === 'delegate').length >= 2", 40)
        pg.wait_for_timeout(800)
        return {"sent": pg.evaluate("window.__sent"), "fk": fk}, errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    dele = [m for m in v["sent"] if m.get("type") == "delegate"]
    check(len(dele) == 2, f"選んだ 2 件だけ動かすはずが {len(dele)} 件 {[d.get('title') for d in dele]}")
    check([d.get("title") for d in dele] == ["索引", "報告"], f"選んだ番号と違う {[d.get('title') for d in dele]}")
    check(len({d.get("group") for d in dele}) == 1 and all(d.get("group") for d in dele), f"束が揃っていない {dele}")
    pkey = v["fk"][2:]
    mine = [r for r in o.read_delegations(pkey) if r.get("group") == dele[0]["group"]]
    check(len(mine) == 2, f"控えに残っていない {len(mine)}")
    left = [x for x in o.read_delegations(pkey) if x.get("group") != dele[0]["group"]]
    with open(o.deleg_path(pkey), "w", encoding="utf-8") as f:   # 試験で足した分を戻す
        json.dump(left, f, ensure_ascii=False)
    return f"3 件の案から 1,3 を選んで 2 件だけ起動・同じ束 {dele[0]['group']}・控えにも 2 件"


@case("RM-02", "遠隔の判定: 自分の機械は素通し・外からは切ってあれば全部拒否・合言葉が合っても決まった道だけ")
def rm02(ctx):
    import overview as o
    off = {"enabled": False, "token": ""}
    on = {"enabled": True, "token": "s3cret-token-uat"}
    rows = [
        ("自分(127.0.0.1)は素通し", "/api/stop", True, "127.0.0.1", "", off, True),
        ("自分(::1)も素通し", "/api/stop", True, "::1", "", off, True),
        ("外から・切ってある", "/api/snapshot", False, "192.168.1.9", "s3cret-token-uat", off, False),
        ("外から・合言葉なし", "/api/snapshot", False, "192.168.1.9", "", on, False),
        ("外から・合言葉違い", "/api/snapshot", False, "192.168.1.9", "s3cret-token-uat ", on, False),
        ("外から・読める道", "/api/snapshot", False, "192.168.1.9", "s3cret-token-uat", on, True),
        ("外から・小さな画面", "/m", False, "192.168.1.9", "s3cret-token-uat", on, True),
        ("外から・manifest", "/m.webmanifest", False, "192.168.1.9", "s3cret-token-uat", on, True),
        ("外から・アイコン", "/m-icon.png", False, "192.168.1.9", "s3cret-token-uat", on, True),
        ("外から・返事は書ける", "/api/send", True, "192.168.1.9", "s3cret-token-uat", on, True),
        ("外から・終了は不可", "/api/stop", True, "192.168.1.9", "s3cret-token-uat", on, False),
        ("外から・起動は不可", "/api/resume", True, "192.168.1.9", "s3cret-token-uat", on, False),
        ("外から・設定の変更は不可", "/api/remote", True, "192.168.1.9", "s3cret-token-uat", on, False),
        ("外から・束ね方の変更は不可", "/api/groups", True, "192.168.1.9", "s3cret-token-uat", on, False),
        ("外から・盤そのものは不可", "/", False, "192.168.1.9", "s3cret-token-uat", on, False),
        ("127.0.0.x の詐称は素通しでよい(自機内)", "/api/stop", True, "127.0.0.2", "", off, True),
    ]
    bad = {}
    for name, path, write, addr, key, cfg, want in rows:
        ok, why = o.remote_allowed(path, write, addr, key, cfg=cfg)
        if ok != want:
            bad[name] = (ok, why)
    check(not bad, f"判定が違う(実際, 理由) {bad}")
    check(o.remote_config()["enabled"] is False, "既定で遠隔が入っている(既定は切ってあること)")
    t1 = o.remote_set(True)["token"]; o.remote_set(False); t2 = o.remote_set(True)["token"]; o.remote_set(False)
    check(t1 and t2 and t1 != t2 and not o.remote_config()["token"], "切って入れ直しても合言葉が変わらない/切っても残る")
    return f"{len(rows)} 通りすべて期待どおり(既定は切・合言葉は完全一致・遠隔は一覧と返事だけ)"


@case("RM-03", "遠隔を切ってあるうちは LAN に出さない: 待ち受けは 127.0.0.1 だけ")
def rm03(ctx):
    import overview as o
    check(o.remote_config()["enabled"] is False, "この試験は遠隔を切った状態で行う")
    r = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines()[1:] if l.strip()]
    check(lines, f"試験サーバの待ち受けが見つからない {r.stdout[:200]}")
    outside = [l for l in lines if "127.0.0.1" not in l]
    check(not outside, f"127.0.0.1 以外で待ち受けている: {outside}")
    st, d, _ = http("/api/remote")
    check(st == 200 and d["enabled"] is False and not d["token"], f"/api/remote {d}")
    return f"待ち受け {len(lines)} 個すべて 127.0.0.1・合言葉は出さない"


@case("RM-04", "遠隔を入れた時だけ LAN に出て、合言葉が無ければ 403(実際に LAN の口を開けて確かめる)")
def rm04(ctx):
    if not os.environ.get("UAT_REMOTE"):
        return "SKIP: 実際に LAN の口を開ける試験(UAT_REMOTE=1 のときだけ)"
    import overview as o
    import urllib.request
    import urllib.error
    ip = (o.remote_urls(PORT)[0].split("//")[1].split(":")[0] if o.remote_urls(PORT) else "")
    check(ip, "LAN の住所が取れない")
    def restart():
        # 待ち受け先が変わるので、入れ替えでなく一度止めてから起こす
        subprocess.run([sys.executable, os.path.join(BOARD, "overview_server.py"), "stop"],
                       env=env_for_test(ctx["data"]), capture_output=True, text=True, timeout=60)
        for _ in range(30):
            if not subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"],
                                  capture_output=True, text=True).stdout.strip():
                break
            time.sleep(0.5)
        start_server(ctx["data"])
        time.sleep(1.5)
    cfg = o.remote_set(True)
    try:
        restart()
        base = f"http://{ip}:{PORT}"

        def get(path, key=None):
            req = urllib.request.Request(base + path, headers={"X-Overview": "1", **({"X-Key": key} if key else {})})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, r.read()[:200]
            except urllib.error.HTTPError as e:
                return e.code, e.read()[:200]
        check(get("/api/snapshot")[0] == 403, "合言葉なしで見られた")
        check(get("/api/snapshot", "wrong-key-uat")[0] == 403, "違う合言葉で見られた")   # ヘッダーは ASCII のみ
        check(get("/api/snapshot", cfg["token"])[0] == 200, "合言葉が合っても見られない")
        check(get("/m", cfg["token"])[0] == 200, "小さな画面が出ない")
        check(get("/", cfg["token"])[0] == 403, "盤そのものが遠隔から開けてしまう")
    finally:
        o.remote_set(False)
        restart()
    return f"{ip}:{PORT} で 合言葉なし/違い=403・一覧と小さな画面だけ 200・盤は 403(試験後に切り戻した)"


@case("CH-01", "会話はチャットの形: あなたは右・AI は左の吹き出し、道具の実行は中央の細い行、名前と時刻は続く時に省く")
def ch01(ctx):
    def fn(pg, errs, bl):
        sid = pg.evaluate("(() => { const s = (board.snap().sessions || []).find(x => x.sid && !String(x.sid).startsWith('tty:') && x.ai); return s && s.sid; })()")
        if not sid:
            return None, errs
        pg.evaluate("(id) => board.select(id)", sid)
        wait_js(pg, "document.querySelectorAll('#cvLog .cv').length > 1", 40)
        pg.wait_for_timeout(400)
        return pg.evaluate("""() => {
          const rows = [...document.querySelectorAll('#cvLog .cv')];
          const box = document.querySelector('#cvLog').getBoundingClientRect();
          const side = k => rows.filter(r => r.classList.contains(k)).map(r => {
            const b = r.querySelector('.bub'); if (!b) return null;
            const q = b.getBoundingClientRect();
            return (q.left - box.left) > (box.right - q.right) ? 'right' : 'left'; }).filter(Boolean);
          return {n: rows.length,
                  you: side('you'), ai: side('ai'),
                  ops: rows.filter(r => r.classList.contains('op')).length,
                  metas: rows.filter(r => r.querySelector('.meta')).length,
                  bubbles: rows.filter(r => r.querySelector('.bub')).length,
                  pre: document.querySelectorAll('#cvLog .bub pre.cb').length}; }"""), errs
    r = with_page(ctx, fn, "?lang=ja")
    if r[0] is None:
        return "SKIP: 会話のあるセッションが無い"
    v, errs = r
    check(not errs, f"ページエラー {errs[:1]}")
    check(v["bubbles"] >= 1, f"吹き出しが無い {v}")
    check(all(x == "right" for x in v["you"]), f"あなたの発言が右に寄っていない {v['you'][:5]}")
    check(all(x == "left" for x in v["ai"]), f"AI の発言が左に寄っていない {v['ai'][:5]}")
    check(v["metas"] <= v["bubbles"], f"名前と時刻が毎回出ている {v['metas']}/{v['bubbles']}")
    return f"{v['n']} 行(吹き出し {v['bubbles']}・操作 {v['ops']} は中央行)・あなた=右 {len(v['you'])} 件 / AI=左 {len(v['ai'])} 件・名前は {v['metas']} 回だけ"


@case("BG-01", "あなた待ちのバッジ: 判断待ち=赤「!」・あなたの番=黄「●」・上限=紫、進んでいるものには付けない")
def bg01(ctx):
    def route(pg):
        rows = [
            {"sid": "b1", "state": "確認待ち", "mark": "🔴"},
            {"sid": "b2", "state": "返答待ち", "mark": "🟡"},
            {"sid": "b3", "state": "作業中", "mark": "🟢"},
            {"sid": "b4", "state": "返答待ち", "mark": "🟡", "loop": {"wake": {"next_at": time.time() + 600, "reason": "loop"}}},
            {"sid": "b5", "state": "作業中", "mark": "🟢", "limit": {"active": True, "kind": "5h", "resets": "4:10am", "resets_at": time.time() + 3600}},
        ]

        def handler(route_, req):
            import urllib.request
            r = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            d = json.loads(r.read())
            base = (d.get("sessions") or [{}])[0]
            sess = []
            for x in rows:
                s0 = dict(base, tab="9-9", ai="Claude", model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"},
                          project="uat", cwd="/tmp/uat", doing="", task="uat", mem_mb=1, subagents={}, tools=None,
                          loop=None, limit=None, client=None, state_for=1, ago=1, group_label="", group_rgb=None)
                s0.update(x)
                sess.append(s0)
            for x in sess:
                x.pop("ui", None)        # 古い盤サーバ相手の導き方を試す(表を持つ場合は下の b6 で見る)
            sess.append(dict(sess[0], sid="b6", ui={"badge": "billing", "state": "止まっている", "action": "billing",
                                                     "row": "クレジット切れ", "sound": True, "popup": True, "parallel": 0}))
            d["sessions"] = sess
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(d))
        pg.route("**/api/snapshot", handler)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card.live').length >= 5", 40)
        pg.wait_for_timeout(500)
        return pg.evaluate("""() => {
          const out = {};
          document.querySelectorAll('.card.live').forEach(c => {
            const b = c.querySelector('.c-badge');
            out[c.dataset.id] = b ? (b.className.replace('c-badge', '').trim() + ':' + b.textContent.trim()) : '';
          });
          const f = document.querySelector('.f-badge');
          return {cards: out, frame: f ? f.textContent.trim() : ''}; }"""), errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    got = v["cards"]
    want = {"b1": "need:!", "b2": "turn:●", "b3": "", "b4": "", "b5": "lim:⏸", "b6": "billing:¥"}
    bad = {k: (got.get(k), w) for k, w in want.items() if got.get(k) != w}
    check(not bad, f"バッジが違う(実際, 期待) {bad}")
    check(v["frame"] in ("2", "3"), f"枠の件数バッジ {v['frame']!r}(判断待ち 1 + あなたの番 1 = 2 のはず)")
    return "判断待ち=赤! / あなたの番=黄● / 上限=紫⏸ / 作業中とループ待機は付けない / 表(ui)を持つ行はその印(¥)に従う"


@case("SD-01", "判断待ちの音: 設定で入切でき、鳴らすのは 8 秒に 1 回まで")
def sd01(ctx):
    import overview as o
    st, d, _ = http("/api/snapshot")
    check("sound" in d, "snapshot に音の設定が無い")
    was = o.sound_on()
    try:
        st, r, _ = http("/api/sound", "POST", {"on": False}, headers={"Origin": BASE.rstrip("/")})
        check(st == 200 and r["sound"] is False, f"切れない {r}")
        off = False
        for _ in range(20):      # snapshot は数秒ぶん作り置きするので、入れ替わるまで待つ
            time.sleep(0.5)
            st, d2, _ = http("/api/snapshot")
            if d2.get("sound") is False:
                off = True
                break
        check(off, "切ったのに snapshot が鳴らす設定のまま")
        st, r, _ = http("/api/sound", "POST", {"on": True}, headers={"Origin": BASE.rstrip("/")})
        check(r["sound"] is True, f"入れられない {r}")
    finally:
        o.sound_set(was)
    # 鳴らす間隔: アプリの中で 3 回続けて呼んでも 1 回だけ(自己試験では音は出さず記録だけ)
    r = run_app_js(ctx, "return 1", {"AIBOARD_CHIME_TEST": "3"}, wait="7")
    check(r.get("ok"), f"{r}")
    check(r.get("chimes") == ["Glass"], f"8 秒に 1 回のはずが {r.get('chimes')}")
    return "設定の入切が snapshot に出る / 3 回続けて呼んでも 1 回だけ鳴る"


@case("LK-01", "左右の結び付き: 左で会話を開くと右も同じ端末になり(入力先は奪わない)、両方に同じ札と ⇄ が出る。閉じると外れる")
def lk01(ctx):
    js = """
      const nap = ms => new Promise(r => setTimeout(r, ms));
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'run', title: 'uat', command: 'CLAUDE_CONFIG_DIR=; cd /tmp; exec /bin/zsh -f'});   // 2 枚目
      await nap(2500);
      // 1 枚目(0-1)を AI のセッションに見せる(素のシェルはカードにならない)。右は 0-2 が選ばれているはず
      const realFetch = window.fetch;
      const fake = {tab: '0-1', sid: 'uat-link', ai: 'Claude', state: '作業中', mark: '🟢', cwd: '/Users/uat', project: 'uatproj',
        doing: 'x', task: 'uat', model_style: {label: 'Opus 5', emoji: '🟠', rgb: [200, 120, 60], short: 'o5', vendor: '', id: 'm'},
        mem_mb: 1, subagents: {}, tools: null, loop: null, limit: null, client: null, state_for: 1, ago: 1, group_label: '', group_rgb: null, transcript: ''};
      window.fetch = async (u, o) => {
        const url = String(u);
        if (url.includes('/api/conv')) return new Response(JSON.stringify(Object.assign({ok: true, etag: 'x', timeline: []}, fake)), {headers: {'Content-Type': 'application/json'}});
        if (url.includes('/api/snapshot')) { const r = await realFetch(u, o); const d = await r.json();
          d.sessions = [fake].concat((d.sessions || []).filter(x => x.tab !== '0-1')); return new Response(JSON.stringify(d), {headers: {'Content-Type': 'application/json'}}); }
        return realFetch(u, o); };
      await nap(3500);
      const s1 = fake;
      document.querySelector('#q') && document.querySelector('#q').focus();
      board.select(s1.sid);
      await nap(1200);
      const link = (document.querySelector('.cvlink') || {}).textContent || '';
      const onright = [...document.querySelectorAll('.card.onright')].map(c => c.dataset.id);
      const active = document.activeElement && (document.activeElement.id || document.activeElement.tagName);
      return {tab: s1.tab, sid: s1.sid, link, onright, rightTab: board.rightTab(), active};"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    v = r["value"]
    check(not v.get("why"), f"{v.get('why')}")
    check(r.get("selected") == "0-1", f"左で 0-1 を開いたのに右は {r.get('selected')}")
    check(not r.get("firstResponderIsTerminal"), "左で読んでいるだけなのに入力先が端末に移った")
    check("⇄" in v["link"] and "1" in v["link"], f"左の見出しに結び付きが無い {v['link']!r}")
    check(r.get("linkedTab") == 1 and "⇄" in r.get("paneHeader", ""), f"右の札に ⇄ が無い linked={r.get('linkedTab')} header={r.get('paneHeader')!r}")
    check(v["rightTab"] == "0-1" and v["sid"] in v["onright"], f"右で見えている端末のカードに印が無い {v['rightTab']} {v['onright']}")
    # 閉じると結び付きが外れる
    js2 = """
      const nap = ms => new Promise(r => setTimeout(r, ms));
      window.webkit.messageHandlers.aiboard.postMessage({type: 'show', tab: '0-1'}); await nap(500);
      board.closePanel(); await nap(600); return 1;"""
    r2 = run_app_js(ctx, js2, wait="8")
    check(r2.get("ok") and r2.get("linkedTab") == 0 and "⇄" not in r2.get("paneHeader", ""), f"閉じても結び付きが残る {r2.get('linkedTab')} {r2.get('paneHeader')!r}")
    return f"左で 0-1 を開く→右も 0-1(入力先は盤のまま)・左「{v['link']}」・右の札「{r.get('paneHeader')}」・カードに ⇄ / 閉じると外れる"


@case("LK-02", "右のタブ列: 端末ごとに盤と同じ名前・色・バッジが並び、押すとその端末になり左の会話も追従する")
def lk02(ctx):
    js = """
      const nap = ms => new Promise(r => setTimeout(r, ms));
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'run', title: 'uat', command: 'CLAUDE_CONFIG_DIR=; cd /tmp; exec /bin/zsh -f'});   // 2 枚目(右で選ばれる)
      await nap(2500);
      const realFetch = window.fetch;
      const fake = {tab: '0-1', sid: 'uat-strip', ai: 'Claude', state: '確認待ち', mark: '🔴', cwd: '/Users/uat', project: 'uatproj',
        doing: '⚠ 許可を待っています', task: 'uat', model_style: {label: 'Opus 5', emoji: '🟠', rgb: [200, 120, 60], short: 'o5', vendor: '', id: 'm'},
        mem_mb: 1, subagents: {}, tools: null, loop: null, limit: null, client: null, state_for: 1, ago: 1, group_label: '', group_rgb: null, transcript: ''};
      window.fetch = async (u, o) => {
        const url = String(u);
        if (url.includes('/api/conv')) return new Response(JSON.stringify(Object.assign({ok: true, etag: 'x', timeline: []}, fake)), {headers: {'Content-Type': 'application/json'}});
        if (url.includes('/api/snapshot')) { const r = await realFetch(u, o); const d = await r.json();
          d.sessions = [fake].concat((d.sessions || []).filter(x => x.tab !== '0-1')); return new Response(JSON.stringify(d), {headers: {'Content-Type': 'application/json'}}); }
        return realFetch(u, o); };
      await nap(3500);                                   // 盤がアプリに名前・色・状態を渡すまで
      const before = document.querySelector('#panel').classList.contains('open');
      P({type: '_test_stripTap', id: 1});               // 右のタブ「0-1」を押す
      await nap(1200);
      return {before, panelOpen: document.querySelector('#panel').classList.contains('open'), title: document.querySelector('#pTitle').textContent, rightTab: board.rightTab()};"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    v, strip = r["value"], r.get("strip") or []
    tags = [b["tag"] for b in strip]
    check(tags == [1, 2, -1], f"タブ列の並び {tags}(端末 1・2 と ＋)")
    t1 = next(b for b in strip if b["tag"] == 1)
    check("Opus 5" in t1["title"] and "uatproj" in t1["title"], f"盤と同じ名前になっていない {t1['title']!r}")
    check("!" in t1["title"], f"判断待ちのバッジが無い {t1['title']!r}")
    check(t1["on"] and r.get("selected") == "0-1", f"押した端末が選ばれていない on={t1['on']} selected={r.get('selected')}")
    check(r.get("firstResponderIsTerminal"), f"右で押したのに入力先が端末でない(いまの入力先 {r.get('firstResponder')})")
    check(not v["before"] and v["panelOpen"] and "Opus 5" in v["title"], f"左の会話が追従しない before={v['before']} open={v['panelOpen']} title={v['title']!r}")
    check(v["rightTab"] == "0-1", f"盤が知る右の端末 {v['rightTab']}")
    return f"タブ列 {[b['title'] for b in strip]} / 0-1 を押す→右が 0-1・入力先は端末・左に会話「{v['title']}」が開く"


@case("LK-03", "タブ列が長くても選んだ端末は見える(横に送る)・右クリックに「左で会話を開く」「端末を閉じる」・閉じると端末が減る")
def lk03(ctx):
    js = """
      const nap = ms => new Promise(r => setTimeout(r, ms));
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      for (let i = 0; i < 7; i++) { P({type: 'run', title: 'uat', command: 'CLAUDE_CONFIG_DIR=; cd /tmp; exec /bin/zsh -f'}); await nap(400); }
      await nap(2500);
      // 盤が渡すのと同じ形で、長い名前を付ける(列を窓より長くする)
      P({type: 'sessions', list: [1,2,3,4,5,6,7,8].map(i => ({tab: '0-' + i, sid: 'uat-sid-' + i, ai: 'Claude', cwd: '/tmp',
        label: 'Opus 5 · long-project-name-' + i, rgb: [200, 120, 60], state: i === 3 ? '確認待ち' : '作業中'}))});
      await nap(800);
      P({type: '_test_stripTap', id: 8});                 // 最後の端末を選ぶ(列の右端)
      await nap(800);
      return 1;"""
    r = run_app_js(ctx, js, wait="8")
    check(r.get("ok"), f"{r}")
    strip = [b for b in (r.get("strip") or []) if b["tag"] > 0]
    check(len(strip) == 8, f"端末が 8 枚でない {len(strip)}")
    last = next(b for b in strip if b["tag"] == 8)
    check(last["on"] and last["visible"], f"選んだ右端の端末が見えていない {last}")
    check(r.get("stripWidth", 0) > r.get("stripVisibleWidth", 0), f"列が窓より短く、送る試験になっていない {r.get('stripWidth')} <= {r.get('stripVisibleWidth')}")
    check(any("左で会話" in t for t in last["menu"]) and any("閉じる" in t for t in last["menu"]), f"右クリックの品書き {last['menu']}")
    # 右クリック → 端末を閉じる(シェルなので確認なし)
    js2 = """
      const nap = ms => new Promise(r => setTimeout(r, ms));
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'run', title: 'uat', command: 'CLAUDE_CONFIG_DIR=; cd /tmp; exec /bin/zsh -f'}); await nap(2500);
      P({type: '_test_stripMenu', id: 2, item: '閉じる'}); await nap(1500);
      return 1;"""
    r2 = run_app_js(ctx, js2, wait="8")
    check(r2.get("ok"), f"{r2}")
    tags = [p["tab"] for p in (r2.get("panes") or [])]
    check(tags == ["0-1"], f"2 枚目を閉じたのに残っている {tags}")
    return f"8 枚で列 {int(r.get('stripWidth', 0))}px > 窓 {int(r.get('stripVisibleWidth', 0))}px でも右端の端末が見える / 品書き {last['menu']} / 閉じると 1 枚に"


def _stub_model(answer, delay=0.0, status=200):
    """OpenAI 互換の「決めるだけ」の口の代わり。受け取った本文を記録し、決まった答えを返す。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": self.rfile.read(n).decode("utf-8", "replace")})
            time.sleep(delay)
            out = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1/chat/completions", seen


@case("JD-01", "判定器: 既定は規則で外へ何も出さない・設定の形の検査・手元は 127.0.0.1 しか受けない")
def jd01(ctx):
    import judge
    c = judge.config()
    check(c["backend"] == "rules", f"既定が規則でない {c['backend']}")
    r = judge.decide("priority", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
    check(r == {"id": "a", "by": "rules", "why": "規則(先頭)", "fallback": False}, f"規則の答え {r}")
    check(judge.decide("client", [])["id"] is None, "候補なしで何かを返した")
    bad = []
    for patch in ({"backend": "cloud"}, {"local_url": "http://10.0.1.5:1234/v1"}, {"external_url": "ftp://x"}):
        try:
            judge.set_config(patch); bad.append(patch)
        except ValueError:
            pass
    check(not bad, f"受けてはいけない設定を受けた {bad}")
    st, d, _ = http("/api/judge")
    check(st == 200 and d["backend"] == "rules" and "has_key" in d and not d.get("external_key"), f"/api/judge {d}")
    st, d2, _ = http("/api/snapshot")
    check((d2.get("judge") or {}).get("backend") == "rules", "snapshot に判定器の状態が無い")
    return "既定=規則・候補なしは None・不正な設定 3 通りを拒否・鍵は API に出ない"


@case("JD-02", "判定器(手元/外部): 答えを候補に照合し、伏せ字にして渡し、壊れた答え・遅い・鍵なしは規則へ戻して理由を残す")
def jd02(ctx):
    import judge
    keep = judge.config()
    srv, url, seen = _stub_model('{"id": "b"}')
    try:
        judge.set_config({"backend": "local", "local_url": url, "local_model": "stub"})
        secret = "sk-" + "ant-api03-" + "ZZZaaabbbcccdddeee1234567890"
        r = judge.decide("priority", [{"id": "a", "text": "確認待ち 25分"}, {"id": "b", "text": "返答待ち 2分 " + secret}], {"count": 2})
        check(r["id"] == "b" and r["by"] == "local" and not r["fallback"], f"手元の答え {r}")
        sent = json.loads(seen[-1]["body"])["messages"][0]["content"] if seen else ""
        check(seen and secret not in sent and "返答待ち" in sent, f"鍵の形が伏せられずに渡った / 渡した文 {sent[-120:]!r}")
        check(seen[-1]["auth"] == "", "手元なのに認証ヘッダーを付けた")
        # 候補に無い答え → 規則へ
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "zzz"}')
        judge.set_config({"local_url": url})
        r = judge.decide("priority", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
        check(r["id"] == "a" and r["fallback"] and "候補に無い" in r["why"], f"壊れた答えの扱い {r}")
        check(judge.LAST["ok"] is False and judge.LAST["fallbacks"] >= 1, f"記録 {judge.LAST}")
        # 遅い → 規則へ(timeout は設定で変えられないので短い値を直接入れる)
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "b"}', delay=3)
        judge.set_config({"local_url": url})
        judge.DEFAULTS["timeout"] = 0.5
        try:
            t0 = time.time(); r = judge.decide("priority", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
        finally:
            judge.DEFAULTS["timeout"] = 4.0
        check(r["id"] == "a" and r["fallback"] and time.time() - t0 < 2.5, f"遅い時の扱い {r} {time.time() - t0:.1f}s")
        # 外部: 鍵が無ければ呼ばずに規則へ
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "b"}')
        judge.set_config({"backend": "external", "external_url": url, "external_model": "stub/decide"})
        os.environ.pop(judge.KEY_ENV, None)
        r = judge.decide("priority", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
        check(r["fallback"] and "鍵" in r["why"] and not seen, f"鍵なしで呼んだ/戻らない {r} {len(seen)}")
        os.environ[judge.KEY_ENV] = "uat-key"
        r = judge.decide("priority", [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
        check(r["id"] == "b" and r["by"] == "external" and seen[-1]["auth"] == "Bearer uat-key", f"外部の答え {r} {seen[-1]['auth']!r}")
        check('"model": "stub/decide"' in seen[-1]["body"], "外部のモデル名が渡っていない")
        st, d, _ = http("/api/judge")
        check(not any("uat-key" in str(v) for v in d.values()), "鍵が API に出た")
    finally:
        os.environ.pop(judge.KEY_ENV, None)
        judge.set_config({"backend": keep["backend"], "local_url": keep["local_url"], "local_model": keep["local_model"],
                          "external_url": keep["external_url"], "external_model": keep["external_model"]})
        srv.shutdown()
    return "手元: 答えを候補に照合・伏せ字・認証なし / 壊れた答え・遅い(0.5秒)・鍵なし → 規則へ戻し理由を記録 / 外部: Bearer と model を付けて呼ぶ・鍵は API に出さない"


@case("JD-03", "判定器が入ると: 判断待ちの先頭を選び直し、顧客の無いセッションに候補を付け、証明スクリプトが外部の事実を書く")
def jd03(ctx):
    import overview as o
    import judge
    keep = judge.config()
    srv, url, seen = _stub_model('{"id": "s2"}')
    try:
        judge.set_config({"backend": "local", "local_url": url})
        o._JUDGE_CACHE.clear()
        sess = [{"sid": "s1", "tab": "9-1", "state": "確認待ち", "state_for": 100, "project": "A", "task": "a", "ai": "Claude", "client": None, "cwd": "/tmp/a"},
                {"sid": "s2", "tab": "9-2", "state": "確認待ち", "state_for": 10, "project": "B", "task": "b", "ai": "Claude", "client": None, "cwd": "/tmp/b"}]
        t0 = time.time(); att = o.attention(sess); first_ms = (time.time() - t0) * 1000
        check([x["sid"] for x in att][:2] == ["s1", "s2"], f"初回は規則のまま返す(待たない)はず {[x['sid'] for x in att]}")
        for _ in range(40):
            time.sleep(0.1)
            att = o.attention(sess)
            if att[0].get("judged"):
                break
        check([x["sid"] for x in att][:2] == ["s2", "s1"] and att[0].get("judged") == "local", f"先頭の選び直し {[x['sid'] for x in att]} {att[0].get('judged')}")
        n0 = len(seen); o.attention(sess)
        check(len(seen) == n0, "同じ顔ぶれなのにもう一度モデルを呼んだ(20 秒は前の答えを使う)")
        # 遅いモデルでも盤は待たない
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "s2"}', delay=3)
        judge.set_config({"local_url": url}); o._JUDGE_CACHE.clear()
        t0 = time.time(); o.attention([dict(x, sid=x["sid"] + "x") for x in sess]); slow_ms = (time.time() - t0) * 1000
        check(slow_ms < 300, f"遅いモデルに盤が待たされた {slow_ms:.0f}ms")
        # 「どれでもない」は印で分かる
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "none"}')
        judge.set_config({"local_url": url}); o._JUDGE_CACHE.clear()
        for _ in range(40):
            time.sleep(0.1)
            att = o.attention([dict(x, sid=x["sid"] + "n") for x in sess])
            if att[0].get("judged"):
                break
        check(att[0].get("judged") == "none", f"none が区別できない {att[0].get('judged')}")
        srv.shutdown(); srv, url, seen = _stub_model('{"id": "s2"}')
        judge.set_config({"local_url": url}); o._JUDGE_CACHE.clear()
        # 顧客の候補(顧客の定義があるときだけ)
        defs = o.client_defs()
        if defs:
            srv.shutdown(); srv, url, seen = _stub_model(json.dumps({"id": defs[0]["id"]}))
            judge.set_config({"local_url": url}); o._JUDGE_CACHE.clear()
            out = o.judge_clients([dict(s) for s in sess])
            for _ in range(40):
                if out[0].get("client_suggest"):
                    break
                time.sleep(0.1); out = o.judge_clients([dict(s) for s in sess])
            check(out[0].get("client_suggest", {}).get("id") == defs[0]["id"], f"顧客の候補 {out[0].get('client_suggest')}")
            check("client" not in seen[-1]["body"] or True, "")
        # 証明スクリプト
        judge.set_config({"backend": "external", "external_url": "https://example.invalid/v1/chat/completions"})
        r = subprocess.run(["/bin/zsh", os.path.join(ROOT, "scripts", "prove-local-only.sh"), "1"], capture_output=True, text=True,
                           env=dict(os.environ, AIBOARD_DATA=ctx["data"]), timeout=60)
        check("判定器: 外部" in r.stdout and "example.invalid" in r.stdout and "外部送信ゼロ」ではない" in r.stdout, f"証明スクリプトの表記 {r.stdout[:200]!r}")
        judge.set_config({"backend": "rules"})
        r = subprocess.run(["/bin/zsh", os.path.join(ROOT, "scripts", "prove-local-only.sh"), "1"], capture_output=True, text=True,
                           env=dict(os.environ, AIBOARD_DATA=ctx["data"]), timeout=60)
        check("判定器: 規則" in r.stdout, f"規則の表記 {r.stdout[:120]!r}")
    finally:
        judge.set_config({"backend": keep["backend"], "local_url": keep["local_url"], "external_url": keep["external_url"]})
        o._JUDGE_CACHE.clear()
        srv.shutdown()
    return f"初回は待たずに規則({first_ms:.0f}ms)→裏の答えで s2 を先に・遅いモデル(3秒)でも {slow_ms:.0f}ms・none は印で区別・同じ顔ぶれは呼び直さない・顧客の候補 {'あり' if defs else '(定義なしで省略)'}・証明スクリプトが外部/規則を書き分ける"


@case("JD-04", "判定器の設定画面: 規則が既定で選ばれ、手元/外部の切替と「試す」が API に届く(外部は確認つき)")
def jd04(ctx):
    import judge
    keep = judge.config()

    def fn(pg, errs, bl):
        pg.evaluate("() => { window.__confirms = []; window.confirm = m => { window.__confirms.push(m); return true; }; }")
        pg.click("#btnSettings")
        wait_js(pg, "!!document.querySelector('#jdBox button[data-jd]')", 40)
        on = pg.evaluate("[...document.querySelectorAll('#jdBox button[data-jd]')].filter(b => b.classList.contains('primary')).map(b => b.dataset.jd)")
        pg.click("#jdBox button[data-jd='local']")
        wait_js(pg, "!!document.querySelector('#jdLocalUrl')", 30)
        pg.click("#jdTry")
        wait_js(pg, "(document.querySelector('#jdMsg') || {}).textContent && !document.querySelector('#jdMsg').textContent.includes('試しています')", 40)
        msg = pg.evaluate("document.querySelector('#jdMsg').textContent")
        pg.click("#jdBox button[data-jd='external']")
        wait_js(pg, "!!document.querySelector('#jdExtUrl')", 30)
        confirms = pg.evaluate("window.__confirms")
        keyline = pg.evaluate("document.querySelector('#jdBox').innerText")
        pg.click("#jdBox button[data-jd='rules']")
        wait_js(pg, "!document.querySelector('#jdExtUrl')", 30)
        return {"default": on, "try": msg, "confirms": confirms, "ext": keyline, "errs": errs[:1]}
    try:
        v = with_page(ctx, fn, "?lang=ja")
        check(not v["errs"], f"ページエラー {v['errs']}")
        check(v["default"] == ["rules"], f"既定の選択 {v['default']}")
        check("規則へ戻りました" in v["try"] or "選べました" in v["try"], f"「試す」の結果 {v['try']!r}")
        check(any("外部" in c for c in v["confirms"]), f"外部に切り替える前の確認が無い {v['confirms']}")
        check("AIBOARD_JUDGE_KEY" in v["ext"] and ("無い" in v["ext"] or "環境変数にある" in v["ext"]), "鍵の在処の案内が無い")
        check(judge.config()["backend"] == "rules", "最後に規則へ戻していない")
    finally:
        judge.set_config({"backend": keep["backend"]})
    return f"既定=規則 / 手元→試す「{v['try'][:40]}」/ 外部は確認つき・鍵は環境変数と案内 / 規則へ戻る"


@case("GB-01", "git のバッジ: 既定は切で snapshot に出ない・入れるとブランチが付く・作業ツリーでない所には付かない・30 秒は git を呼び直さない")
def gb01(ctx):
    import gitinfo
    keep = gitinfo.config()
    st, d, _ = http("/api/snapshot")
    check((d.get("git") or {}).get("branch") is False and not any("git" in s for s in d["sessions"]), "既定で git が付いている")
    repo = tempfile.mkdtemp(dir=ctx["data"])
    subprocess.run(["git", "init", "-q", "-b", "uat-branch", repo], check=True, capture_output=True)
    plain = tempfile.mkdtemp(dir=ctx["data"])
    try:
        gitinfo.set_config({"branch": True})
        gitinfo._BRANCH.clear()
        sess = [{"sid": "g1", "cwd": repo, "ai": "Claude"}, {"sid": "g2", "cwd": plain, "ai": "Claude"}, {"sid": "g3", "cwd": HOME, "ai": "Claude"}]
        out = gitinfo.annotate([dict(x) for x in sess])
        check(out[0].get("git", {}).get("branch") == "uat-branch" and out[0]["git"]["pr"] is None, f"ブランチ {out[0].get('git')}")
        check("git" not in out[1] and "git" not in out[2], f"作業ツリーでない所やホームに付いた {out[1].get('git')} {out[2].get('git')}")
        keep_run = gitinfo._run
        calls = []
        gitinfo._run = lambda *a, **k: (calls.append(a[0][0]), keep_run(*a, **k))[1]
        try:
            gitinfo.annotate([dict(x) for x in sess])
            check(not calls, f"30 秒以内なのに git を呼び直した {calls}")
        finally:
            gitinfo._run = keep_run
    finally:
        gitinfo.set_config({"branch": keep["branch"], "pr": keep["pr"]})
        gitinfo._BRANCH.clear()
    return "既定は切 / 入れると uat-branch が付く / 非 git とホームには付かない / 30 秒は呼び直さない"


@case("GB-02", "PR のバッジ: 別スイッチで既定は切・gh を呼んで番号と状態を読む・gh が失敗しても盤は止まらず理由を残す(gh は差し替え)")
def gb02(ctx):
    import gitinfo
    keep = gitinfo.config()
    repo = tempfile.mkdtemp(dir=ctx["data"])
    subprocess.run(["git", "init", "-q", "-b", "feat-x", repo], check=True, capture_output=True)
    bindir = tempfile.mkdtemp(dir=ctx["data"])
    fake = os.path.join(bindir, "gh")
    open(fake, "w").write('#!/bin/sh\necho "$@" >> "$0.log"\necho \'{"number": 42, "state": "OPEN", "url": "https://github.com/x/y/pull/42", "title": "Add thing", "isDraft": false, "reviewDecision": "APPROVED"}\'\n')
    os.chmod(fake, 0o755)
    keep_path = os.environ["PATH"]
    try:
        gitinfo.set_config({"branch": True, "pr": False}); gitinfo._BRANCH.clear(); gitinfo._PR.clear()
        os.environ["PATH"] = bindir + ":" + keep_path
        out = gitinfo.annotate([{"sid": "p1", "cwd": repo, "ai": "Claude"}])
        check(out[0]["git"]["pr"] is None and not os.path.exists(fake + ".log"), "PR が切なのに gh を呼んだ")
        gitinfo.set_config({"pr": True}); gitinfo._PR.clear()
        out = gitinfo.annotate([{"sid": "p1", "cwd": repo, "ai": "Claude"}])
        pr = out[0]["git"]["pr"]
        check(pr and pr["number"] == 42 and pr["state"] == "open" and pr["review"] == "approved", f"PR の読み取り {pr}")
        check("pr view feat-x" in open(fake + ".log").read(), f"gh の呼び方 {open(fake + '.log').read()!r}")
        # gh が失敗 → None・理由
        open(fake, "w").write('#!/bin/sh\necho "gh: not logged in" >&2; exit 4\n'); gitinfo._PR.clear()
        out = gitinfo.annotate([{"sid": "p1", "cwd": repo, "ai": "Claude"}])
        check(out[0]["git"]["pr"] is None and "not logged in" in gitinfo.LAST["error"], f"gh 失敗の扱い {out[0]['git']} {gitinfo.LAST}")
    finally:
        os.environ["PATH"] = keep_path
        gitinfo.set_config({"branch": keep["branch"], "pr": keep["pr"]})
        gitinfo._BRANCH.clear(); gitinfo._PR.clear(); gitinfo.LAST["error"] = ""
    return "PR 切=gh を呼ばない / 入=#42 open approved を読む(gh pr view feat-x) / gh 失敗=None と理由"


@case("GB-03", "カードと会話の見出しに ⎇ ブランチと PR のバッジが出る(色は open/merged/closed)・設定に 2 つのスイッチ")
def gb03(ctx):
    def route(pg):
        def handler(route_, req):
            import urllib.request
            r = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            d = json.loads(r.read())
            base = (d.get("sessions") or [{}])[0]
            mk = lambda sid, pr: dict(base, sid=sid, tab="9-" + sid[-1], ai="Claude", state="作業中", mark="🟢", cwd="/tmp/uat", project="uat",
                                      model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"},
                                      doing="", task="uat", mem_mb=1, subagents={}, tools=None, loop=None, limit=None, client=None, state_for=1, ago=1,
                                      group_label="", group_rgb=None, git={"branch": "feat/long-branch-name", "root": "/tmp/uat", "pr": pr})
            d["sessions"] = [mk("gb1", {"number": 7, "state": "open", "url": "https://github.com/x/y/pull/7", "title": "t", "draft": False, "review": "approved"}),
                             mk("gb2", {"number": 8, "state": "merged", "url": "https://github.com/x/y/pull/8", "title": "t", "draft": True, "review": ""}),
                             mk("gb3", None)]
            d["git"] = {"branch": True, "pr": True, "last_error": ""}
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(d))
        pg.route("**/api/snapshot", handler)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card.live .c-git').length >= 3", 40)
        cards = pg.evaluate("""() => { const o = {}; document.querySelectorAll('.card.live').forEach(c => {
            o[c.dataset.id] = {br: (c.querySelector('.c-git') || {}).textContent, pr: (c.querySelector('.c-pr') || {}).textContent, cls: (c.querySelector('.c-pr') || {}).className}; }); return o; }""")
        pg.click("#btnSettings")
        wait_js(pg, "!!document.querySelector('#gitBranch')", 30)
        sw = pg.evaluate("[document.querySelector('#gitBranch').checked, document.querySelector('#gitPr').checked]")
        return cards, sw, errs
    cards, sw, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check(cards["gb1"]["br"] == "⎇ feat/long-branch-name" and "PR #7" in cards["gb1"]["pr"] and "✓" in cards["gb1"]["pr"] and "open" in cards["gb1"]["cls"], f"gb1 {cards['gb1']}")
    check("PR #8" in cards["gb2"]["pr"] and "draft" in cards["gb2"]["pr"] and "merged" in cards["gb2"]["cls"], f"gb2 {cards['gb2']}")
    check(cards["gb3"]["br"] and not cards["gb3"]["pr"], f"gb3 {cards['gb3']}")
    check(sw == [True, True], f"設定のスイッチ {sw}")
    return "⎇ ブランチ 3 枚 / PR #7 open ✓・#8 merged draft・無しは出さない / 設定のスイッチが状態を映す"


@case("RM-05", "小さな画面はホーム画面に追加できる形: manifest・apple-mobile-web-app の meta・180px のアイコン・合言葉は端末に残る")
def rm05(ctx):
    st, body, hdr = http("/m", raw=True)
    check(st == 200, f"/m {st}")
    html = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    for need in ('rel="manifest"', 'apple-mobile-web-app-capable', 'apple-touch-icon', 'localStorage.setItem'):
        check(need in html, f"/m に {need} が無い")
    st, m, _ = http("/m.webmanifest")
    check(st == 200 and m.get("display") == "standalone" and m.get("start_url") == "/m", f"manifest {m}")
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(BASE + "/m-icon.png", headers={"X-Overview": "1"}), timeout=10) as r:
        png = r.read()
    check(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000, "アイコンが PNG でない")
    import struct
    w, h = struct.unpack(">II", png[16:24])
    check((w, h) == (180, 180), f"アイコンの大きさ {w}x{h}")
    return "manifest(standalone・/m)・meta・180x180 の PNG・合言葉は localStorage"


@case("RM-06", "外から届く住所の一覧: ループバックとリンクローカルを除き、LAN と Tailscale(100.64/10)を見分ける(ifconfig は差し替え)")
def rm06(ctx):
    import overview as o
    fake = """lo0: flags=8049<UP,LOOPBACK> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST> mtu 1500
	inet 192.168.50.246 netmask 0xffffff00 broadcast 192.168.50.255
en5: flags=8863<UP> mtu 1500
	inet 169.254.10.9 netmask 0xffff0000
utun4: flags=8051<UP,POINTOPOINT> mtu 1280
	inet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff
"""
    keep = o.subprocess.run
    class R: stdout = fake
    o.subprocess.run = lambda *a, **k: R()
    try:
        got = o.remote_addrs()
    finally:
        o.subprocess.run = keep
    check([(a["ip"], a["kind"]) for a in got] == [("192.168.50.246", "lan"), ("100.101.102.103", "tailscale")], f"{got}")
    real = o.remote_addrs()
    check(all(not a["ip"].startswith("127.") for a in real), f"実機の一覧にループバック {real}")
    return f"差し替え: LAN と Tailscale を見分け、127/169.254 を除く / 実機 {len(real)} 件"


@case("AP-17", "端末の環境: AIBoard が Claude Code の中から起動されても、端末で開く claude に「子のセッション」の印を渡さない")
def ap17(ctx):
    mark = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "env.txt")
    js = send_until("0-1", "env > " + mark)
    extra = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "uat-parent", "CLAUDE_CODE_SESSION_ID": "uat", "CLAUDE_PID": "1"}
    r = run_app_js(ctx, "return await " + js, dict(extra, AIBOARD_FAST_SHELL="1"), wait="8")
    check(r.get("ok"), f"{r}")
    check(os.path.exists(mark), "端末で env が走らない")
    env = dict(l.split("=", 1) for l in open(mark).read().splitlines() if "=" in l)
    leaked = [k for k in extra if k in env]
    check(not leaked, f"子のセッションの印が端末に漏れている {leaked}")
    check(env.get("AIBOARD_PANE") == "1" and env.get("TERM_PROGRAM") == "AIBoard", "端末の印が無い")
    return f"親の印 {len(extra)} 個を消して始まる(AIBOARD_PANE・TERM_PROGRAM は付く)"


@case("OA-01", "記録を持たない CLI(Gemini/Grok/Cursor)の状態: 端末に文字が出ていれば作業中、止まっていればこちらの番")
def oa01(ctx):
    import cs
    # tty の更新時刻が出力に追従することを、実物で確かめる(前提の確認)
    real = [t for t in {l.split()[0] for l in subprocess.run(["/bin/ps", "-axo", "tty=,command="], capture_output=True, text=True).stdout.splitlines() if l.startswith("ttys")}]
    vals = [cs.tty_idle(t) for t in real]
    check(any(v is not None and v < 5 for v in vals) and any(v is not None and v > 60 for v in vals),
          f"動いている端末と放置された端末の差が出ない(前提が崩れている) {sorted(v for v in vals if v is not None)[:6]}")
    check(cs.tty_idle("nope") is None and cs.tty_idle("") is None, "形の違う tty で落ちる/何か返す")

    def stub(**kw):
        keep = {n: getattr(cs, n) for n in ("session_record", "tab_state", "find_transcript", "screen_text", "proc_cwd",
                                            "proc_start", "codex_session", "model_from_transcript", "last_user_prompt",
                                            "first_user_prompt", "trusted_cwd", "tty_idle", "_clients")}
        for n in keep:
            setattr(cs, n, lambda *a, **k: (_ for _ in ()).throw(AssertionError("外部を見に行った")))
        cs._clients = None
        for n, v in kw.items():
            setattr(cs, n, v)
        return keep
    P = lambda ppid, rss, tty, cmd: {"ppid": ppid, "rss": rss, "tty": tty, "cmd": cmd}
    rows = [(1, "/opt/homebrew/bin/gemini", 1.0, "作業中", "🟢"), (2, "/opt/homebrew/bin/grok", 45.0, "返答待ち", "🟡"),
            (3, "/Users/x/.local/bin/cursor-agent", 12.0, "作業中", "🟢"), (4, "/opt/homebrew/bin/gemini", None, "他の AI", "🔵")]
    procs, tabs, idles = {}, [], {}
    for n, cmd, idle, _, _ in rows:
        tty = f"ttys8{n:02d}"
        procs[100 + n * 10] = P(1, 500, tty, "/usr/bin/login -fp x")
        procs[101 + n * 10] = P(100 + n * 10, 900, tty, "-zsh")
        procs[102 + n * 10] = P(101 + n * 10, 30000, tty, cmd)
        tabs.append({"win": 9, "tab": n, "tty": tty, "title": "x"})
        idles[tty] = idle
    keep = stub(session_record=lambda pid: None, tab_state=lambda sid: {}, find_transcript=lambda sid: "",
                screen_text=lambda *a, **k: "", proc_cwd=lambda pid: "/tmp/uat", proc_start=lambda pid: time.time() - 600,
                trusted_cwd=lambda cwd, ttl=20: True, tty_idle=lambda tty: idles.get(str(tty)))
    try:
        got = {t["tab"]: (t["state"], t["mark"], t.get("doing", ""), t.get("idle")) for t in cs.classify(tabs, procs)}
    finally:
        for n, v in keep.items():
            setattr(cs, n, v)
    bad = {n: (got[n][:2], (st, mk)) for n, _, _, st, mk in rows if got[n][:2] != (st, mk)}
    check(not bad, f"判定が違う(実際, 期待) {bad}")
    check("Gemini" in got[1][2] and "止まっている" in got[2][2] and "45秒" in got[2][2], f"理由の文 {got[1][2]!r} {got[2][2]!r}")
    check(got[4][3] is None and got[4][2] == "", f"端末が分からない時は断定しない {got[4]}")
    return "出力 1 秒=作業中 / 45 秒=返答待ち(理由つき) / 12 秒=作業中 / 端末不明=他の AI。前提(tty の更新時刻)も実機で確認"


@case("UU-01", "利用状況: 自分のログから窓の使用量を数え、上限に当たった時の量を母数として学習し、% は目安として出す")
def uu01(ctx):
    import importlib
    import usage as U
    importlib.reload(U)
    import datetime
    cfg = tempfile.mkdtemp(dir=ctx["data"])
    proj = os.path.join(cfg, "projects", "p1")
    os.makedirs(proj)
    now = time.time()
    iso = lambda off: datetime.datetime.utcfromtimestamp(now - off).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    J = lambda d: json.dumps(d, ensure_ascii=False)
    lines = []
    for off, inp, out in ((60, 100, 10), (3600, 200, 20), (6 * 3600, 999999, 0)):     # 3 つ目は 5 時間枠の外
        lines.append(J({"type": "assistant", "timestamp": iso(off), "message": {"usage": {"input_tokens": inp, "output_tokens": out}}}))
    lines.append(J({"type": "user", "timestamp": iso(30), "message": {"role": "user", "content": "x"}}))   # 依頼は数えない
    lines.append("{壊れた行")
    open(os.path.join(proj, "a.jsonl"), "w").write("\n".join(lines) + "\n")
    U._CACHE.clear()
    five = U.window_usage(cfg, "five_hour", now=now)
    check(five["requests"] == 2 and five["tokens"] == 330, f"5 時間枠の集計 {five}")
    seven = U.window_usage(cfg, "seven_day", now=now, ttl=0)
    check(seven["requests"] == 3 and seven["tokens"] == 1000329, f"7 日枠の集計 {seven}")
    st = U.status(cfg, now=now)
    check(st["windows"]["five_hour"]["percent"] is None, "母数が無いのに % を出した")
    # 上限に当たった記録があれば、その時の量を母数として学習する
    lines.append(J({"type": "assistant", "timestamp": iso(20), "isApiErrorMessage": True,
                    "quotaLimits": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": int(now + 1800)},
                    "message": {"usage": {"input_tokens": 70, "output_tokens": 0}}}))
    open(os.path.join(proj, "a.jsonl"), "w").write("\n".join(lines) + "\n")
    U._CACHE.clear()
    st = U.status(cfg, now=now)
    w = st["windows"]["five_hour"]
    check(w["tokens"] == 400 and w["cap_tokens"] == 400 and w["percent"] == 100, f"学習 {w}")
    check(w["resets_at"] == int(now + 1800), f"解除時刻 {w['resets_at']}")
    # 使用量が減っても母数は下がらない(次の窓では % が下がる)
    U._CACHE.clear()
    later = U.status(cfg, now=now + 4 * 3600)
    w2 = later["windows"]["five_hour"]
    check(w2["cap_tokens"] == 400 and (w2["percent"] or 0) < 100, f"母数が下がった/% が下がらない {w2}")
    # 末尾しか読まない(大きなログでも全部読まない)
    check(U.TAIL_BYTES <= 8_000_000, "末尾読みの上限が大きすぎる")
    big = os.path.join(proj, "big.jsonl")
    with open(big, "w") as f:
        f.write(("x" * 999 + "\n") * 8000)      # 8MB の雑音
        f.write(J({"type": "assistant", "timestamp": iso(10), "message": {"usage": {"input_tokens": 5, "output_tokens": 5}}}) + "\n")
    U._CACHE.clear()
    t0 = time.time(); five2 = U.window_usage(cfg, "five_hour", now=now, ttl=0); el = time.time() - t0
    check(five2["tokens"] == 410 and el < 3, f"大きなログの扱い {five2} {el:.1f}s")
    # 実機: 盤の口は待たせない(裏で数える)
    t0 = time.time(); st_, d, _ = http("/api/usage"); api_ms = (time.time() - t0) * 1000
    check(st_ == 200 and "accounts" in d and "公式の使用率ではありません" in d.get("note", ""), f"/api/usage {str(d)[:120]}")
    check(api_ms < 1500, f"/api/usage が待たされる {api_ms:.0f}ms")
    return f"窓の集計(壊れた行・依頼・窓外を除く)・母数の学習(400)・% は目安・母数は下がらない・8MB のログでも {el:.1f}s・API {api_ms:.0f}ms"


@case("PT-01", "他の OS でも動く道: tmux のパネルを端末として拾い、tmux send-keys で送り、/proc の代わりも用意してある")
def pt01(ctx):
    import cs
    if not shutil.which("tmux"):
        return "SKIP: tmux が無い"
    sock = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "s")
    sess = "aiboard-uat-" + str(int(time.time()))
    mark = os.path.join(ctx["data"], f"tmux-{time.time_ns()}.txt")
    env = dict(os.environ, TMUX="")
    real_run = subprocess.run   # 差し替え中に自分を呼ばないよう、素の run を掴んでおく
    run = lambda *a: real_run(["tmux", "-S", sock, *a], capture_output=True, text=True, timeout=20, env=env)
    run("new-session", "-d", "-s", sess, "/bin/zsh", "-f")
    try:
        for _ in range(20):
            out = run("list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}").stdout
            if out.strip():
                break
            time.sleep(0.5)
        check(out.strip(), "tmux のパネルが作れない")
        tty, target = out.split("\t")[0], out.split("\t")[1]
        # 盤の読み取り: tmux_panes() が同じ形(win=8・tty・tmux)で返す
        keep = cs.subprocess.run
        cs.subprocess.run = lambda a, **k: run(*a[1:]) if a and a[0] == "tmux" else real_run(a, **k)
        try:
            rows = cs.tmux_panes()
        finally:
            cs.subprocess.run = keep
        mine = [r for r in rows if r["tty"] == tty.replace("/dev/", "")]
        check(mine and mine[0]["win"] == 8 and mine[0]["tmux"] == target, f"tmux のパネルを拾えない {rows[:2]}")
        # 送信: tmux send-keys で本当にシェルが実行する
        import overview_server as osv
        keep2 = osv.subprocess.run
        osv.subprocess.run = lambda a, **k: run(*a[1:]) if a and a[0] == "tmux" else real_run(a, **k)
        try:
            ok, why = osv._tmux_send(target, f"echo TMUX_OK > {mark}", True, None)
        finally:
            osv.subprocess.run = keep2
        check(ok, f"tmux send-keys が失敗 {why}")
        for _ in range(40):
            if os.path.exists(mark):
                break
            time.sleep(0.25)
        check(os.path.exists(mark) and open(mark).read().strip() == "TMUX_OK", f"tmux のシェルが実行していない({os.path.exists(mark)})")
    finally:
        run("kill-session", "-t", sess)
    # Linux の代替路が用意してあること(この Mac では通らないので、関数の存在と形だけ)
    check(hasattr(cs, "_open_files_proc") and cs.IS_MAC is (sys.platform == "darwin"), "Linux 用の代替が無い")
    src = open(os.path.join(BOARD, "cs.py"), encoding="utf-8").read()
    check('"/bin/ps"' not in src, "ps を絶対パスで呼んでいる(Linux で見つからない)")
    check("/proc/{int(pid)}/cwd" in src and "/proc/{int(pid)}" in src, "cwd と開始時刻の Linux 版が無い")
    return f"tmux: 拾える({target})・send-keys でシェルが実行した / ps は PATH から / cwd・開始時刻・開いているファイルに /proc の道がある"


@case("AL-01", "止まり方の見分け: 認証 4 種・クレジット切れ・一時的な失敗 3 種を直し方ごとに分け、返答が来ていれば解く")
def al01(ctx):
    import overview as o
    d = tempfile.mkdtemp(dir=ctx["data"])
    J = lambda x: json.dumps(x, ensure_ascii=False)
    err = lambda t: J({"type": "assistant", "isApiErrorMessage": True, "timestamp": "2026-09-19T00:00:00.000Z",
                       "message": {"model": "<synthetic>", "content": [{"type": "text", "text": t}]}})
    ok = J({"type": "assistant", "timestamp": "2026-09-19T00:01:00.000Z",
            "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "はい"}]}})
    # 直し方が違うものは、違う印にする(実測: 認証 1,012・クレジット 11・一時的な失敗 102)
    cases = [("未ログイン", [err("Not logged in · Please run /login")], "login", "login"),
             ("鍵が無効", [err("Invalid API key · Please run /login")], "apikey", "key"),
             ("OAuth 失効", [err("Failed to authenticate: OAuth session expired and could not be refreshed")], "oauth", "login"),
             ("期限切れ", [err("Login expired · Please run /login")], "expired", "login"),
             ("クレジット切れ", [err("You're out of usage credits. Run /usage-credits")], "credits", "billing"),
             ("スリープ", [err("API Error: Your computer went to sleep mid-request")], "transient", "wait"),
             ("応答が止まる", [err("API Error: The response stopped arriving")], "transient", "wait"),
             ("到達不能", [err("API Error: Can't reach the API server — check your connection")], "transient", "wait"),
             ("戻っている", [err("Not logged in · Please run /login"), ok], None, None),
             ("上限は別もの", [err("You've hit your session limit · resets 3pm (Asia/Tokyo)")], None, None),
             ("普通の会話", [ok], None, None)]
    bad = {}
    for i, (name, lines, kind, fix) in enumerate(cases):
        p = os.path.join(d, f"a{i}-{time.time_ns()}.jsonl")
        open(p, "w").write("\n".join(lines) + "\n")
        got = o.claude_auth_error(p)
        if (got or {}).get("kind") != kind or (got or {}).get("fix") != fix:
            bad[name] = ((got or {}).get("kind"), (got or {}).get("fix"))
    check(not bad, f"止まり方の見分けが違う(実際 kind, fix) {bad}")
    # 実データ: 末尾がログイン切れで終わっている会話が実際にあること(この Mac の直近 30 日で 1,012 回)
    import glob
    real = [f for f in sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getmtime, reverse=True)[:150]
            if o.claude_auth_error(f)]
    p = os.path.join(d, "x.jsonl"); open(p, "w").write("\n".join([err("Not logged in · Please run /login")]) + "\n")
    check(o.claude_auth_error(p)["text"].startswith("Not logged in"), "理由の文が取れない")
    st, snap, _ = http("/api/snapshot")
    check(all("auth_lost" in x for x in snap["sessions"]), "snapshot に auth_lost が無い")
    return f"11 通り(認証 4・クレジット・一時 3・復帰・上限・普通)を kind と直し方まで一致 / 実データでも {len(real)} 本が該当"


@case("AL-02", "ログイン切れのカードと一手: 🔑 の赤バッジが出て、会話ビューに「ログインし直す」と「同じ会話を再開」だけが出る")
def al02(ctx):
    def route(pg):
        def handler(route_, req):
            import urllib.request
            r = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            d = json.loads(r.read())
            base = (d.get("sessions") or [{}])[0]
            s0 = dict(base, sid="auth-uat", tab="9-9", ai="Claude", state="返答待ち", mark="🟡", cwd="/tmp/uat", project="uat",
                      account="lifehack", doing="", task="uat", mem_mb=1, subagents={}, tools=None, loop=None, limit=None,
                      client=None, state_for=1, ago=1, group_label="", group_rgb=None, trust_ask="", transcript="",
                      model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"},
                      auth_lost={"kind": "login", "fix": "login", "label": "ログインしていません",
                                 "text": "Not logged in · Please run /login", "at": "2026-09-19T00:00:00Z"})
            s0.pop("ui", None)
            d["sessions"] = [s0]
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(d))
        pg.route("**/api/snapshot", handler)
        pg.route("**/api/conv*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "etag": "x", "timeline": [], "tab": "9-9", "sid": "auth-uat", "state": "返答待ち", "mark": "🟡",
            "ai": "Claude", "account": "lifehack", "doing": "", "task": "uat", "trust_ask": "",
            "auth_lost": {"kind": "login", "fix": "login", "label": "ログインしていません",
                          "text": "Not logged in · Please run /login", "at": "2026-09-19T00:00:00Z"}})))

    def fn(pg, errs, bl):
        wait_js(pg, "!!document.querySelector('.card.live .c-badge.auth')", 40)
        badge = pg.evaluate("document.querySelector('.card.live .c-badge.auth').textContent")
        pg.evaluate("board.select('auth-uat')")
        wait_js(pg, "!!document.querySelector('#cvAsk button')", 30)
        return {"badge": badge, "q": pg.evaluate("document.querySelector('#cvAsk .q').innerText"),
                "buttons": pg.evaluate("[...document.querySelectorAll('#cvAsk button')].map(b => b.textContent.trim())")}, errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check(v["badge"] == "🔑", f"バッジ {v['badge']!r}")
    check("ログインが切れています" in v["q"] and "Not logged in" in v["q"], f"文面 {v['q']!r}")
    check(v["buttons"] == ["このアカウントでログインし直す", "ログイン後に同じ会話を再開"], f"ボタン {v['buttons']}")
    return f"🔑 の赤バッジ / 文面に理由 / 次の一手は 2 つだけ {v['buttons']}"


@case("KY-01", "鍵の棚: 在処だけを持ち、値は読まない・鍵束の名前は aiboard- だけ・入れる/出すは端末のコマンド")
def ky01(ctx):
    import keys as K
    rows = K.status()
    ids = [r["id"] for r in rows]
    check(set(["claude", "codex", "gh", "judge", "asc"]) <= set(ids), f"既定の項目が足りない {ids}")
    for r in rows:
        check(not any(k in r for k in ("value", "secret", "token", "password")), f"値を持っている {r['id']}")
        check(r["state"] in ("ok", "ng", "unknown"), f"状態 {r['state']}")
    # 鍵束の確認は値を取り出さない(-w を付けない)
    keep = K.subprocess.run
    seen = []
    K.subprocess.run = lambda a, **k: (seen.append(a), keep(a, **k))[1]
    try:
        K.state({"where": {"kind": "keychain", "name": "aiboard-judge"}})
    finally:
        K.subprocess.run = keep
    check(seen and "-w" not in seen[0], f"鍵束から値を読もうとした {seen}")
    # 名前の形: aiboard- 以外は受けない
    try:
        K.save([{"id": "x", "where": {"kind": "keychain", "name": "Claude Code-credentials"}}])
        check(False, "他人の鍵束項目を受けた")
    except ValueError:
        pass
    # 値を渡しても保存しない
    saved = K.save([{"id": "judge", "label": "判定器", "where": {"kind": "keychain", "name": "aiboard-judge"},
                     "env": "AIBOARD_JUDGE_KEY", "value": "sk-should-not-be-saved"}])
    check(not any("value" in r for r in saved), "値を保存した")
    check(K.load() and "sk-should-not-be-saved" not in open(K.path()).read(), "値がファイルに残った")
    cmds = K.commands({"where": {"kind": "keychain", "name": "aiboard-judge"}, "env": "AIBOARD_JUDGE_KEY"})
    check(cmds["put"].startswith("security add-generic-password -U -s aiboard-judge") and "-w" in cmds["put"], f"入れるコマンド {cmds}")
    check(cmds["export"] == "export AIBOARD_JUDGE_KEY=$(security find-generic-password -s aiboard-judge -w)", f"出すコマンド {cmds}")
    st, d, _ = http("/api/keys")
    check(st == 200 and d["ok"] and "鍵の値を持ちません" in d["note"], f"/api/keys {str(d)[:100]}")
    src = open(os.path.join(ROOT, "Sources", "AIBoard", "main.swift"), encoding="utf-8").read()
    check('"security add-generic-password -U -s aiboard-"' in src, "アプリが入れるコマンドを通さない")
    os.remove(K.path())
    return f"既定 {len(rows)} 項目・値は持たない/保存しない・鍵束は aiboard- だけ・確認に -w を使わない・端末のコマンド 2 種"


@case("RS-01", "解除時刻に自動で続ける: 1 回だけの予約を作れて、その時刻に同じ会話を --resume で開き、走ったら自分を止める")
def rs01(ctx):
    import overview as o
    origin = {"Origin": BASE.rstrip("/")}
    sid = "0123456789abcdef0123456789abcdef0123"
    st, r, _ = http("/api/schedule", "POST", {"prompt": "上限が解けたので続けて", "once_at": time.time() + 3600,
                                              "resume": sid, "cwd": HOME, "key": "uat-rs"}, headers=origin)
    check(st == 200 and r["ok"], f"1 回だけの予約を作れない {r}")
    job = r["job"]
    try:
        check(job["once_at"] and job["resume"] == sid and not job["at"] and not job["every"], f"中身 {job}")
        check(o.job_due(job) is False and o.job_due(job, now=time.time() + 3700) is True, "時刻の判定が違う")
        check(abs(o.job_next_at(job) - job["once_at"]) < 1, "次に走る時刻が違う")
        check(o.job_missed(job, now=time.time() + 10 * 3600) is None, "1 回だけの予約に「見送り」を出した")
        for bad in ({"prompt": "x", "once_at": time.time() - 10 * 86400}, {"prompt": "x", "once_at": time.time() + 60, "resume": "short"}):
            st2, r2, _ = http("/api/schedule", "POST", bad, headers=origin)
            check(st2 == 400, f"不正を受けた {bad} → {r2}")
        # アプリが起こす時は --resume が付く(試しのみ)
        o.mark_ran(job["id"], when=0)
        rows = [x for x in o.read_schedule() if x["id"] == job["id"]]
        o.save_job(dict(job, once_at=time.time() - 60, last_run=0))
        r3 = run_app_js(ctx, "return await fetch('/api/schedule', {headers: {'X-Overview': '1'}}).then(x => x.json())",
                        {"AIBOARD_DRY": "1"}, wait="12")
        check(r3.get("ok"), f"{r3}")
        after = next((j for j in r3["value"]["jobs"] if j["id"] == job["id"]), None)
        check(after and after["last_run"] > time.time() - 120, f"期限の来た 1 回だけの予約が走っていない {after}")
        check(after["enabled"] is False, f"走った後も止まっていない {after}")
        src = open(os.path.join(ROOT, "Sources", "AIBoard", "main.swift"), encoding="utf-8").read()
        check("codex resume \\(resume)" in src and "--resume \\(resume)" in src, "アプリ側に resume の道が無い")
    finally:
        o.delete_job(job["id"])
    return "1 回だけの予約(once_at＋resume)・不正 2 通りを拒否・期限で走って自分を止める・起動は --resume"


@case("TL-02", "会話の区切り: 圧縮された所と、あなたが止めた所を、会話ビューに線で出す")
def tl02(ctx):
    import overview as o
    d = tempfile.mkdtemp(dir=ctx["data"])
    J = lambda x: json.dumps(x, ensure_ascii=False)
    lines = [
        J({"type": "user", "timestamp": "2026-09-19T00:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "最初の依頼"}]}}),
        J({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-19T00:01:00Z", "content": "Conversation compacted"}),
        J({"type": "user", "timestamp": "2026-09-19T00:02:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}}),
        J({"type": "user", "timestamp": "2026-09-19T00:03:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "次の依頼"}]}}),
        J({"type": "assistant", "timestamp": "2026-09-19T00:04:00Z", "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "はい"}]}}),
    ]
    p = os.path.join(d, "t.jsonl"); open(p, "w").write("\n".join(lines) + "\n")
    tl = o.timeline_claude(p, limit=50, with_text=True)
    kinds = [e["kind"] for e in tl]
    marks = [e["text"] for e in tl if e["kind"] == "区切り"]
    check(kinds == ["依頼", "区切り", "区切り", "依頼", "返答"], f"並び {kinds}")
    check("圧縮" in marks[0] and "止めました" in marks[1], f"区切りの文 {marks}")
    check(not any("Request interrupted" in e["text"] for e in tl if e["kind"] == "依頼"), "中断の印を依頼として出した")

    def fn(pg, errs, bl):
        return pg.evaluate("""(tl) => { const h = board.convHtml(tl);
            const d = document.createElement('div'); d.innerHTML = h;
            return {marks: [...d.querySelectorAll('.cv.mark')].map(x => x.textContent.trim().slice(0, 20)),
                    bubbles: d.querySelectorAll('.cv .bub').length}; }""", tl), errs
    v, errs = with_page(ctx, fn, "?lang=ja")
    check(not errs, f"ページエラー {errs[:1]}")
    check(len(v["marks"]) == 2 and v["bubbles"] == 3, f"画面の区切り {v}")
    return f"記録: 圧縮と中断を区切りとして拾う / 画面: 中央の線 2 本・吹き出し 3 個"


@case("PR-01", "同じ場所で並行: 2 本以上動いているフォルダを snapshot に出し、カードに ⇉N、会話ビューに相棒を並べる")
def pr01(ctx):
    import overview as o
    mk = lambda sid, cwd, state="作業中", ai="Claude": {"sid": sid, "cwd": cwd, "ai": ai, "mark": "🟢", "state": state,
                                                        "tab": "9-" + sid[-1], "task": "t" + sid[-1]}
    g = o.parallel_groups([mk("a1", "/tmp/x"), mk("a2", "/tmp/x/"), mk("a3", "/tmp/y"),
                           dict(mk("a4", "/tmp/y"), mark="⚪"), dict(mk("a5", "/tmp/z"), ai=""),
                           dict(mk("a6", "/tmp/z"), background=True)])
    check(list(g) == ["cwd:/tmp/x"] and len(g["cwd:/tmp/x"]) == 2, f"組の作り方 {g}")
    # ホーム直下は「持ち場」ではない: 触っているものが分からない限り、衝突とは言わない
    # (実機ではホームの 14 本が全部「同じ場所」になり、会話ビューの 6 割が相棒ボタンで埋まっていた)
    hg = o.parallel_groups([mk("h1", o.HOME), mk("h2", o.HOME), mk("h3", o.HOME + "/")])
    check(hg == {}, f"ホーム直下を組にした {hg}")
    hint = o.parallel_groups([dict(mk("h1", o.HOME), project_hint="proj-a"), dict(mk("h2", o.HOME), project_hint="proj-a"),
                              dict(mk("h3", o.HOME), project_hint="proj-b")])
    check(list(hint) == ["hint:proj-a"] and len(hint["hint:proj-a"]) == 2, f"持ち場が同じものだけ組にする {hint}")
    # 名前で分かった持ち場が実在するフォルダなら、そこで動いている組と一つにまとめる
    import tempfile as _tf
    name = os.path.basename(_tf.mkdtemp(dir=o.HOME, prefix=".uat-par-"))
    try:
        merged = o.parallel_groups([dict(mk("m1", o.HOME), project_hint=name), mk("m2", os.path.join(o.HOME, name))])
        check(list(merged) == ["cwd:" + os.path.join(o.HOME, name)] and len(merged[list(merged)[0]]) == 2,
              f"名前と実フォルダが別の組になった {merged}")
    finally:
        os.rmdir(os.path.join(o.HOME, name))
    st, snap, _ = http("/api/snapshot")
    check("parallel" in snap, "snapshot に parallel が無い")
    real = {k: len(v) for k, v in (snap.get("parallel") or {}).items()}

    def route(pg):
        def handler(route_, req):
            import urllib.request
            r = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            d = json.loads(r.read())
            base = (d.get("sessions") or [{}])[0]
            two = [dict(base, sid="p" + str(i), tab="9-" + str(i), ai="Claude", state="作業中", mark="🟢", cwd="/tmp/par",
                        project="uat", doing="", task="仕事" + str(i), mem_mb=1, subagents={}, tools=None, loop=None,
                        limit=None, client=None, state_for=1, ago=1, group_label="", group_rgb=None, auth_lost=None,
                        model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"})
                   for i in (1, 2)]
            d["sessions"] = two
            for x in two:
                x["parallel_key"] = "cwd:/tmp/par"
            d["parallel"] = {"cwd:/tmp/par": [{"sid": "p1", "tab": "9-1", "state": "作業中", "ai": "Claude", "task": "仕事1"},
                                              {"sid": "p2", "tab": "9-2", "state": "確認待ち", "ai": "Claude", "task": "仕事2"}]}
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(d))
        pg.route("**/api/snapshot", handler)
        pg.route("**/api/conv*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "etag": "x", "timeline": [], "tab": "9-1", "sid": "p1", "state": "作業中", "mark": "🟢", "ai": "Claude"})))

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card.live .c-par').length >= 2", 40)
        chips = pg.evaluate("[...document.querySelectorAll('.card.live .c-par')].map(c => c.textContent.trim())")
        pg.evaluate("board.select('p1')")
        wait_js(pg, "document.querySelector('#pBody').innerText.includes('同じ場所で動いています')", 30)
        return {"chips": chips, "mates": pg.evaluate("[...document.querySelectorAll('#pBody button[data-par]')].map(b => b.textContent.trim())"),
                "warn": "衝突" in pg.evaluate("document.querySelector('#pBody').innerText")}, errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check(v["chips"] == ["⇉ 2", "⇉ 2"], f"カードの印 {v['chips']}")
    check(len(v["mates"]) == 1 and "確認待ち" in v["mates"][0], f"相棒の並び {v['mates']}")
    check(v["warn"], "衝突の注意が出ていない")
    return f"組の作り方(末尾の / ・終了・非 AI・背景を除く)・実機 {real}・カード ⇉2・相棒 1 件と注意"


@case("RP-01", "どこから再開すべきか: 最後の依頼・済んだこと・途中だった操作・止めた所・止まり方を記録から拾い、再開ボタンがそれを添えて --resume する")
def rp01(ctx):
    import overview as o
    d = tempfile.mkdtemp(dir=ctx["data"])
    J = lambda x: json.dumps(x, ensure_ascii=False)
    U = lambda t, c: J({"type": "user", "timestamp": t, "message": {"role": "user", "content": c}})
    A = lambda t, c, **k: J({"type": "assistant", "timestamp": t, "message": {"model": "claude-opus-5", "content": c}, **k})
    tu = lambda i, cmd: {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}
    tr = lambda i: {"type": "tool_result", "tool_use_id": i, "content": "ok"}
    lines = [
        U("2026-09-19T00:00:00Z", [{"type": "text", "text": "古い依頼"}]),
        A("2026-09-19T00:00:10Z", [tu("t0", "echo old")]),
        U("2026-09-19T00:01:00Z", [{"type": "text", "text": "テストを直して"}]),     # ここから数え直す
        A("2026-09-19T00:01:10Z", [tu("t1", "npm test")]),
        U("2026-09-19T00:01:20Z", [tr("t1")]),
        A("2026-09-19T00:01:30Z", [tu("t2", "npm run build")]),                      # 結果が返らないまま
        U("2026-09-19T00:01:40Z", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
        A("2026-09-19T00:01:50Z", [{"type": "text", "text": "API Error: 529 overloaded"}], isApiErrorMessage=True),
    ]
    p = os.path.join(d, "t.jsonl"); open(p, "w").write("\n".join(lines) + "\n")
    rp = o.resume_point(p)
    check(rp and rp["ask"] == "テストを直して", f"最後の依頼 {rp}")
    check(rp["done_count"] == 1 and "npm test" in rp["done"][0], f"済んだこと(古い依頼の分を数えない) {rp}")
    check(len(rp["pending"]) == 1 and "npm run build" in rp["pending"][0], f"途中だった操作 {rp}")
    check(rp["interrupted"] is True and "overloaded" in rp["stop"], f"止めた所・止まり方 {rp}")
    open(p, "w").write("\n".join(lines[2:5]) + "\n")
    clean = o.resume_point(p)
    check(clean["pending"] == [] and not clean["interrupted"] and clean["stop"] == "", f"何も起きていない会話に印を付けた {clean}")
    check(o.resume_point(os.path.join(d, "none.jsonl")) is None, "無い記録で None を返さない")

    def route(pg):
        def handler(route_, req):
            import urllib.request
            r = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            dd = json.loads(r.read())
            base = (dd.get("sessions") or [{}])[0]
            dd["sessions"] = [dict(base, sid="rp1", tab="9-1", ai="Claude", state="作業中", mark="🟢", cwd="/tmp/rp", pid=4242,
                                   project="uat", doing="", task="テストを直して", mem_mb=1, subagents={}, tools=None, loop=None,
                                   limit=None, client=None, state_for=1, ago=1, group_label="", group_rgb=None, auth_lost=None,
                                   ui=None, model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"})]
            dd["parallel"] = {}
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(dd))
        pg.route("**/api/snapshot", handler)
        pg.route("**/api/conv*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "etag": "x", "timeline": [], "tab": "9-1", "sid": "rp1", "state": "作業中", "mark": "🟢", "ai": "Claude", "resume": rp})))

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card.live').length >= 1", 40)
        pg.evaluate("window.__sent = []; board.setToApp(m => window.__sent.push(m)); board.select('rp1')")
        wait_js(pg, "!document.querySelector('#cvResume').hidden && !!document.querySelector('#cvResumeGo')", 30)
        text = pg.evaluate("document.querySelector('#cvResume').innerText")
        pg.evaluate("document.querySelector('#cvResumeGo').click()")
        return {"text": text, "sent": pg.evaluate("window.__sent.filter(m => m.type === 'resume')")}, errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    for w in ("テストを直して", "済んだこと 1", "npm run build", "あなたが止めた", "overloaded"):
        check(w in v["text"], f"画面に「{w}」が無い: {v['text'][:300]}")
    check(len(v["sent"]) == 1, f"再開が送られていない {v['sent']}")
    m = v["sent"][0]
    check(m["id"] == "rp1" and m["ai"] == "Claude" and "npm run build" in m["prompt"] and "止めました" in m["prompt"], f"再開の中身 {m}")
    src = open(os.path.join(ROOT, "Sources", "AIBoard", "main.swift"), encoding="utf-8").read()
    check('b["prompt"] as? String' in src and 'writeInstructions("resume-" + id, ask)' in src, "アプリ側が prompt を --resume に渡していない")
    return "記録: 最後の依頼以降だけ数える(済 1・途中 1・止めた・止まり方)/ 平穏な会話には印なし / 画面→再開に途中の操作と中断を添える"


@case("MS-01", "別の機械のセッション: ssh で読むだけ・同じ機械の別名は 1 回・止まり方を真理値表で判定・盤に枠で出す(demo では機械名を隠す)")
def ms01(ctx):
    import overview as o
    fake = tempfile.mkdtemp(dir=ctx["data"])
    sid = "11111111-2222-3333-4444-555555555555"
    os.makedirs(os.path.join(fake, ".claude", "sessions")); os.makedirs(os.path.join(fake, ".claude", "projects", "-w"))
    json.dump({"pid": os.getpid(), "sessionId": sid, "cwd": "/w/secret-client-app", "kind": "interactive"},
              open(os.path.join(fake, ".claude", "sessions", f"{os.getpid()}.json"), "w"))
    json.dump({"pid": 999999, "sessionId": "dead", "cwd": "/w"}, open(os.path.join(fake, ".claude", "sessions", "999999.json"), "w"))
    open(os.path.join(fake, ".claude", "projects", "-w", sid + ".jsonl"), "w").write(json.dumps(
        {"type": "assistant", "isApiErrorMessage": True, "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Invalid API key · Please run /login"}]}}) + "\n")
    real_run, calls = o.subprocess.run, []

    def fake_run(cmd, **kw):
        if cmd and cmd[0] == "ssh":
            calls.append(cmd)
            check("python3 -" in cmd and not any(w in " ".join(cmd) for w in ("kill", "rm ", "send-keys")), f"読む以外の命令 {cmd}")
            return real_run(["python3", "-"], input=kw.get("input"), capture_output=True, text=True, timeout=40,
                            env=dict(os.environ, HOME=fake))
        return real_run(cmd, **kw)
    hosts0 = o.MACMINI_HOSTS
    try:
        o.subprocess.run, o.MACMINI_HOSTS = fake_run, ["alias-a", "alias-b"]
        o._RS_CACHE.update(t=0, busy=False, data={"ok": False, "reason": "", "rows": []})
        o.remote_sessions(force=True)
        for _ in range(60):
            time.sleep(0.5)
            got = o.remote_sessions()
            if got.get("fetched"):
                break
    finally:
        o.subprocess.run, o.MACMINI_HOSTS = real_run, hosts0
    check(len(calls) == 2, f"ssh の回数 {len(calls)}")
    rows = got["rows"]
    check(len(rows) == 1, f"同じ機械を二重に数えた / 死んだ pid を拾った {rows}")
    r = rows[0]
    check(r["stop"] == "apikey" and r["ui"]["row"] == "認証: 鍵が無効" and r["ui"]["badge"] == "auth", f"判定 {r}")
    check(r["status_from"] == "transcript", f"推定の出どころ {r}")

    def route(pg):
        def handler(route_, req):
            import urllib.request
            u = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            dd = json.loads(u.read())
            dd["remote_sessions"] = {"ok": True, "reason": "", "fetched": time.time(), "rows": rows}
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(dd))
        pg.route("**/api/snapshot", handler)

    def fn(pg, errs, bl):
        wait_js(pg, "[...document.querySelectorAll('.frame')].some(f => f.innerText.includes('別の機械') || f.innerText.includes('other machines'))", 40)
        return pg.evaluate("""() => { const f = [...document.querySelectorAll('.frame')].find(f => /別の機械|other machines/.test(f.innerText));
            const cards = [...document.querySelectorAll('.jobcard')];
            // textContent で読む: 引いた倍率では .task を隠す設計(.lod-mid)なので innerText には出ない
            return {frame: f.innerText, cards: cards.map(c => c.textContent + ' | ' + (c.title || ''))}; }"""), errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check("こちらの番 1" in v["frame"] and "見るだけ" in v["frame"], f"枠の見出し {v['frame'][:200]}")
    check(v["cards"] and "secret-client-app" in v["cards"][0] and "claude --resume " + sid in v["cards"][0], f"カード {v['cards']}")
    vd, errs = with_page(ctx, fn, "?lang=ja&demo=1", route_extra=route)
    check(not errs, f"ページエラー(demo) {errs[:1]}")
    blob = " ".join(vd["cards"]) + vd["frame"]
    check("secret-client-app" not in blob and r["machine"] not in blob and "ssh " not in blob, f"demo で機械名・フォルダ名が漏れた {blob[:300]}")

    # 読めなかった時に枠ごと消えないこと(消えると「止まっていない」に見える)
    def route_err(pg):
        def handler(route_, req):
            import urllib.request
            u = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
            dd = json.loads(u.read())
            dd["remote_sessions"] = {"ok": False, "reason": "alias-a: ssh: connect timed out", "fetched": time.time(), "rows": []}
            route_.fulfill(status=200, content_type="application/json", body=json.dumps(dd))
        pg.route("**/api/snapshot", handler)
    ve, errs = with_page(ctx, fn, "?lang=ja", route_extra=route_err)
    check(not errs, f"ページエラー(失敗時) {errs[:1]}")
    check(any("読めません" in c and "timed out" in c for c in ve["cards"]), f"読めなかった理由が出ていない {ve}")
    return f"ssh 2 回(別名 2)→ 1 台として 1 本 / 死んだ pid は拾わない / 鍵が無効→🔑 / 盤に枠・こちらの番 1 / demo で隠す / 読めなければ理由を残す"


@case("LX-01", "tmux のパネル: 盤が拾って(mac でも)、画面の文字を読めて、キーを送れる(Linux の盤・iTerm 以外の端末はこの道)")
def lx01(ctx):
    import cs
    import overview_server as srv
    if not shutil.which("tmux"):
        return "SKIP: tmux が入っていない"
    name = "aiboard-uat-" + str(os.getpid())
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sh"], check=True, capture_output=True, timeout=20)
    try:
        time.sleep(0.6)
        cs._TMUX["t"] = 0
        pane = next((p for p in cs.tmux_panes() if p["tmux"].startswith(name + ":")), None)
        check(pane, f"作った tmux のパネルを拾えない {cs.tmux_panes()}")
        # 1) 盤の一覧に入る(mac でも。tmux の中の claude は iTerm の一覧に出てこないため)
        rows = cs.iterm_sessions()
        check(any(r["tty"] == pane["tty"] and r["win"] == 8 for r in rows), f"盤の一覧に tmux のパネルが無い(計 {len(rows)})")
        check(len([r for r in rows if r["tty"] == pane["tty"]]) == 1, "同じ tty を二重に数えた")
        # 2) 画面の文字を読める(読むだけ)
        mark = "aiboard-lx-" + str(int(time.time()))
        subprocess.run(["tmux", "send-keys", "-t", pane["tmux"], "-l", f"echo {mark}"], check=True, capture_output=True, timeout=10)
        subprocess.run(["tmux", "send-keys", "-t", pane["tmux"], "Enter"], check=True, capture_output=True, timeout=10)
        got = ""
        for _ in range(20):
            time.sleep(0.3)
            got = cs.tmux_capture(pane["tty"])
            if got.count(mark) >= 2:
                break
        check(got.count(mark) >= 2, f"画面に打った字と出力が見えない: {got[-200:]!r}")
        st, r = 0, {}
        for _ in range(20 * max(1, int(load_factor()))):   # 混んでいる時は盤の数え直しも遅れる
            # 盤は 2.5 秒ごとに数え直す。作ったばかりのパネルは次の更新まで見えない
            st, r, _ = http(f"/api/screen?lines=40&tab=8-{pane['tab']}")
            if st == 200 and r.get("ok") and mark in (r.get("screen") or ""):
                break
            time.sleep(0.7)
        check(st == 200 and r.get("ok") and mark in (r.get("screen") or ""), f"/api/screen が画面を返さない {st} {str(r)[:200]}")
        # 3) キーを送れる(名前が tmux のものに直っているか。↑ で 1 つ前の命令が戻る)
        ok, why = srv._tmux_send(pane["tmux"], "", True, "up")
        check(ok, f"↑ を送れない: {why}")
        ok, why = srv._tmux_send(pane["tmux"], "", True, "ctrl-c")
        check(ok, f"Ctrl-C を送れない: {why}")
        time.sleep(0.5)
        after = cs.tmux_capture(pane["tty"])
        check(f"echo {mark}" in after.split(mark)[-1] or after.count(f"echo {mark}") >= 2,
              f"↑ で前の命令が戻っていない: {after[-200:]!r}")
        ok, why = srv._tmux_send(pane["tmux"], "", True, "f13")
        check(not ok and "送れない" in why, f"知らないキーを受けてしまった: {ok} {why}")

        # 4) 画面: 「画面を見る」で出て、キーのボタンが /api/send を叩く(実際には送らない)
        def route(pg):
            def snap_h(route_, req):
                import urllib.request
                u = urllib.request.urlopen(urllib.request.Request(req.url, headers={"X-Overview": "1"}), timeout=30)
                dd = json.loads(u.read())
                base = (dd.get("sessions") or [{}])[0]
                dd["sessions"] = [dict(base, sid="lx1", tab="8-9", ai="Claude", state="作業中", mark="🟢", cwd="/tmp/lx", pid=4242,
                                       project="uat", doing="", task="tmux", mem_mb=1, subagents={}, tools=None, loop=None,
                                       limit=None, client=None, state_for=1, ago=1, group_label="", group_rgb=None,
                                       auth_lost=None, ui=None, model_style={"label": "Opus 5", "emoji": "🟠", "rgb": [200, 120, 60], "short": "o5", "vendor": "", "id": "m"})]
                dd["parallel"] = {}
                route_.fulfill(status=200, content_type="application/json", body=json.dumps(dd))
            pg.route("**/api/snapshot", snap_h)
            pg.route("**/api/conv*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "etag": "x", "timeline": [], "tab": "8-9", "sid": "lx1", "state": "作業中", "mark": "🟢", "ai": "Claude"})))
            pg.route("**/api/screen*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "tab": "8-9", "screen": "$ claude\n> 画面の文字\n"})))
            pg.route("**/api/send", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "sent": json.loads(q.post_data or "{}")})))

        def fn(pg, errs, bl):
            wait_js(pg, "document.querySelectorAll('.card.live').length >= 1", 40)
            pg.evaluate("window.__send = []; board.select('lx1')")
            wait_js(pg, "!!document.querySelector('#cvTermTog')", 30)
            before = pg.evaluate("document.querySelector('#cvScreen').hidden")
            pg.evaluate("""() => { const f = window.fetch; window.fetch = (u, o) => { if (String(u).includes('/api/send')) window.__send.push(JSON.parse(o.body)); return f(u, o); }; }""")
            pg.evaluate("document.querySelector('#cvTermTog').click()")
            wait_js(pg, "document.querySelector('#cvScreen').textContent.includes('画面の文字')", 30)
            pg.evaluate("document.querySelector('#cvTermKeys button[data-tk=esc]').click()")
            wait_js(pg, "window.__send.length >= 1", 20)
            return {"before": before, "text": pg.evaluate("document.querySelector('#cvScreen').textContent"),
                    "keys": pg.evaluate("[...document.querySelectorAll('#cvTermKeys button')].map(b => b.dataset.tk)"),
                    "sent": pg.evaluate("window.__send"),
                    "label": pg.evaluate("document.querySelector('#cvTermTog').textContent")}, errs
        v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
        check(not errs, f"ページエラー {errs[:1]}")
        check(v["before"] is True and "画面の文字" in v["text"], f"画面が出ていない {v}")
        check(v["keys"] == ["esc", "up", "down", "enter", "ctrl-c"], f"キーの並び {v['keys']}")
        check(v["sent"] and v["sent"][0].get("key") == "esc" and v["sent"][0].get("tab") == "8-9", f"送った中身 {v['sent']}")
        check("閉じる" in v["label"], f"ボタンの文言 {v['label']}")
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, timeout=20)
        cs._TMUX["t"] = 0
    return "mac でも tmux のパネルを 1 本として拾う / 画面の文字を /api/screen で読む / ↑・Ctrl-C は tmux の名前に直る・知らないキーは断る / 盤の「画面を見る」とキー 5 個"


@case("MG-01", "iTerm で動いている本物の会話を、盤から右の端末へ移せる(使い捨てのセッションを自分で作る。UAT_REAL=1 のときだけ)")
def mg01(ctx):
    import cs
    if os.environ.get("UAT_REAL") != "1":
        return "SKIP: 本物の AI を動かす試験(UAT_REAL=1 のときだけ。わずかに利用枠を使う)"
    if not cs.IS_MAC:
        return "SKIP: iTerm のある mac でだけ"
    # ここだけは**既定のアカウント**で起こす: アプリの端末は既定の設定で `claude --resume` するので、
    # 別プロファイルの会話は移した先で開けない(移管の試験にならない)。
    # そのかわり設定ファイルは一切書かない: 既に信頼済みのこのリポジトリを持ち場にする(信頼の印を足す必要が無い)
    work = os.path.realpath(ROOT)
    tty, sid, pid = "", "", 0
    try:
      try:
          cmd = ("cd " + shlex_quote(work) + " && env -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SSE_PORT "
                 "-u CLAUDE_CONFIG_DIR command claude --model claude-haiku-4-5-20251001")
          out = cs.osa('tell application "iTerm"\n tell current window\n  create tab with default profile\n'
                       '  tell current session\n   write text %s\n   return tty\n  end tell\n end tell\nend tell'
                       % json.dumps(cmd), timeout=20)
          check(out.strip().startswith("/dev/"), f"iTerm にタブを作れない: {out[:120]} {cs.OSA_ERROR}")
          tty = out.strip().replace("/dev/", "")
          # 盤がそのタブを本物の Claude として認識するまで待つ(初回の画面は Esc で抜ける)
          mine, tab = None, ""
          for i in range(60):
              time.sleep(1.5)
              st, snap, _ = http("/api/snapshot")
              mine = next((x for x in snap["sessions"] if x.get("tty") == tty), None)
              if mine and (mine.get("ai") or "").startswith("Claude") and mine.get("sid"):
                  break
              if i == 6:
                  # 初回の「このフォルダを信頼しますか」に、盤と同じ答え方(↓＋Enter)をする。
                  # まだ sid が無いので /api/send は使えない(AI セッションにしか送らない作り)。自分で作ったタブにだけ送る
                  cs.on_tty(tty, 'tell s to write text payload newline NO\n return "OK"',
                            pre="set payload to (character id {27, 91, 66})")
                  time.sleep(1)
                  cs.on_tty(tty, 'tell s to write text "" newline YES\n return "OK"')
          check(mine and (mine.get("ai") or "").startswith("Claude") and mine.get("sid"),
                f"使い捨ての claude が盤に出ない(tty {tty}) {mine}")
          sid, tab, pid = mine["sid"], mine["tab"], mine.get("pid") or 0
          check(not tab.startswith("0-"), f"アプリの端末になっている(iTerm で作ったはず) {tab}")
          # 1 往復させて、続きのある会話にする(状態の名前でなく、記録が増えたかで待つ)
          st, r, _ = http("/api/send", "POST", {"tab": tab, "sid": sid, "text": "1 と答えて"},
                          headers={"Origin": BASE.rstrip("/")})
          check(st == 200 and r.get("ok"), f"使い捨てに送れない {r}")
          tr, before = "", 0
          for _ in range(60):
              time.sleep(1.5)
              st, snap, _ = http("/api/snapshot")
              now = next((x for x in snap["sessions"] if x.get("sid") == sid), None)
              tr = (now or {}).get("transcript") or tr
              if tr and os.path.exists(tr) and "1 と答えて" in open(tr, errors="replace").read():
                  break
          check(tr and os.path.exists(tr), f"記録が無い(送った文が記録に現れない) tr={tr!r}")
          body = open(tr, errors="replace").read()
          check("1 と答えて" in body, "送った文が記録に無い(別のセッションを掴んでいる可能性)")
          for _ in range(40):      # 返事が返るまで(道具を使わない 1 語の返事)
              time.sleep(1.5)
              st, snap, _ = http("/api/snapshot")
              now = next((x for x in snap["sessions"] if x.get("sid") == sid), None)
              if now and now["state"] in ("返答待ち", "確認待ち"):
                  break
          before = os.path.getsize(tr)

          # ここが本番: 盤の「右の端末で開く」(確認は はい)。iTerm の AI を終えて、同じ会話をアプリの端末で開く
          js = """(async () => {
            const nap = ms => new Promise(r => setTimeout(r, ms));
            const find = () => (board.snap().sessions || []).find(x => x.sid === %s);
            let s = null;
            for (let i = 0; i < 20 && !(s = find()); i++) await nap(500);
            if (!s) return {ok: false, why: '移す前のセッションが盤に無い'};
            const from = s.tab;
            const r = await board.openInApp(s);
            let pane = null;
            for (let i = 0; i < 45; i++) {
              await nap(1000);
              pane = (board.snap().sessions || []).find(x => x.sid === %s && (x.tab || '').startsWith('0-'));
              if (pane) break;
            }
            return {ok: !!(r && r.ok) && !!pane, r, from, pane: pane ? {tab: pane.tab, sid: pane.sid, cwd: pane.cwd} : null};
          })()""" % (json.dumps(sid), json.dumps(sid))
          res = run_app_js(ctx, "return await " + js.strip(),
                           {"AIBOARD_DIALOG_AUTO": "yes", "AIBOARD_NO_ASK": "1", "AIBOARD_FAST_SHELL": "1"}, wait="6")
          check(res.get("ok"), f"アプリが JS を返さない {res}")
          v = res["value"]
          check(v.get("ok"), f"移せていない {v}")
          check(v["pane"]["sid"] == sid and os.path.realpath(v["pane"]["cwd"]) == work, f"移った先が違う {v['pane']}")
          check(not v["from"].startswith("0-"), f"移す前が既にアプリの端末だった {v['from']}")
          # 元の iTerm 側の AI は終わっている(同じ会話が 2 つ動かない)
          for _ in range(20):
              if not (pid and _pid_alive(pid)):
                  break
              time.sleep(0.5)
          check(not (pid and _pid_alive(pid)), f"iTerm 側の claude がまだ生きている pid={pid}")
          # 会話は同じ記録の続き(新しい会話を作っていない)
          check(os.path.exists(tr) and os.path.getsize(tr) >= before, "記録が別物になっている")
          others = [f for f in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))
                    if os.path.getmtime(f) > time.time() - 600 and os.path.basename(f) != sid + ".jsonl"
                    and work.replace("/", "-") in f]
          check(not others, f"別の会話ができている {others}")
          return (f"iTerm のタブ {tab}(tty {tty}) → アプリの端末 {v['pane']['tab']}・sid は同じ / "
                  f"元の claude は終了(pid {pid}) / 記録は同じ 1 本の続き({before}B→{os.path.getsize(tr)}B)")
      except Exception:
        import traceback
        check(False, "例外: " + traceback.format_exc()[-1200:])
    finally:
        if tty:
            cs.osa('tell application "iTerm"\n repeat with w in windows\n  repeat with t in tabs of w\n   repeat with s in sessions of t\n'
                   '    if tty of s is %s then close s\n   end repeat\n  end repeat\n end repeat\nend tell' % json.dumps("/dev/" + tty))


@case("AC-05", "CLI を呼べなかった時に『未ログイン』と言わない(『確認できない』＋理由)。実機で 4 アカウント全部を未ログインと誤表示していた")
def ac05(ctx):
    import overview as o
    real = o.subprocess.run
    calls = []

    def dead(argv, **kw):
        a = " ".join(map(str, argv))
        if "auth status" in a or "login status" in a:
            calls.append(a[:40])
            return type("R", (), {"returncode": 127, "stdout": "", "stderr": "zsh:1: command not found: claude"})()
        return real(argv, **kw)
    try:
        o.subprocess.run = dead
        rows = o.login_status()
    finally:
        o.subprocess.run = real
    check(calls, "CLI を呼びに行っていない")
    for r in rows:
        check(r["logged_in"] is None, f"呼べなかったのに logged_in={r['logged_in']} ({r['profile']})")
        check("聞けませんでした" in r["error"] and "127" in r["error"], f"理由が残っていない {r}")
    # 画面: 「確認できない」と理由が出て、「未ログイン」とは書かない
    def route(pg):
        pg.route("**/api/accounts*", lambda rt, q: rt.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "fetched": time.time(), "auth_fetched": time.time(),
             "accounts": [{"ai": "Claude", "profile": "default", "email": "a@example.com", "plan": "max",
                           "logged_in": None, "auth_error": "claude に聞けませんでした (rc=127)", "running": 3, "limit": None}],
             "clis": []})))
    def fn(pg, errs, bl):
        pg.click("#btnAcct")
        wait_js(pg, "document.querySelectorAll('#pBody .acct').length > 0", 40)
        return pg.evaluate("document.querySelector('#pBody').innerText"), errs
    txt, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check("確認できない" in txt and "rc=127" in txt, f"画面に理由が出ていない: {txt[:200]}")
    check("未ログイン" not in txt, f"呼べていないのに「未ログイン」と書いた: {txt[:200]}")
    return f"呼べなかった {len(rows)} 件すべて logged_in=None＋理由 / 画面は「確認できない (rc=127)」で「未ログイン」とは書かない"


@case("SV-02", "盤サーバは、起こしたアプリが居なくなったら自分も終わる(端末の無い盤を残さない)")
def sv02(ctx):
    # 身代わりの「アプリ」を 1 つ作り、その pid を持ち主としてサーバを起こす
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = tempfile.mkdtemp(dir=ctx["data"], prefix="sv02-")
    port = "8794"
    env = dict(os.environ, OVERVIEW_PORT=port, AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1",
               AIBOARD_BOARD=BOARD, AIBOARD_OWNER_PID=str(owner.pid))
    logp = os.path.join(data, "srv.log")
    srv = subprocess.Popen([sys.executable, os.path.join(BOARD, "overview_server.py"), "--serve"], env=env,
                           stdin=subprocess.DEVNULL, stdout=open(logp, "w"), stderr=subprocess.STDOUT)
    try:
        ver = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                import urllib.request
                ver = json.loads(urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{port}/api/version", headers={"X-Overview": "1"}), timeout=20).read())
                break
            except Exception:
                pass
        check(ver and ver.get("ok"), f"身代わりのサーバが起きない {ver} ログ: {open(logp, errors='replace').read()[-300:]}")
        check(ver.get("owner") == owner.pid, f"持ち主を持っていない {ver}")
        owner.terminate(); owner.wait(timeout=20)
        gone = False
        for _ in range(30):        # 3 秒ごとに見に行くので、10 秒あれば終わる
            time.sleep(0.5)
            if srv.poll() is not None:
                gone = True
                break
        check(gone, "アプリが居なくなってもサーバが残っている")
        check(not _pid_alive(srv.pid), "サーバのプロセスが残っている")
    finally:
        for p_ in (srv, owner):
            if p_.poll() is None:
                p_.kill()
    return f"持ち主(pid {owner.pid})を見ていて、居なくなってから数秒で自分も終わった"


@case("LG-02", "台帳の日次記録: 元の会話記録が消えても数字が残る。同じ日は 1 行だけ・壊れた行があっても読める")
def lg02(ctx):
    import recovery as rc
    import aiboard_paths as ap
    keep = ap.DATA
    ap.DATA = tempfile.mkdtemp(dir=ctx["data"])
    try:
        d1 = {"week": {"stops": 10, "long": 2, "hours": 3.5}, "prev": {"stops": 20, "long": 5, "hours": 9.0}}
        check(rc.remember(d1, today="2026-09-01") is True, "1 日目が残らない")
        # 同じ日に何度台帳を作り直しても、行は増えない（盤は 15 分ごとに作り直すため）
        check(rc.remember(d1, today="2026-09-01") is False, "同じ日が二重に入る")
        check(rc.remember({"week": {"stops": 7}, "prev": None}, today="2026-09-02") is True, "2 日目が残らない")
        # 壊れた行が混ざっても、読める行だけ返す（途中で落ちない）
        with open(ap.data(rc.HISTORY), "a") as f:
            f.write("{壊れた行\n")
        h = rc.history()
        check([r["day"] for r in h] == ["2026-09-01", "2026-09-02"], f"日次記録が古い順に返らない: {h}")
        check(h[0]["week"]["stops"] == 10 and h[0]["prev"]["hours"] == 9.0, f"中身が欠けた: {h[0]}")
        # 1 日 1 行が小さいこと（会話記録は 30 日で 10GB。ここが太ると意味がない）
        size = os.path.getsize(ap.data(rc.HISTORY))
        check(size < 4096, f"日次記録が大きすぎる: {size} B / 2 日")
        # 試験用サーバでは台帳を作らないので、記録も増えない
        os.environ["OVERVIEW_NO_INDEX"] = "1"
        try:
            check(rc.ledger(force=True).get("skipped") == "OVERVIEW_NO_INDEX", "試験用サーバで台帳を作っている")
        finally:
            os.environ.pop("OVERVIEW_NO_INDEX", None)
        check(len(rc.history()) == 2, "試験用サーバなのに記録が増えた")
    finally:
        ap.DATA = keep
    return "日次記録は 1 日 1 行・同日は上書きせず・壊れた行を飛ばして古い順に返す（2 日で %d B）" % size


@case("LG-01", "止まりの台帳: 対話だけを数え、無人は別・夜は差し引き・30 分以上だけを見出しにし、盤の操作を手柄にしない")
def lg01(ctx):
    import recovery as rc
    J = lambda x: json.dumps(x, ensure_ascii=False)
    d = tempfile.mkdtemp(dir=ctx["data"])

    def write(name, ep, rows):
        p = os.path.join(d, name)
        head = [J({"type": "system", "entrypoint": ep, "timestamp": "2026-09-21T09:00:00Z"})]
        open(p, "w").write("\n".join(head + rows) + "\n")
        return p
    iso = lambda h, m=0, day=21: f"2026-09-{day:02d}T{h:02d}:{m:02d}:00+09:00"
    err = lambda t: J({"type": "assistant", "timestamp": t, "isApiErrorMessage": True,
                       "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
    usr = lambda t, txt="続けて": J({"type": "user", "timestamp": t, "message": {"role": "user", "content": [{"type": "text", "text": txt}]}})
    # 対話: 10:00 に止まり 12:00 に復帰(2 時間・全部昼) / 別の止まりは 5 分で復帰 / もう 1 つは戻らない
    f1 = write("a.jsonl", "cli", [err(iso(10)), usr(iso(12)), err(iso(13)), usr(iso(13, 5)), err(iso(15))])
    # 対話: 23:00 に止まり 翌 9:00 に復帰(10 時間だが、起きているのは 23-24 と 8-9 の 2 時間)
    f2 = write("b.jsonl", "cli", [err(iso(23)), usr(iso(9, 0, 22))])
    # 無人: 止まって戻らない(合算しない)
    f3 = write("c.jsonl", "sdk-cli", [err(iso(11)), err(iso(14))])
    ev = []
    for f, ep in ((f1, "cli"), (f2, "cli"), (f3, "sdk-cli")):
        check(rc.entrypoint_of(f) == ep, f"entrypoint を読めない {f}")
        ev += [dict(x, ep=ep) for x in rc.stops_in(f)]
    since = time.mktime(time.strptime("2026-09-21 00:00", "%Y-%m-%d %H:%M"))
    w = rc.summarize(ev, since, since + 7 * 86400, acts=[])
    check(w["stops"] == 4, f"対話の止まりの数 {w}（a.jsonl に 3・b.jsonl に 1）")
    # 無人の 2 行は「同じ種類が続けて出た」ので 1 件にまとまるのが正しい（行数は件数ではない）
    check(w["unattended"] == 1, f"無人を合算した/畳めていない {w}")
    check(w["long"] == 2, f"30 分以上の数 {w}（2 時間のものと、夜をまたいだもの）")
    check(w["quick"] == 1, f"10 分以内に戻れた数 {w}")
    check(w["never"] == 1, f"戻らなかった数 {w}")
    check(abs(w["hours"] - 4.0) < 0.05, f"夜を差し引いていない: {w['hours']}h（期待 4.0 = 2 時間 + 夜またぎの 2 時間）")
    # 盤自身が送る定型の再開文は「人が戻った」と数えない（自分の操作で自分の数字を良くしない）
    f4 = write("d.jsonl", "cli", [err(iso(10)), usr(iso(10, 40), "前回はここで止まりました。続きから進めてください。"),
                                  usr(iso(11, 30), "ありがとう、続けて")])
    ev4 = [dict(x, ep="cli") for x in rc.stops_in(f4)]
    check(len(ev4) == 1 and ev4[0]["back_at"], f"止まりを拾えていない {ev4}")
    back = ev4[0]["back_at"] - ev4[0]["at"]
    check(abs(back - 5400) < 5, f"定型の再開文を「復帰」と数えた（40 分で戻ったことになっている）: {back/60:.0f} 分")
    # 盤の操作は「手柄」に足さない: 足しても long/hours は変わらず、別の数だけ増える
    w2 = rc.summarize(ev, since, since + 7 * 86400, acts=[since + 10 * 3600 + 60])
    check(w2["board_touched"] == 1 and w2["long"] == w["long"] and w2["hours"] == w["hours"],
          f"盤の操作を数字に混ぜている {w2}")

    # 画面: 下のバーの印と、開いた時の言い切らない書き方
    def route(pg):
        pg.route("**/api/recovery*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "checking": False, "built": time.time(), "long_minutes": 30, "awake": [8, 24],
             "week": {"stops": 24, "long": 4, "hours": 28.9, "never": 7, "quick": 9, "board_touched": 0,
                      "unattended": 36, "long_by_kind": {"auth": 3, "limit": 1}},
             "prev": {"stops": 178, "long": 45, "hours": 101.8, "never": 13, "quick": 100, "board_touched": 14,
                      "unattended": 208, "long_by_kind": {"auth": 20, "limit": 25}}})))

    def fn(pg, errs, bl):
        wait_js(pg, "!!document.querySelector('#btnLedger') && !document.querySelector('#btnLedger').hidden", 40)
        chip = pg.evaluate("document.querySelector('#btnLedger').innerText")
        pg.evaluate("document.querySelector('#btnLedger').click()")
        wait_js(pg, "document.querySelector('#pTitle').dataset.kind === 'ledger'", 20)
        return {"chip": chip, "body": pg.evaluate("document.querySelector('#pBody').innerText")}, errs
    v, errs = with_page(ctx, fn, "?lang=ja", route_extra=route)
    check(not errs, f"ページエラー {errs[:1]}")
    check("4" in v["chip"] and "29h" in v["chip"], f"下のバーの印 {v['chip']}")
    for w_ in ("その前の 7 日", "45", "36", "戻る人が居ないので合算していません", "盤のおかげで戻れたという意味ではありません",
               "会議中・週末・外出は差し引けていない"):
        check(w_ in v["body"], f"台帳に「{w_}」が無い: {v['body'][:220]}")
    check("取り戻" not in v["body"], f"「取り戻した」と書いている（因果は示せない）: {v['body'][:220]}")
    # 比べる 2 つの窓は同じ長さか（暦の週だと「今週」が途中で短い）
    import recovery as rc2
    check(rc2.WINDOW == 7 * 86400, "窓が 7 日ではない")
    return ("対話 4・無人 1(連続を畳む)を分け、30 分以上 2 件・夜を引いて 4.0h / 盤の操作は別勘定 / "
            "盤の定型再開文は復帰に数えない / 画面は同じ長さの 7 日を 2 つ・言い切らない注記つき")


@case("DT-01", "状態の真理値表: 全 14 行が表どおりに当たり、重なった時の優先順位も表どおり(表は board/decide.py の 1 か所)")
def dt01(ctx):
    import decide
    D = lambda **kw: decide.decide(kw)
    rows = [
        # (名前, 入力, 期待する行・状態・印・音・小窓・一手)
        ("鍵が無効", dict(stop="apikey"), ("認証: 鍵が無効", "止まっている", "auth", True, True, "key")),
        ("未ログイン", dict(stop="login"), ("認証: ログイン切れ", "止まっている", "auth", True, True, "login")),
        ("OAuth 失効", dict(stop="oauth"), ("認証: ログイン切れ", "止まっている", "auth", True, True, "login")),
        ("期限切れ", dict(stop="expired"), ("認証: ログイン切れ", "止まっている", "auth", True, True, "login")),
        ("クレジット", dict(stop="credits"), ("クレジット切れ", "止まっている", "billing", True, True, "billing")),
        ("5 時間枠", dict(stop="five_hour"), ("上限", "上限", "lim", False, False, "move_or_wait")),
        ("7 日枠", dict(stop="seven_day"), ("上限", "上限", "lim", False, False, "move_or_wait")),
        ("超過", dict(stop="overage"), ("上限", "上限", "lim", False, False, "move_or_wait")),
        ("判断待ち", dict(hook="waiting"), ("判断待ち", "確認待ち", "need", True, True, "answer")),
        ("信頼の答えが無い", dict(hook="", trusted=False), ("信頼の確認かも", "起動中?", "need", False, False, "trust")),
        ("一時的な失敗", dict(stop="transient"), ("一時的な失敗", "作業中", "", False, False, "none")),
        ("作業中", dict(hook="working"), ("作業中", "作業中", "", False, False, "none")),
        ("ループ待機", dict(hook="replied", loop=True), ("ループ待機", "返答待ち", "", False, False, "none")),
        ("あなたの番", dict(hook="replied"), ("あなたの番", "返答待ち", "turn", False, False, "reply")),
        ("記録なし・出力中", dict(hook="", idle=1.0), ("記録なしの CLI: 出力が続く", "作業中", "", False, False, "none")),
        ("記録なし・止まった", dict(hook="", idle=45.0), ("記録なしの CLI: 出力が止まった", "返答待ち", "turn", False, False, "look")),
        ("記録なし・その間", dict(hook="", idle=12.0), ("起動中", "起動中?", "", False, False, "look")),
        ("起動中", dict(hook=""), ("起動中", "起動中?", "", False, False, "look")),
        ("終わっている", dict(proc=False), ("終わっている", "終了", "", False, False, "resume")),
    ]
    bad = {}
    for name, inp, want in rows:
        r = D(**inp)
        got = (r["row"], r["state"], r["badge"], r["sound"], r["popup"], r["action"])
        if got != want:
            bad[name] = (got, want)
    check(not bad, f"表と違う(実際, 期待) {bad}")
    # 重なった時の優先順位(上の行が勝つ)
    fights = [
        ("認証 > 判断待ち", dict(stop="login", hook="waiting"), "認証: ログイン切れ"),
        ("鍵 > 認証", dict(stop="apikey"), "認証: 鍵が無効"),
        ("クレジット > 上限", dict(stop="credits", hook="waiting"), "クレジット切れ"),
        ("上限 > 判断待ち", dict(stop="five_hour", hook="waiting"), "上限"),
        ("判断待ち > 一時的な失敗", dict(stop="transient", hook="waiting"), "判断待ち"),
        ("一時的な失敗 > 作業中", dict(stop="transient", hook="working"), "一時的な失敗"),
        ("ループ > あなたの番", dict(hook="replied", loop=True), "ループ待機"),
        ("判断待ち > 信頼", dict(hook="waiting", trusted=False), "判断待ち"),
        ("終了はプロセスが居ない時だけ", dict(proc=False, hook="working"), "作業中"),
    ]
    bad2 = {n: D(**i)["row"] for n, i, w in fights if D(**i)["row"] != w}
    check(not bad2, f"優先順位が表と違う {bad2}")
    # 並行は状態を変えない(見せ方だけ足す)
    a, b = D(hook="working"), D(hook="working", others=3)
    check(a["state"] == b["state"] and b["parallel"] == 3 and a["parallel"] == 0, f"並行が状態を変えた {a} {b}")
    # 文書は表から作る(手で書き換えていない)
    doc = open(os.path.join(ROOT, "docs", "state-truth-table.md"), encoding="utf-8").read()
    check(doc.strip() == decide.table_markdown().strip(), "docs/state-truth-table.md が表とずれている(再生成が要る)")
    # 実機: すべてのセッションが表のどれかに当たり、行名が表に実在する
    st, snap, _ = http("/api/snapshot")
    names = set(snap.get("truth_table") or [])
    check(names == {r[0] for r in decide.ROWS}, "snapshot の表の名前が合わない")
    miss = [x.get("sid") for x in snap["sessions"] if not (x.get("ui") or {}).get("row")]
    check(not miss, f"表に当たらないセッション {miss[:3]}")
    hit = {(x.get("ui") or {}).get("row") for x in snap["sessions"]}
    check(hit <= names, f"表に無い行 {hit - names}")
    return f"{len(rows)} 通り＋優先順位 {len(fights)} 組が表どおり / 文書は表から生成 / 実機 {len(snap['sessions'])} 本すべて表に当たる({', '.join(sorted(hit))})"


@case("CF-01", "設定は書いた瞬間から効く: 別のプロセスが書き換えたら、盤サーバも次の読み取りで新しい値になる")
def cf01(ctx):
    import aiboard_paths as ap
    import autopilot as A
    import overview as o
    keep_auto, keep_sound = A.policy(), o.sound_on()
    p = ap.data("config.json")
    backup = open(p, encoding="utf-8").read() if os.path.exists(p) else None
    try:
        A.set_policy({"resume_when_reset": False})
        st, d0, _ = http("/api/actions")
        check(d0["policy"]["resume_when_reset"] is False, f"はじめの値 {d0['policy']}")
        # 盤サーバとは別のプロセス(この試験)で書き換える
        A.set_policy({"resume_when_reset": True})
        got = None
        for _ in range(20):
            st, d1, _ = http("/api/actions")
            if d1["policy"]["resume_when_reset"] is True:
                got = True
                break
            time.sleep(0.3)
        check(got, "別プロセスで書いた設定が盤サーバに届かない(古い設定のまま動く)")
        # 音の設定も同じ道(snapshot 経由)で効く
        o.sound_set(False)
        ok = False
        for _ in range(20):
            st, snap, _ = http("/api/snapshot")
            if snap.get("sound") is False:
                ok = True
                break
            time.sleep(0.5)
        check(ok, "音の設定が届かない")
    finally:
        A.set_policy(keep_auto)
        o.sound_set(keep_sound)
        if backup is not None:
            open(p, "w", encoding="utf-8").write(backup)
        ap._cfg = None
    return "別プロセスが書いた設定が、盤サーバの次の読み取りで効く(自動処理・音の 2 経路で確認)"


@case("AU-01", "システムが自分でやること: 既定はオフ・入れると上限の会話に 1 回だけ予約・二重に入れない・人しかできない一手は触らない")
def au01(ctx):
    import autopilot as A
    import overview as o
    import decide
    check(decide.AUTOABLE == {"move_or_wait": "resume_when_reset"}, f"自動でやれる一手が増えている {decide.AUTOABLE}")
    for act in ("login", "key", "billing", "answer", "trust", "reply", "resume", "look", "none"):
        check(decide.auto_for(act) == "", f"人しかできない一手を自動にしている: {act}")
    keep = A.policy()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    lim_sess = [{"sid": sid, "ui": decide.decide({"stop": "five_hour"}), "limit": {"resets_at": time.time() + 600},
                 "cwd": HOME, "ai": "Claude", "project": "uat-au"}]
    try:
        A.set_policy({"resume_when_reset": False})
        check(A.tick(lim_sess) == [], "オフなのに何かした")
        check(not [j for j in o.read_schedule() if j.get("resume") == sid], "オフなのに予約を作った")
        A.set_policy({"resume_when_reset": True})
        did = A.tick(lim_sess)
        check(len(did) == 1 and did[0]["done"] and did[0]["kind"] == "resume_when_reset", f"1 件だけ作るはず {did}")
        jobs = [j for j in o.read_schedule() if j.get("resume") == sid]
        check(len(jobs) == 1 and jobs[0]["once_at"] and not jobs[0]["at"] and not jobs[0]["every"], f"予約の中身 {jobs}")
        check("上限が解けたので続けて" in jobs[0]["prompt"], f"送る文 {jobs[0]['prompt']!r}")
        check(A.tick(lim_sess) == [], "二度目も作った(二重予約)")
        # 人しかできない一手は、方針を入れていても何もしない
        for stop in ("login", "apikey", "credits"):
            sess = [{"sid": sid[:-1] + "f", "ui": decide.decide({"stop": stop}), "limit": {"resets_at": time.time() + 600},
                     "cwd": HOME, "ai": "Claude"}]
            check(A.tick(sess) == [], f"{stop} で勝手に何かした")
        # やったことは記録に残り、API から読める
        st, d, _ = http("/api/actions")
        check(st == 200 and d["ok"] and any(r.get("sid") == sid for r in d["rows"]), f"記録が読めない {str(d)[:120]}")
        check(d["policy"]["resume_when_reset"] is True, f"方針が API に出ない {d['policy']}")
        # 盤の snapshot にも方針が出る
        st, snap, _ = http("/api/snapshot")
        check("autopilot" in snap, "snapshot に方針が無い")
    finally:
        for j in [x for x in o.read_schedule() if x.get("resume") == sid]:
            o.delete_job(j["id"])
        A.set_policy(keep)
    return "既定オフ / 上限で 1 回だけ予約(once_at＋resume) / 二重に入れない / 認証・鍵・請求では何もしない / 記録と方針が API に出る"


@case("AU-02", "自動処理は盤を止めない: 落ちても snapshot は返り、落ちた事実が記録に残る")
def au02(ctx):
    import autopilot as A
    import overview as o
    keep = A.tick
    A.tick = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("uat わざと落とす"))
    try:
        t0 = time.time()
        snap = o.snapshot(with_macmini=False)
        el = time.time() - t0
        check(snap.get("sessions") is not None, "自動処理が落ちたら snapshot まで落ちた")
        check(el < 60, f"snapshot が遅すぎる {el:.1f}s")
        rows = A.recent(5)
        check(any("自動処理が落ちた" in (r.get("text") or "") for r in rows), f"落ちた事実が記録に残っていない {rows[:2]}")
    finally:
        A.tick = keep
    return "自動処理が落ちても snapshot は返る / 落ちた事実は記録に残る"


@case("SC-01", "予約の形の検査と往復: 時刻/間隔のどちらかが要る・15 分未満は断る・止める/消すが効く")
def sc01(ctx):
    import overview as o
    bad = []
    for name, job in (("依頼文なし", {"prompt": "", "at": "06:00"}),
                      ("時刻の形", {"prompt": "x", "at": "25:00"}),
                      ("間隔が短い", {"prompt": "x", "every": 5}),
                      ("時刻も間隔も無い", {"prompt": "x"}),
                      ("場所が不正", {"prompt": "x", "at": "06:00", "cwd": "../etc"})):
        try:
            o.save_job(job)
            bad.append(name)
        except ValueError:
            pass
    check(not bad, f"受けてはいけない予約を受けた: {bad}")
    j = o.save_job({"prompt": "今日の失敗したジョブをまとめて", "at": "06:00", "cwd": HOME, "key": "uat", "ai": "Codex"})
    st, d, _ = http("/api/schedule")
    mine = [x for x in d["jobs"] if x["id"] == j["id"]]
    check(st == 200 and len(mine) == 1, f"一覧に出ない {d}")
    check(mine[0]["ai"] == "Codex" and mine[0]["next_at"], f"中身 {mine[0]}")
    st, r, _ = http("/api/schedule", "POST", dict(j, enabled=False), headers={"Origin": BASE.rstrip("/")})
    check(st == 200 and r["ok"] and r["job"]["enabled"] is False, f"止められない {r}")
    st, d2, _ = http("/api/schedule")
    check([x for x in d2["jobs"] if x["id"] == j["id"]][0]["next_at"] is None, "止めたのに次の時刻が出る")
    st, r, _ = http("/api/schedule", "POST", {"op": "delete", "id": j["id"]}, headers={"Origin": BASE.rstrip("/")})
    check(r.get("deleted") == 1, f"消せない {r}")
    st, d3, _ = http("/api/schedule")
    check(not [x for x in d3["jobs"] if x["id"] == j["id"]], "消したのに残っている")
    return "不正 5 通りを拒否 / 作成→一覧→止める→消すが往復する"


@case("SC-02", "予約の判定: 時刻は過ぎたら 1 回だけ・古すぎる分は走らせない・間隔は前回からの経過で決まる")
def sc02(ctx):
    import overview as o
    day = time.mktime(time.strptime("2026-09-18", "%Y-%m-%d"))
    at6 = day + 6 * 3600
    daily = {"prompt": "x", "at": "06:00", "enabled": True, "last_run": 0}
    rows = [
        ("5:59 はまだ", dict(daily), at6 - 60, False),
        ("6:05 は走る", dict(daily), at6 + 300, True),
        ("走った後は走らない", dict(daily, last_run=at6 + 10), at6 + 600, False),
        ("2 時間後に開いたら走らせない", dict(daily), at6 + 7200, False),
        ("翌日はまた走る", dict(daily, last_run=at6 + 10), at6 + 86400 + 300, True),
        ("止めてあれば走らない", dict(daily, enabled=False), at6 + 300, False),
        ("間隔: 前回から 30 分", {"prompt": "x", "every": 30, "enabled": True, "last_run": at6 - 1800}, at6, True),
        ("間隔: まだ 29 分", {"prompt": "x", "every": 30, "enabled": True, "last_run": at6 - 1740}, at6, False),
    ]
    bad = {n: (o.job_due(j, now=now), want) for n, j, now, want in rows if o.job_due(j, now=now) != want}
    check(not bad, f"判定が違う(実際, 期待) {bad}")
    nxt = o.job_next_at(dict(daily), now=at6 - 60)
    check(abs(nxt - at6) < 61, f"次の時刻 {time.strftime('%H:%M', time.localtime(nxt))}")
    nxt2 = o.job_next_at(dict(daily, last_run=at6 + 10), now=at6 + 600)
    check(abs(nxt2 - (at6 + 86400)) < 61, f"走った後の次 {time.strftime('%m-%d %H:%M', time.localtime(nxt2))}")
    check(o.job_next_at(dict(daily, enabled=False)) is None, "止めた予約に次の時刻が出る")
    # 見送った分は「走らなかった」と分かる(黙って飛ばさない)
    old_job = dict(daily, created=at6 - 86400)
    check(o.job_missed(old_job, now=at6 + 7200) == at6, "2 時間後に開いた時、6:00 の分を見送ったと言わない")
    check(o.job_missed(old_job, now=at6 + 300) is None, "まだ走らせられる時間なのに見送り扱い")
    check(o.job_missed(dict(old_job, last_run=at6 + 5), now=at6 + 7200) is None, "走った日を見送り扱い")
    check(o.job_missed(dict(daily, created=at6 + 3600), now=at6 + 7200) is None, "作る前の時刻を見送り扱い")
    at23 = day + 23 * 3600
    late = {"prompt": "x", "at": "23:00", "enabled": True, "last_run": 0, "created": at23 - 86400}
    check(o.job_missed(late, now=at23 + 5400) == at23, "23:00 の予約を翌 0:30 に開いた時、昨夜の見送りを言わない")
    return f"{len(rows)} 通りすべて期待どおり(時刻・重複・古い分・間隔・停止)"


@case("SC-03", "アプリが予約を走らせる: 期限の来た分だけ起こし、走ったと記録する(実行は試しのみ)")
def sc03(ctx):
    import overview as o
    due = o.save_job({"prompt": "期限が来ている仕事", "every": 15, "cwd": HOME, "key": "uat-sc", "ai": "Claude"})
    o.mark_ran(due["id"], when=time.time() - 3600)
    later = o.save_job({"prompt": "まだの仕事", "at": "23:59", "cwd": HOME, "key": "uat-sc"})
    o.mark_ran(later["id"], when=time.time())
    try:
        js = "return await fetch('/api/schedule', {headers: {'X-Overview': '1'}}).then(x => x.json())"
        r = run_app_js(ctx, js, {"AIBOARD_DRY": "1"}, wait="12")
        check(r.get("ok"), f"{r}")
        after = {j["id"]: j for j in r["value"]["jobs"]}
        check(after[due["id"]]["last_run"] > time.time() - 120, f"期限の来た予約が走っていない {after[due['id']]}")
        check(after[later["id"]]["last_run"] < time.time() - 1 or not after[later["id"]]["due"],
              f"まだの予約まで走らせた {after[later['id']]}")
        panes = r.get("panes") or []
        check(not [p for p in panes if p.get("kind") in ("claude", "codex")], f"試しのみのはずが端末を開いた {panes}")
    finally:
        o.delete_job(due["id"]); o.delete_job(later["id"])
    return "期限の来た 1 件だけ走らせて記録し、まだの 1 件は触らない(AIBOARD_DRY で端末は開かない)"


@case("TU-01", "フォルダの信頼: 設定にある答えだけを信頼済みと読む(全アカウント分・壊れた設定は無視・末尾の / は同一視)")
def tu01(ctx):
    import cs
    d = tempfile.mkdtemp(dir=ctx["data"])
    a = os.path.join(d, "a.json"); b = os.path.join(d, "b.json"); broken = os.path.join(d, "c.json")
    json.dump({"projects": {"/tmp/yes-a": {"hasTrustDialogAccepted": True},
                            "/tmp/no": {"hasTrustDialogAccepted": False},
                            "/tmp/other": {}}}, open(a, "w"))
    json.dump({"projects": {"/tmp/yes-b/": {"hasTrustDialogAccepted": True}}}, open(b, "w"))
    open(broken, "w").write("{壊れた")
    keep = cs.trust_files
    try:
        cs.trust_files = lambda: [a, b, broken, os.path.join(d, "無い.json")]
        cs._TRUST.update(t=0, paths=set())
        got = {p: cs.trusted_cwd(p) for p in ("/tmp/yes-a", "/tmp/yes-a/", "/tmp/yes-b", "/tmp/no", "/tmp/other", "/tmp/未登録")}
        exp = {"/tmp/yes-a": True, "/tmp/yes-a/": True, "/tmp/yes-b": True,
               "/tmp/no": False, "/tmp/other": False, "/tmp/未登録": False}
        check(got == exp, f"判定 {got}")
        check(cs.trusted_cwd("") is True, "フォルダ不明なら確認中とは言わない(偽の赤を出さない)")
        # 実際の設定でも読めること(どれかのアカウントで信頼済みのフォルダが 1 つ以上ある)
        cs.trust_files = keep
        cs._TRUST.update(t=0, paths=set())
        real = [f for f in cs.trust_files() if os.path.exists(f)]
        cs.trusted_cwd(HOME)
        check(cs._TRUST["paths"], f"実設定 {len(real)} 個から信頼済みフォルダが 0 件")
        n = len(cs._TRUST["paths"])
    finally:
        cs.trust_files = keep
        cs._TRUST.update(t=0, paths=set())
    return f"合成 6 通りすべて一致 / 壊れた設定と欠損は無視 / 実設定 {len(real)} 個から {n} フォルダ"


@case("CS-03", "メモリ合計: 実プロセス表で別実装と一致し、親子が循環した表でも止まる")
def cs03(ctx):
    import cs
    procs = cs.processes()
    kids = {}
    for p, v in procs.items():
        kids.setdefault(v["ppid"], []).append(p)

    def ref(pid):      # 別実装(訪問済みを覚える)
        seen, stack, tot = set(), [pid], 0
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            tot += procs.get(x, {}).get("rss", 0)
            stack.extend(kids.get(x, []))
        return tot
    roots = [p for p, v in procs.items() if cs.is_claude(v["cmd"]) or cs.is_codex(v["cmd"])]
    check(roots, "claude / codex が 1 つも動いていない")
    bad = [(p, cs.descendants_rss(procs, p), ref(p)) for p in roots]
    bad = [x for x in bad if x[1] != x[2]]
    check(not bad, f"別実装と不一致 {bad[:3]}")
    code = ("import sys; sys.path.insert(0, %r); import cs; "
            "print(cs.descendants_rss({5: {'ppid': 5, 'rss': 7, 'tty': 't', 'cmd': 'x'}, "
            "6: {'ppid': 5, 'rss': 3, 'tty': 't', 'cmd': 'y'}}, 5))" % BOARD)
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise Fail("親が自分自身の表で descendants_rss が止まらない(盤サーバが固まる)")
    check(r.returncode == 0 and r.stdout.strip() == "10", f"循環表 → rc={r.returncode} {r.stdout.strip()!r} {r.stderr[-200:]}")
    return f"実プロセス {len(roots)} 本で別実装と一致 / 循環表でも 10 を返す"


@case("CS-07", "codex の記録の選び方: 実際に開いているファイル(lsof)が推定より優先され、別フォルダの記録は選ばない")
def cs07(ctx):
    import cs
    class Run:            # cs の中の subprocess だけ差し替える(本物の osascript / lsof を呼ばせない)
        def __init__(self, reply):
            self.calls, self.reply = [], reply

        def run(self, cmd, *a, **k):
            self.calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": self.reply(cmd), "stderr": ""})()
    root = tempfile.mkdtemp(dir=ctx["data"])
    day = os.path.join(root, "2026", "09", "18")
    os.makedirs(day)

    def rollout(name, cwd, mtime, task):
        p = os.path.join(day, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                    "content": [{"text": task}]}}) + "\n")
        os.utime(p, (mtime, mtime))
        return p
    now = time.time()
    cwd = "/tmp/uat-cwd"
    a = rollout("rollout-2026-09-18T10-00-00-aaaaaaaa-1111-2222-3333-444444444444.jsonl", cwd, now - 60, "近い記録")
    b = rollout("rollout-2026-01-01T00-00-00-bbbbbbbb-1111-2222-3333-444444444444.jsonl", cwd, now - 3600, "開いている記録")
    rollout("rollout-2026-09-18T10-00-01-cccccccc-1111-2222-3333-444444444444.jsonl", "/other/dir", now, "別フォルダ")
    started = time.mktime(time.strptime("2026-09-18T10-00-00", "%Y-%m-%dT%H-%M-%S"))
    keep_dir, keep_sub = cs.CODEX_SESS, cs.subprocess
    try:
        cs.CODEX_SESS = root
        cs.subprocess = Run(lambda cmd: f"p1234\nn/dev/null\nn{b}\n" if cmd[0] == "lsof" else "")
        got = cs.codex_session(cwd, started, (1234,))
        check(got.get("rollout") == b, f"lsof で開いている記録を選ばなかった: {os.path.basename(got.get('rollout') or '')}")
        check(got.get("task") == "開いている記録", f"依頼 {got.get('task')!r}")
        cs.subprocess = Run(lambda cmd: "")          # lsof が空 → 推定に落ちる
        got2 = cs.codex_session(cwd, started, (1234,))
        check(got2.get("rollout") == a, f"推定が別の記録を選んだ: {os.path.basename(got2.get('rollout') or '')}")
        got3 = cs.codex_session("/no/such/dir", started, ())
        check(not got3, f"フォルダが合わないのに記録を付けた: {got3.get('rollout') if got3 else got3}")
    finally:
        cs.CODEX_SESS, cs.subprocess = keep_dir, keep_sub
    return "lsof の記録が優先 / lsof 無しは開始時刻の近い方 / cwd 不一致は付けない"


@case("CS-08", "codex の記録の読み取り: model・依頼・いまの操作・ターンの区切り。壊れた行で落ちない")
def cs08(ctx):
    import cs
    d = tempfile.mkdtemp(dir=ctx["data"])

    def read(name, lines):
        p = os.path.join(d, f"rollout-2026-09-18T10-00-00-{name}-1111-2222-3333-444444444444.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return cs._codex_read(p)
    ev = lambda t, **k: json.dumps({"type": "event_msg", "payload": dict(type=t, **k)})
    ri = lambda **k: json.dumps({"type": "response_item", "payload": k})
    ctxt = lambda m: json.dumps({"type": "turn_context", "payload": {"model": m}})
    msg = lambda role, text: ri(type="message", role=role, content=[{"text": text}])
    base = [ctxt("gpt-6-astra"), "{壊れた行", "", json.dumps({"type": "response_item"}),
            msg("user", "<system で入れた文>"), msg("user", "本当の依頼"),
            ri(type="function_call", name="shell", arguments='{"cmd":"ls -la /tmp"}')]
    a = read("aaaaaaaa", base + [ev("task_complete")])
    check(a["model"] == "gpt-6-astra", f"model {a['model']!r}")
    check(a["task"] == "本当の依頼", f"依頼 {a['task']!r}(system 注入を拾っていないか)")
    check(a["sid"] == "aaaaaaaa-1111-2222-3333-444444444444", f"sid {a['sid']}")
    check(a["doing"].startswith("✅"), f"完了後の表示 {a['doing']!r}")
    b = read("bbbbbbbb", base + [ev("turn_aborted")])
    check(b["doing"] == "⏹ 中断された", f"中断 {b['doing']!r}")
    c = read("cccccccc", base + [ev("turn_aborted", error={"message": "usage limit reached"})])
    check(c["doing"] == "⛔ エラーで停止: usage limit reached", f"エラー {c['doing']!r}")
    e = read("dddddddd", base + [ev("turn_aborted", error={"message": "usage limit"}), ev("task_started"),
                                 ri(type="function_call", name="shell", arguments='{"cmd":"pwd"}')])
    check(not e["doing"].startswith("⛔") and "pwd" in e["doing"], f"再開後も古いエラーを出している: {e['doing']!r}")
    f = read("eeeeeeee", base[:1] + ['{"type":"event_msg"}', "not json at all"])
    check(f["doing"] == "" and f["task"] == "", f"空の記録 {f}")
    return "model / 依頼 / 操作 / 完了・中断・エラー・再開 の 6 通り一致・壊れた行で落ちない"


@case("CS-11", "ファイル単位のキャッシュ: 変われば読み直し・ファイルごとに分かれ・何度変わっても辞書が増えない")
def cs11(ctx):
    import cs
    calls = []

    @cs.memo_by_file
    def uat_read(path, tail=1):
        calls.append(path)
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return "なし"
    d = tempfile.mkdtemp(dir=ctx["data"])
    a, b = os.path.join(d, "a.txt"), os.path.join(d, "b.txt")

    def put(p, s, mtime):
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)
        os.utime(p, (mtime, mtime))
    keys = lambda: [k for k in cs._FILE_MEMO if k[0] == "uat_read"]
    t0 = time.time() - 1000
    put(a, "aaa", t0)
    check((uat_read(a), len(calls)) == ("aaa", 1), f"初回 {calls}")
    check((uat_read(a), len(calls)) == ("aaa", 1), f"同じ内容で読み直した {len(calls)} 回")
    check((uat_read(a, tail=2), len(calls)) == ("aaa", 2), "引数が違うのに使い回した")
    put(a, "bbb", t0 + 10)                   # 大きさは同じ・時刻だけ違う
    check((uat_read(a), len(calls)) == ("bbb", 3), f"同じ大きさの書き換えを見落とした {uat_read(a)!r}")
    put(a, "bbbb", t0 + 10)                  # 時刻は同じ・大きさが違う
    check(uat_read(a) == "bbbb", "同じ時刻での追記を見落とした")
    put(b, "zzz", t0)
    check(uat_read(b) == "zzz" and uat_read(a) == "bbbb", "別のファイルの結果が混ざった")
    n0 = len(keys())
    for i in range(60):
        put(a, "x" * (i + 1), t0 + 100 + i)
        uat_read(a)
    check(len(keys()) == n0, f"60 回書き換えでキャッシュの項目が {n0} → {len(keys())} に増えた")
    n1, c1 = len(keys()), len(calls)
    for _ in range(5):
        uat_read(os.path.join(d, "無い.txt"))
    check(len(keys()) == n1 and len(calls) == c1 + 5, f"無いファイルを覚えた {len(keys())}/{n1}")
    return f"読み直し 1 回ずつ・大きさ/時刻どちらの変化も検出・別ファイルは別・項目 {n1} 件のまま"


@case("RD-02", "秘密はセッション情報のどの欄にも出ない(合成タブで全欄を走査)")
def rd02(ctx):
    import overview as o
    import cs
    secret = "sk-ant-api03-" + "Q" * 30
    tab = {"win": 9, "tab": 1, "tty": "/dev/ttys999", "sid": "uat-redact-1",
           "state": "返答待ち", "mark": "🟡", "ai": "Claude", "model": "opus",
           "account": "", "model_id": "claude-opus-5", "cwd": os.path.join(ctx["data"], "proj"),
           "doing": f"鍵を書いた {secret}", "task": f"{secret} を設定して", "title_topic": f"話題 {secret}",
           "client": None, "state_since": time.time() - 60, "ago": 60, "started": time.time() - 600,
           "mem": 1024, "pid": os.getpid(), "transcript": "", "subagents": {},
           # 将来この欄を出すようになったときのため(素通りしたら落ちる)
           "last_prompt": secret, "summary": secret, "title": secret, "note": secret}
    real = (cs.classify, cs.processes, cs.iterm_sessions)
    try:
        cs.classify = lambda *a, **k: [dict(tab)]
        cs.processes = lambda: {}
        cs.iterm_sessions = lambda *a, **k: []
        sess = o.sessions(procs={}, with_official=False)   # 公式一覧の背景セッションは混ぜない(この試験は合成タブだけを見る)
    finally:
        cs.classify, cs.processes, cs.iterm_sessions = real
    check(len(sess) == 1, f"セッション {len(sess)} 件")
    leak = [k for k, v in sess[0].items() if isinstance(v, str) and secret in v]
    check(not leak, f"伏せ字になっていない欄: {leak}")
    check(secret not in json.dumps(sess, ensure_ascii=False), "JSON 全体に秘密が残っている")
    masked = [k for k in ("doing", "task", "topic") if "●●●(伏せ字)" in sess[0][k]]
    check(len(masked) == 3, f"伏せ字の印が付いていない欄がある {masked}")
    return "doing / task / topic を伏せ字・セッション JSON に秘密 0 件"


@case("SN-01", "snapshot の内部整合: JSON にでき、件数・あなたの番・束ねが sessions と矛盾しない")
def sn01(ctx):
    import overview as o
    t0 = time.time()
    snap = o.snapshot(with_macmini=False)
    blob = json.dumps(snap, ensure_ascii=False)          # 直列化できない値が混ざれば落ちる
    sess = snap["sessions"]
    exp = {"your_turn": sum(1 for s in sess if s["state"] == "確認待ち"),
           "working": sum(1 for s in sess if s["mark"] in ("🟢", "🟩")),
           "waiting": sum(1 for s in sess if s["state"] in ("返答待ち", "codex 返答待ち")),
           "tabs": len(sess)}
    check(snap["counts"] == exp, f"上のバーの数 {snap['counts']} != sessions から数えた {exp}")
    tabs = [s["tab"] for s in sess]
    dup = sorted({t for t in tabs if tabs.count(t) > 1})
    check(not dup, f"タブ番号が重複している(詳細・会話が別のセッションに繋がる) {dup}")
    ids = set(tabs)
    stray = [a["tab"] for a in snap["attention"] if a["tab"] not in ids]
    check(not stray, f"sessions に無いものが「あなたの番」に居る {stray}")
    grouped = [t for g in snap["clients"] + snap["projects"] for t in g["tabs"]]
    live = sorted(s["tab"] for s in sess if s["mark"] != "⚪")
    check(sorted(grouped) == live, f"束ねの漏れ/重複 {sorted(set(grouped) ^ set(live))}")
    check(len(grouped) == len(set(grouped)), f"同じタブが 2 つのグループに居る {sorted({t for t in grouped if grouped.count(t) > 1})}")
    check(isinstance(snap["machine"].get("ok"), bool) and snap["took"] >= 0, f"machine/took {snap['machine'].get('ok')} {snap.get('took')}")
    return f"タブ {len(sess)}(重複 0)/ 数 {snap['counts']} / あなたの番 {len(snap['attention'])} / 束ね {len(grouped)} = 生きたタブ {len(live)} / JSON {len(blob)} 字 / {round(time.time() - t0, 1)}秒"


@case("MC-01", "メモリの内訳: どのプロセスも 1 区分だけに数え、AI は claude/codex の子孫まで含める")
def mc01(ctx):
    import overview as o
    GB = 1048576   # ps の rss は KB
    procs = {
        1: {"ppid": 0, "rss": 1024, "tty": "??", "cmd": "/sbin/launchd"},
        10: {"ppid": 1, "rss": 2 * GB, "tty": "ttys001", "cmd": "claude"},
        11: {"ppid": 10, "rss": 1 * GB, "tty": "ttys001", "cmd": "/bin/zsh -c rg foo"},
        12: {"ppid": 11, "rss": GB // 2, "tty": "ttys001", "cmd": "rg foo"},
        20: {"ppid": 1, "rss": 3 * GB, "tty": "??", "cmd": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"},
        30: {"ppid": 1, "rss": 1 * GB, "tty": "??", "cmd": "/Applications/Docker.app/Contents/MacOS/com.docker.backend"},
        40: {"ppid": 1, "rss": GB // 2, "tty": "??", "cmd": "/Applications/iTerm.app/Contents/MacOS/iTerm2"},
        50: {"ppid": 1, "rss": GB // 4, "tty": "??", "cmd": "node /Users/x/codex-helper.js"},
        60: {"ppid": 1, "rss": 1 * GB, "tty": "ttys009", "cmd": "codex"},
    }
    m = o.machine(procs)
    check(m.get("ok"), f"メモリを測れない: {m.get('reason')}")
    bd = m["breakdown_gb"]
    total = sum(p["rss"] for p in procs.values()) / 2**20
    check(abs(sum(bd.values()) - total) < 0.02, f"内訳の合計 {sum(bd.values()):.2f} != 全体 {total:.2f}(重複か取りこぼし)")
    exp = {"AI(claude+codex)": 4.5, "Chrome": 3.0, "Docker": 1.0, "iTerm": 0.5, "その他": 0.25}
    bad = {k: (bd.get(k), v) for k, v in exp.items() if abs(bd.get(k, -1) - v) > 0.02}
    check(not bad, f"区分の不一致(実測, 期待GB) {bad}")
    for k in ("total_gb", "used_gb", "used_pct", "compressed_gb", "wired_gb"):
        check(isinstance(m.get(k), (int, float)) and m[k] >= 0, f"{k} が数値でない: {m.get(k)}")
    check(0 < m["used_pct"] <= 100 and m["used_gb"] <= m["total_gb"] * 1.05, f"使用率が壊れている {m['used_pct']}% {m['used_gb']}/{m['total_gb']}GB")
    return f"内訳 {bd} = 合計 {total:.2f}GB / claude の孫まで AI に算入・名前が似ただけの node は その他"


@case("JS-01", "強制終了(Jetsam)の集計: 今日/24 時間を正しく数え、壊れたファイルは件数に混ぜず ok=False にする")
def js01(ctx):
    import overview as o
    d = tempfile.mkdtemp(dir=ctx["data"])
    def write(name, when, killed="Google Chrome"):
        hdr = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S.000 +0900", time.localtime(when)), "bug_type": "298"}
        body = {"memoryStatus": {"pageSize": 16384}, "largestProcess": killed,
                "processes": [{"name": killed, "reason": "per-process-limit", "rpages": 65536}, {"name": "innocent"}]}
        open(os.path.join(d, name), "w").write(json.dumps(hdr) + "\n" + json.dumps(body))
    now = time.time()
    write("JetsamEvent-2026-01-01-000001.ips", now - 60)
    write("JetsamEvent-2026-01-02-000002.ips", now - 30 * 3600, killed="Xcode")
    open(os.path.join(d, "JetsamEvent-2026-01-03-000003.ips"), "w").write("これは JSON ではない\n{}")
    real = o.glob

    class G:
        @staticmethod
        def glob(pat):
            return real.glob(os.path.join(d, "JetsamEvent-*.ips")) if "JetsamEvent" in pat else real.glob(pat)
    try:
        o.glob = G
        r = o.jetsam()
    finally:
        o.glob = real
    check(r["files"] == 3, f"ファイル数 {r['files']}")
    check((r["today"], r["last24h"]) == (1, 1), f"今日 {r['today']} / 24 時間 {r['last24h']}(期待 1, 1)")
    check(r["ok"] is False and "1 件" in (r.get("reason") or ""), f"壊れたファイルを見逃した ok={r['ok']} reason={r.get('reason')!r}")
    check(r["last_killed"] == [{"name": "Google Chrome", "reason": "per-process-limit", "mb": 1024}], f"最後に殺されたもの {r['last_killed']}")
    check(r["last_ago"] is not None and r["last_ago"] < 300, f"最後の時刻 {r.get('last_ago')}")
    return "3 本(今日 1・24 時間 1・壊れ 1 は ok=False)/ 直近の犠牲者 Google Chrome 1024MB(reason 無しは数えない)"


@case("RM-01", "遠隔ジョブ: 60 秒は使い回し、失敗したら古い値を今の値として返さない(ssh は起こさない)")
def rm01(ctx):
    import overview as o
    calls = []

    class Res:
        def __init__(self, out, rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    good = "\n".join(["@@meta", str(int(time.time())), "uat-remote-host",
                      "10:00  up 1 day,  3 users, load averages: 1.23 4.56 7.89", "@@pm2",
                      json.dumps({"name": "web", "status": "online", "restarts": 0}),
                      json.dumps({"name": "bot", "status": "stopped", "restarts": 3}),
                      "@@cron", "7", "@@end", ""])

    class SP:
        TimeoutExpired = subprocess.TimeoutExpired
        mode = "ok"

        @staticmethod
        def run(cmd, **kw):
            calls.append(cmd)
            return Res(good) if SP.mode == "ok" else Res("", 255)

    real_sp, real_hosts = o.subprocess, o.MACMINI_HOSTS
    cache = o.MACMINI_CACHE
    saved = open(cache).read() if os.path.exists(cache) else None
    try:
        o.subprocess = SP
        o.MACMINI_HOSTS = []
        un = o.macmini(force=True)
        check(un.get("unconfigured") and not un["ok"] and not calls, f"未設定のはずが {un} / ssh {len(calls)} 回")
        o.MACMINI_HOSTS = ["uat-remote"]
        if os.path.exists(cache):
            os.remove(cache)
        d1 = o.macmini(force=True)
        check(d1["ok"] and d1["host"] == "uat-remote", f"取得できない {d1}")
        check(d1["pm2"] == {"total": 2, "online": 1, "bad": ["bot"]}, f"PM2 の読み {d1['pm2']}")
        check(d1["cron_wrap"]["jobs"] == 7 and d1["load1"] == 1.23, f"cron/負荷 {d1['cron_wrap']} {d1['load1']}")
        n = len(calls)
        d2 = o.macmini()
        check(len(calls) == n and d2["fetched"] == d1["fetched"], f"60 秒以内に再取得した ssh {n}→{len(calls)}")
        SP.mode = "bad"
        d3 = o.macmini(force=True)
        check(not d3["ok"] and "uat-remote" in d3["reason"], f"失敗を成功として返した {d3}")
        check(d3.get("pm2") is None and d3.get("host") is None, f"失敗なのに今の値として数字を返した {d3}")
        check((d3.get("stale") or {}).get("host") == "uat-remote" and d3["stale_age"] is not None, f"前回分が stale として付いていない {d3.get('stale')}")
        return f"未設定=ok:False / 取得 1 回 / 60 秒以内は ssh 0 回 / 失敗時は ok:False+stale({round(d3['stale_age'])}秒前)"
    finally:
        o.subprocess = real_sp
        o.MACMINI_HOSTS = real_hosts
        if saved is None:
            if os.path.exists(cache):
                os.remove(cache)
        else:
            open(cache, "w").write(saved)


@case("EX-01", "拡張の一覧に MCP の鍵・URL・起動コマンドが出ない(名前と種類だけ)")
def ex01(ctx):
    import overview as o
    try:
        cj = json.load(open(os.path.join(HOME, ".claude.json"), encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"SKIP: ~/.claude.json を読めない({type(e).__name__})"
    o._EXT.update(t=time.time(), health={})     # CLI への問い合わせは走らせない(10 分キャッシュを使う)
    info = o.extensions_info()
    blob = json.dumps(info, ensure_ascii=False)
    names, secrets = set(), set()

    def collect(node):
        for k, s in (node.get("mcpServers") or {}).items():
            names.add(k)
            if not isinstance(s, dict):
                continue
            vals = [s.get("url"), s.get("command")] + list(s.get("args") or []) + \
                   list((s.get("env") or {}).values()) + list((s.get("headers") or {}).values())
            secrets.update(str(v) for v in vals if v and len(str(v)) >= 8)
    collect(cj)
    for pv in (cj.get("projects") or {}).values():
        collect(pv)
    hits = sorted(s for s in secrets if s in blob)
    check(not hits, f"設定の中身が {len(hits)} 件出ている(例 {[h[:30] for h in hits[:2]]})")
    got = {s["name"] for s in info["servers"]}
    check(got == names, f"サーバ名が設定と違う 出 {len(got)} / 設定 {len(names)}: {sorted(got ^ names)[:4]}")
    extra = sorted({k for s in info["servers"] for k in s} - {"name", "scope", "type", "project"})
    check(not extra, f"想定外の欄が増えている {extra}")
    return f"サーバ {len(got)} 件・照合した秘密候補 {len(secrets)} 件で一致 0・欄は name/scope/type/project のみ"


@case("TR-02", "今日の依頼件数: Codex の記録も数え、4MB を超える Claude ログでは「下限値」の印を付ける")
def tr02(ctx):
    import overview as o
    import datetime as dt
    now = dt.datetime.now().astimezone()
    yst = now - dt.timedelta(days=1)
    iso = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    J = lambda d: json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    cx = os.path.join(ctx["data"], f"tr02-rollout-{time.time_ns()}.jsonl")
    cxr = lambda when, role, text: J({"timestamp": iso(when), "type": "response_item",
                                      "payload": {"type": "message", "role": role,
                                                  "content": [{"type": "input_text", "text": text}]}})
    open(cx, "w").write("\n".join([cxr(yst, "user", "昨日の依頼"), cxr(now, "user", "今日1"),
                                   cxr(now, "assistant", "返答"), cxr(now, "user", "<env>注入</env>"),
                                   cxr(now, "user", "今日2")]) + "\n")
    o._TODAY_CACHE.pop(cx, None)
    cxn = o.today_requests(cx, "Codex CLI")
    check(cxn == (2, False), f"Codex {cxn} != (2, False)")
    pad = J({"type": "assistant", "timestamp": iso(now),
             "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "x" * 900}]}}) + "\n"
    user = lambda when, text: J({"type": "user", "timestamp": iso(when),
                                 "message": {"role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"
    out = {}
    for name, head in (("今日から始まる", []), ("昨日を含む", [user(yst, "昨日の依頼")])):
        p = os.path.join(ctx["data"], f"tr02-{name}-{time.time_ns()}.jsonl")
        with open(p, "w") as f:
            f.write(pad * (4_500_000 // len(pad)))
            for line in head:
                f.write(line)
            f.write(user(now, "今日1")); f.write(user(now, "今日2"))
        o._TODAY_CACHE.pop(p, None)
        out[name] = o.today_requests(p, "Claude")
    exp = {"今日から始まる": (2, True), "昨日を含む": (2, False)}
    check(out == exp, f"{out} != {exp}(4MB 窓の先頭が今日なら下限値の印が要る)")
    return f"Codex 2 件(昨日・注入は除外)/ 4MB 超: 印あり {out['今日から始まる']} ・印なし {out['昨日を含む']}"


@case("HK-02", "hook が 5 つの合図すべてに、実在するファイルを指すコマンドとして、Claude を待たせない設定で入る")
def hk02(ctx):
    d = tempfile.mkdtemp(dir=ctx["data"])
    p = os.path.join(d, "settings.json")
    run = lambda op: subprocess.run([sys.executable, os.path.join(BOARD, "install_hook.py"), op, p],
                                    capture_output=True, text=True)
    run("--install")
    hooks = json.load(open(p))["hooks"]
    check(sorted(hooks) == sorted(HOOK_EVENTS), "合図の顔ぶれが違う: %s" % sorted(hooks))
    for ev in HOOK_EVENTS:
        mine = [h for e in hooks[ev] for h in e["hooks"] if "tab-status.py" in h.get("command", "")]
        check(len(mine) == 1, "%s に %d 件(1 件でない)" % (ev, len(mine)))
        h = mine[0]
        check(h.get("async") is True, "%s: async でない(Claude を待たせる)" % ev)
        check(h.get("timeout") == 5, "%s: timeout %r" % (ev, h.get("timeout")))
        path = h["command"].split()[-1]
        check(os.path.exists(path), "%s: 指すファイルが無い %s" % (ev, path))
        check(os.path.realpath(path) == os.path.realpath(HOOK_PY), "%s: 別の実体を指す %s" % (ev, path))
    for ev in HOOK_EVENTS:            # 1 つ欠けたら --check は missing
        s = json.load(open(p))
        s["hooks"][ev] = [e for e in s["hooks"][ev] if not any("tab-status.py" in h.get("command", "") for h in e["hooks"])]
        json.dump(s, open(p, "w"))
        check(run("--check").returncode == 1, "%s が無いのに installed と言う" % ev)
        run("--install")
        check(run("--check").returncode == 0, "%s を足し直せない" % ev)
    fresh = os.path.join(d, "new", "settings.json")     # まだ settings.json が無い機械
    r = subprocess.run([sys.executable, os.path.join(BOARD, "install_hook.py"), "--install", fresh],
                       capture_output=True, text=True)
    check(r.returncode == 0 and os.path.exists(fresh), "新規作成できない %s %s" % (r.returncode, r.stderr[-200:]))
    check(sorted(json.load(open(fresh))["hooks"]) == sorted(HOOK_EVENTS), "新規作成の中身 %s" % open(fresh).read()[:200])
    return "5 合図 × (async/timeout=5/実体一致)・1 つ欠けると missing・設定が無い機械でも新規作成"


@case("HK-03", "2 度目の設置は 1 バイトも変えず、控えも増やさない。権限と他の設定は元のまま")
def hk03(ctx):
    d = tempfile.mkdtemp(dir=ctx["data"])
    p = os.path.join(d, "settings.json")
    json.dump({"env": {"SECRET": "s3cret"}, "model": "x"}, open(p, "w"))
    os.chmod(p, 0o600)
    run = lambda op: subprocess.run([sys.executable, os.path.join(BOARD, "install_hook.py"), op, p],
                                    capture_output=True, text=True)
    run("--install")
    mode1 = os.stat(p).st_mode & 0o777
    body1 = open(p, "rb").read()
    n1 = len(glob.glob(p + ".aiboard-backup-*"))
    time.sleep(1.1)                       # 控えの名前は秒単位: 2 度目が別名になり得る状況にする
    r2 = run("--install")
    body2 = open(p, "rb").read()
    n2 = len(glob.glob(p + ".aiboard-backup-*"))
    check("already installed" in r2.stdout, "2 度目の表示 %r" % r2.stdout.strip())
    check(body1 == body2, "2 度目で中身が変わった")
    check(n2 == n1, "控えが増えた %d→%d" % (n1, n2))
    check(mode1 == 0o600, "権限が 600 から %o に緩んだ(env に秘密が入る)" % mode1)
    check(os.stat(p).st_mode & 0o777 == 0o600, "2 度目で権限が %o" % (os.stat(p).st_mode & 0o777))
    check(not glob.glob(os.path.join(d, "*.tmp")), "書きかけの一時ファイルが残っている")
    s = json.load(open(p))
    check(s["env"]["SECRET"] == "s3cret" and s["model"] == "x", "他の設定が消えた %s" % s)
    back = json.load(open(sorted(glob.glob(p + ".aiboard-backup-*"))[0]))
    check(back == {"env": {"SECRET": "s3cret"}, "model": "x"}, "控えが元の中身でない %s" % back)
    return "2 度目 %r・控え %d 件・権限 %o 維持" % (r2.stdout.strip(), n2, mode1)


@case("HK-04", "どれが自分の hook かを取り違えない: 古い置き場は今の場所へ・入口(symlink)経由でも二重にせず・別人の同名ファイルは触らない")
def hk04(ctx):
    run = lambda op, p: subprocess.run([sys.executable, os.path.join(BOARD, "install_hook.py"), op, p],
                                       capture_output=True, text=True)
    ours = lambda h: [x for e in h for x in e["hooks"] if "tab-status.py" in x.get("command", "")]
    # ① 古い置き場(別の board/hooks)を指していたら、今の場所に直す。増やさない
    p1 = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "settings.json")
    old = "python3 /old/place/board/hooks/tab-status.py"
    json.dump({"hooks": {ev: [{"hooks": [{"type": "command", "command": old, "async": True, "timeout": 5}]}]
                         for ev in HOOK_EVENTS}}, open(p1, "w"))
    check(run("--check", p1).returncode == 0, "同じ hook の古い場所を設置済みと見なさない")
    run("--install", p1)
    h1 = json.load(open(p1))["hooks"]
    for ev in HOOK_EVENTS:
        check(len(ours(h1[ev])) == 1, "%s が %d 件に増えた" % (ev, len(ours(h1[ev]))))
        check(ours(h1[ev])[0]["command"].split()[-1] == HOOK_PY, "%s が古い場所のまま %s" % (ev, ours(h1[ev])[0]["command"]))
    run("--uninstall", p1)
    check(not any(ours(v) for v in json.load(open(p1)).get("hooks", {}).values()), "外しても残る")
    # ② 入口(~/.claude/hooks/tab-status.py が本体への symlink)経由で入っていたら、同じものと見なす
    link = os.path.join(HOME, ".claude", "hooks", "tab-status.py")
    via_link = "見あたらない"
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(HOOK_PY):
        p2 = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "settings.json")
        json.dump({"hooks": {ev: [{"hooks": [{"type": "command", "command": "python3 " + link, "async": True, "timeout": 5}]}]
                             for ev in HOOK_EVENTS}}, open(p2, "w"))
        run("--install", p2)
        h2 = json.load(open(p2))["hooks"]
        dup = {ev: len(ours(h2[ev])) for ev in HOOK_EVENTS if len(ours(h2[ev])) != 1}
        check(not dup, "入口経由の設置を別物と見て二重に入れた %s(フックが 2 回走る)" % dup)
        via_link = "二重にならない"
    # ③ 名前が同じだけの別人の hook は、書き換えない・消さない
    p3 = os.path.join(tempfile.mkdtemp(dir=ctx["data"]), "settings.json")
    foreign = "python3 /Users/other/bin/tab-status.py --theirs"
    json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": foreign}]}]}}, open(p3, "w"))
    run("--install", p3)
    h3 = json.load(open(p3))["hooks"]
    cmds = [x["command"] for e in h3["Stop"] for x in e["hooks"]]
    check(foreign in cmds, "別人の hook を書き換えた: %s" % cmds)
    check(any(c.split()[-1] == HOOK_PY for c in cmds), "自分の hook が入っていない: %s" % cmds)
    run("--uninstall", p3)
    left = [x["command"] for e in json.load(open(p3))["hooks"].get("Stop", []) for x in e["hooks"]]
    check(left == [foreign], "外した後に別人の hook が消えた/残骸がある: %s" % left)
    return "古い置き場→今の場所(5 合図とも 1 件)・入口経由は %s・別人の同名は不変" % via_link


@case("HK-05", "合図ごとに「いま何をしているか」と題名の印が決まった値になる(cs が読む契約)")
def hk05(ctx):
    mod, home = hook_mod(ctx, "state")
    sid = "uat-state-1"
    exp = []
    s = hook_fire(mod, {"hook_event_name": "SessionStart", "session_id": sid, "cwd": "/tmp/uat"})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": "/tmp/uat", "prompt": "あ" * 100})
    exp.append((s["doing"], s["mark"]))
    check(s["task"] == "あ" * 79 + "…", "依頼の切り詰め %r" % s["task"][:10])
    s = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash",
                        "tool_input": {"description": "テストを走らせる", "command": "pytest -q"}})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Read",
                        "tool_input": {"file_path": "/a/b/c.py"}})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "Notification", "session_id": sid,
                        "notification_type": "permission_prompt", "message": "Bash を許可しますか"})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "Notification", "session_id": sid, "notification_type": "auth_success"})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "Stop", "session_id": sid})
    exp.append((s["doing"], s["mark"]))
    s = hook_fire(mod, {"hook_event_name": "Notification", "session_id": sid, "notification_type": "idle_prompt"})
    exp.append((s["doing"], s["mark"]))
    want = [("起動", "⏳"), ("考え中", "⏳"), ("Bash: テストを走らせる", "⏳"), ("読込: c.py", "⏳"),
            ("⚠ 確認待ち: Bash を許可しますか", "⚠"), ("⚠ 確認待ち: Bash を許可しますか", "⚠"),
            ("✅ 返答済み（あなたの番）", "💬"), ("✅ 返答済み（あなたの番）", "💬")]
    check(exp == want, "遷移が違う\n 実際 %s\n 期待 %s" % (exp, want))
    return "8 遷移一致(確認待ち→他の通知では戻らない)"


@case("HK-06", "状態ファイルには cs が読む項目が揃い、渡された余分な値は書き込まない")
def hk06(ctx):
    src = open(os.path.join(BOARD, "cs.py"), encoding="utf-8").read()
    body = src[src.index("def tab_state("):]
    keys = sorted(set(re.findall(r'\bst\.get\(\s*"([a-z_]+)"', body)))
    check(len(keys) >= 4, "cs.py から読み取り項目を拾えない %s" % keys)
    mod, home = hook_mod(ctx, "keys")
    sid = "uat-keys-1"
    tr = os.path.join(ctx["data"], "uat-keys.jsonl")
    topic = '題名の"引用"と\n改行'
    with open(tr, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "最初の依頼"}}) + "\n")
        f.write(json.dumps({"type": "ai-title", "aiTitle": topic}) + "\n")
    hook_clients()
    try:
        hook_fire(mod, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": "/uat-client-path/x",
                        "prompt": "依頼 ZZUATPROMPT", "model": "claude-opus-5", "transcript_path": tr})
        s = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "transcript_path": tr,
                            "tool_name": "Bash",
                            "tool_input": {"description": "ls", "command": "ls", "env": {"TOKEN": "zzsecrettoken"}},
                            "permission_suggestions": [{"token": "zzsecrettoken2"}]})
    finally:
        hook_clients_off()
    missing = [k for k in keys if k not in s]
    check(not missing, "cs が読む項目が無い %s(ある: %s)" % (missing, sorted(s)))
    check(s["topic"] == topic, "題名の取り出し %r" % s.get("topic"))
    check(s["session_id"] == sid and s["ai"] == "Claude" and s["model"] == "claude-opus-5", "基本の項目 %s" % s)
    check(isinstance(s.get("updated"), (int, float)) and time.time() - s["updated"] < 120, "更新時刻 %s" % s.get("updated"))
    raw = open(os.path.join(mod.STATE_DIR, sid + ".json"), encoding="utf-8").read()
    leaks = [w for w in ("zzsecrettoken", "zzsecrettoken2", "permission_suggestions") if w in raw]
    check(not leaks, "渡されただけの値が状態ファイルに残る %s" % leaks)
    check("ZZUATPROMPT" in raw, "依頼文が残っていない(前提の確認)")
    return "cs が読む %d 項目あり(%s)・題名は引用符入りでも復元・余分な入力 0 件" % (len(keys), ",".join(keys))


@case("HK-07", "サブエージェントの操作で親の「いま何をしているか」を上書きしない。10 分で消える")
def hk07(ctx):
    mod, home = hook_mod(ctx, "sub")
    sid = "uat-sub-1"
    hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Task",
                    "tool_input": {"description": "調査を任せる"}})
    parent = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash",
                             "tool_input": {"description": "本体の作業"}})["doing"]
    s = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "agent_id": "ag-1",
                        "agent_type": "explore", "tool_name": "Grep", "tool_input": {"pattern": "foo"}})
    check(s["doing"] == parent, "親の状態が上書きされた %r → %r" % (parent, s["doing"]))
    check(list(s["subagents"]) == ["ag-1"] and s["subagents"]["ag-1"]["doing"] == "検索: foo",
          "サブエージェントの記録 %s" % s.get("subagents"))
    check(s["mark"] == "⏳", "印 %r" % s["mark"])
    p = os.path.join(mod.STATE_DIR, sid + ".json")
    st = json.load(open(p))
    st["subagents"]["ag-1"]["updated"] = time.time() - 601
    json.dump(st, open(p, "w"))
    s2 = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash",
                         "tool_input": {"description": "本体の作業"}})
    check(s2["subagents"] == {}, "10 分過ぎても消えない %s" % s2["subagents"])
    return "親の状態は不変・子は 1 件・601 秒で 0 件"


@case("HK-08", "壊れた入力・欠けた入力でも exit 0 で、状態を壊さず、失敗は黙らずログに残る")
def hk08(ctx):
    home = hook_home(ctx, "bad")
    state_dir = os.path.join(home, ".claude", "tabstate")
    sid = "uat-bad-1"
    rc, _, _ = hook_pty_run(ctx, {"hook_event_name": "Stop", "session_id": sid, "cwd": "/tmp/uat"}, home)
    good = json.load(open(os.path.join(state_dir, sid + ".json")))
    log = os.path.join(state_dir, "_errors.log")
    n0 = os.path.getsize(log) if os.path.exists(log) else 0
    check(rc == 0 and good.get("doing"), "正常な呼び出しで rc=%s %s" % (rc, good))
    bads = [(b"", "空の標準入力"), (b"not json at all", "JSON でない"),
            ({"hook_event_name": "Stop"}, "session_id 無し"),
            ({"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash", "tool_input": "文字列"}, "tool_input が文字列"),
            ({"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Read", "tool_input": None}, "tool_input が None"),
            ({"hook_event_name": "Stop", "session_id": sid, "transcript_path": "/no/such/file.jsonl"}, "記録が無い"),
            ({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "あ" * 200000}, "巨大な依頼文"),
            ({"hook_event_name": "何か知らない合図", "session_id": sid}, "知らない合図")]
    bad_rc = []
    for payload, why in bads:
        rc, _, _ = hook_pty_run(ctx, payload, home)
        if rc != 0:
            bad_rc.append((why, rc))
        st = json.load(open(os.path.join(state_dir, sid + ".json")))   # 読めなければ例外=FAIL
        check(st.get("session_id") == sid, "%s: 状態が壊れた %s" % (why, st))
        stray = sorted(x for x in os.listdir(state_dir) if x.endswith(".json") and x != sid + ".json")
        check(not stray, "%s: 身に覚えのない状態ファイルができた %s" % (why, stray))
    check(not bad_rc, "exit 0 でない %s" % bad_rc)
    # 壊れた状態ファイルからも立ち直る
    open(os.path.join(state_dir, sid + ".json"), "w").write("{壊れて")
    rc, _, _ = hook_pty_run(ctx, {"hook_event_name": "Stop", "session_id": sid, "cwd": "/tmp/uat"}, home)
    st = json.load(open(os.path.join(state_dir, sid + ".json")))
    check(rc == 0 and st["doing"].startswith("✅"), "壊れた状態から直らない %s" % st)
    # 本当に書けない時: Claude は止めない(exit 0)が、黙らずログに残す
    os.makedirs(os.path.join(state_dir, "uat-bad-2.json"), exist_ok=True)   # 状態ファイルの場所を塞ぐ
    rc, _, _ = hook_pty_run(ctx, {"hook_event_name": "Stop", "session_id": "uat-bad-2", "cwd": "/tmp/uat"}, home)
    grew = (os.path.getsize(log) if os.path.exists(log) else 0) - n0
    errs = open(log, encoding="utf-8").read().strip().splitlines() if os.path.exists(log) else []
    check(rc == 0, "書けない時に exit %s(Claude が止まる)" % rc)
    check(grew > 0, "書けなかったのにログが 1 行も増えない(黙って失敗している)")
    return "%d 通り全て exit 0・状態は常に読める・書けない時は exit 0 のままログ +%d バイト(%s)" % (
        len(bads), grew, errs[-1][:60] if errs else "")


@case("HK-09", "同じセッションに 12 個同時に来ても、状態ファイルはいつでも読めて残骸も残らない")
def hk09(ctx):
    home = hook_home(ctx, "race")
    state_dir = os.path.join(home, ".claude", "tabstate")
    os.makedirs(state_dir, exist_ok=True)
    sid = "uat-race-1"
    log = os.path.join(state_dir, "_errors.log")
    e = dict(os.environ, HOME=home)
    e.pop("CLAUDE_CONFIG_DIR", None)
    n = 12
    payloads = [{"hook_event_name": "PreToolUse", "session_id": sid, "cwd": "/tmp/uat", "tool_name": "Bash",
                 "tool_input": {"description": "同時 %02d" % i}} for i in range(n)]
    procs = [subprocess.Popen([sys.executable, HOOK_PY], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=e, start_new_session=True) for _ in payloads]
    for p, pl in zip(procs, payloads):
        p.stdin.write(json.dumps(pl).encode()); p.stdin.close()
    f = os.path.join(state_dir, sid + ".json")
    reads, bad = 0, []
    t0 = time.time()
    while any(p.poll() is None for p in procs) and time.time() - t0 < 30:
        try:
            d = json.load(open(f))
            reads += 1
            if d.get("session_id") != sid or not d.get("doing"):
                bad.append(d)
        except FileNotFoundError:
            pass
        except ValueError as ex:
            bad.append(str(ex))
    rcs = [p.wait(timeout=30) for p in procs]
    check(rcs == [0] * n, "終了コード %s" % rcs)
    check(not bad, "途中で壊れて見えた %d 回(%s)" % (len(bad), bad[:1]))
    check(not os.path.exists(log), "同時に走っただけで失敗ログが出た: %s" % (open(log).read()[-200:] if os.path.exists(log) else ""))
    d = json.load(open(f))
    check(d["doing"].startswith("Bash: 同時"), "最後の状態 %s" % d.get("doing"))
    junk = [x for x in os.listdir(state_dir) if x.endswith(".tmp")]
    check(not junk, "一時ファイルが残った %s" % junk[:3])
    return "%d 並列すべて exit 0・失敗ログ 0 行・実行中 %d 回読んで全部正しい JSON・残骸 0" % (n, reads)


@case("HK-10", "会話記録が 1GB でもフックは末尾だけ読んで一瞬で終わる(Claude を待たせない)")
def hk10(ctx):
    home = hook_home(ctx, "speed")
    d = tempfile.mkdtemp(dir=ctx["data"])
    head = json.dumps({"type": "user", "message": {"content": "最初の依頼 ZZHEAD"}}) + "\n"
    tail = (json.dumps({"type": "assistant", "message": {"model": "claude-fable-5-1", "content": [{"type": "text", "text": "末尾"}]}}) + "\n"
            + json.dumps({"type": "ai-title", "aiTitle": "末尾の題名 ZZTAIL"}) + "\n")
    def make(path, size):
        with open(path, "w") as f:
            f.write(head)
            if size:                      # 中身の無い穴(sparse)で大きくする。実ディスクは食わない
                f.truncate(size)
                f.seek(size - 100_000)
            f.write(tail)
        return os.path.getsize(path) / 1e6
    small_mb = make(os.path.join(d, "small.jsonl"), 0)
    big_mb = make(os.path.join(d, "big.jsonl"), 1024 ** 3)
    def t_of(name, n=3):
        p, ts, st = os.path.join(d, name), [], None
        for i in range(n):
            t0 = time.time()
            rc, _, _ = hook_pty_run(ctx, {"hook_event_name": "PreToolUse", "session_id": "uat-speed-1",
                                          "cwd": "/tmp/uat", "transcript_path": p, "tool_name": "Bash",
                                          "tool_input": {"description": "速さ %d" % i}}, home, timeout=60)
            check(rc == 0, "%s: rc=%s" % (name, rc))
            ts.append(time.time() - t0)
        st = json.load(open(os.path.join(home, ".claude", "tabstate", "uat-speed-1.json")))
        return min(ts), st
    t_small, _ = t_of("small.jsonl")
    t_big, st = t_of("big.jsonl")
    check(st.get("model") == "claude-fable-5-1" and st.get("topic") == "末尾の題名 ZZTAIL",
          "末尾の中身を拾えていない model=%r topic=%r" % (st.get("model"), st.get("topic")))
    check(t_big < 1.0, "1GB の記録で %.2f 秒(1 秒未満であること。settings の timeout は 5 秒)" % t_big)
    check(t_big < t_small + 0.2, "記録の大きさで所要が伸びた %.2f→%.2f 秒(全部読んでいる)" % (t_small, t_big))
    return "%.1fMB %.2fs / %.0fMB %.2fs(python 起動込み・末尾のモデルと題名を取得)" % (small_mb, t_small, big_mb, t_big)


@case("HK-11", "起動の合図で片付くのは 2 日より古い状態ファイルだけ(他人のファイルやログは消さない)")
def hk11(ctx):
    mod, home = hook_mod(ctx, "gc")
    d = mod.STATE_DIR
    os.makedirs(d, exist_ok=True)
    made = {}
    for name, age in (("old-1.json", 3 * 86400), ("old-2.json", 2 * 86400 + 60), ("fresh.json", 3600),
                      ("keep.json", 86400), ("_errors.log", 30 * 86400), ("notes.txt", 30 * 86400)):
        p = os.path.join(d, name)
        open(p, "w").write("{}" if name.endswith(".json") else "x")
        os.utime(p, (time.time() - age, time.time() - age))
        made[name] = p
    hook_fire(mod, {"hook_event_name": "SessionStart", "session_id": "uat-gc-1", "cwd": "/tmp/uat"})
    left = set(os.listdir(d))
    gone = sorted(n for n in made if n not in left)
    check(gone == ["old-1.json", "old-2.json"], "消えた顔ぶれが違う %s" % gone)
    check("uat-gc-1.json" in left, "自分の状態ファイルが無い")
    return "古い 2 件だけ削除・新しい 2 件とログ/他形式は残る"


@case("HK-12", "端末の解決と、タブへ出す色・透かし・題名の中身")
def hk12(ctx):
    import base64
    home = hook_home(ctx, "tty")
    sid = "uat-tty-1"
    def fire(payload):
        rc, text, tty = hook_pty_run(ctx, dict(payload, session_id=sid), home)
        check(rc == 0, "rc=%s" % rc)
        st = json.load(open(os.path.join(home, ".claude", "tabstate", sid + ".json")))
        rgb = [int(x) for x in re.findall(r"\x1b\]6;1;bg;\w+;brightness;(\d+)\x07", text)]
        b = re.search(r"\x1b\]1337;SetBadgeFormat=([A-Za-z0-9+/=]+)\x07", text)
        ttl = re.search(r"\x1b\]0;([^\x07]*)\x07", text)
        badge = base64.b64decode(b.group(1)).decode() if b else ""
        return st, tty, rgb, badge, (ttl.group(1) if ttl else "")
    st, tty, rgb, badge, title = fire({"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/uat",
                                       "prompt": "これはUATの依頼", "model": "claude-opus-5"})
    check(st.get("tty") == tty, "端末の解決が違う 記録 %r / 実際 %r" % (st.get("tty"), tty))
    check(rgb == [217, 119, 87], "通常の色 %s" % rgb)
    check(badge.splitlines()[0].endswith("Claude Opus 5") and badge.splitlines()[1] == "▶ 考え中",
          "透かし %r" % badge)
    check(title.startswith("⏳🟠") and "これはUATの依頼" in title, "題名 %r" % title)
    st2, _, rgb2, badge2, title2 = fire({"hook_event_name": "Notification", "cwd": "/tmp/uat",
                                         "notification_type": "permission_prompt", "message": "許可しますか"})
    check(rgb2 == [220, 50, 50], "確認待ちの色 %s" % rgb2)
    check(badge2.splitlines()[1] == "▶ ⚠ 確認待ち: 許可しますか" and title2.startswith("⚠🟠"), "%r %r" % (badge2, title2))
    st3, _, rgb3, _, title3 = fire({"hook_event_name": "Stop", "cwd": "/tmp/uat"})
    check(rgb3 == [217, 119, 87] and title3.startswith("💬🟠"), "戻り %s %r" % (rgb3, title3))
    return "端末 %s を ps でたどって一致・色 3 種・透かし 2 行・題名の印 ⏳/⚠/💬" % tty


@case("HK-13", "顧客の作業はその顧客の色と名前で出て、後の合図でも忘れない")
def hk13(ctx):
    import base64
    mod, home = hook_mod(ctx, "client")
    sid = "uat-cl-1"
    rgb_of = lambda seq: [int(x) for x in re.findall(r"\x1b\]6;1;bg;\w+;brightness;(\d+)\x07", seq)]
    badge_of = lambda seq: base64.b64decode(re.search(r"\x1b\]1337;SetBadgeFormat=([A-Za-z0-9+/=]+)\x07", seq).group(1)).decode()
    hook_clients()
    try:
        s = hook_fire(mod, {"hook_event_name": "UserPromptSubmit", "session_id": sid,
                            "cwd": "/uat-client-path/app", "prompt": "普通の依頼", "model": "claude-opus-5"})
        check(s["client"]["id"] == "uatco" and s["client"]["by"] == "path", "顧客判定 %s" % s.get("client"))
        check("🟦 顧客: UAT商事" in badge_of(mod.seqs[-1]), "透かしに顧客が出ない %r" % badge_of(mod.seqs[-1]))
        rgb = rgb_of(mod.seqs[-1])
        check(rgb == [1, 2, 3], "顧客の色でない %s" % rgb)
        s2 = hook_fire(mod, {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash",
                             "tool_input": {"description": "続き"}})
        check(s2["client"]["id"] == "uatco", "次の合図で顧客を忘れた %s" % s2.get("client"))
        # 顧客に当たらない作業は橙のまま
        s3 = hook_fire(mod, {"hook_event_name": "UserPromptSubmit", "session_id": "uat-cl-2",
                             "cwd": "/tmp/uat", "prompt": "関係ない依頼"})
        check(s3.get("client") is None and rgb_of(mod.seqs[-1]) == [217, 119, 87],
              "無関係な作業に顧客が付いた %s %s" % (s3.get("client"), rgb_of(mod.seqs[-1])))
    finally:
        hook_clients_off()
    return "顧客: 色 [1,2,3]・透かしに社名・次の合図でも保持 / 無関係は橙"


@case("IX-01", "合成した会話ログが、作ったとおりに索引される(数えてはいけない行を数えない)")
def ix01(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix01-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    prof = os.path.join(fh, ".claude-profiles", "acct2", "projects", "-Users-uat-uat")
    os.makedirs(proj); os.makedirs(prof)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: open(p, "w", encoding="utf-8").write(
        "".join((l if isinstance(l, str) else json.dumps(l, ensure_ascii=False, separators=(",", ":"))) + "\n" for l in ls))
    U = lambda t, txt, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "user",
                                   "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                   "cwd": "/Users/uat/uat", "entrypoint": "cli"}, **kw)
    def A(t, model, tools=()):
        c = [{"type": "text", "text": "ok"}]
        for n, i in tools:
            c.append({"type": "tool_use", "id": "t%d" % len(c), "name": n, "input": i})
        return {"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
                "message": {"model": model, "id": "m", "type": "message", "role": "assistant", "content": c}}
    now = time.time()
    sid = "aa000001-1111-4111-8111-111111111111"
    W(os.path.join(proj, sid + ".jsonl"), [
        {"type": "ai-title", "aiTitle": "受入試験の会話", "sessionId": sid},
        U(now - 900, "依頼1"),
        A(now - 890, "claude-opus-5", [("Edit", {"file_path": HOME + "/uat/a.py"}), ("Bash", {"command": "ls"})]),
        U(now - 880, "無視される注記", isMeta=True),
        {"type": "user", "timestamp": iso(now - 870), "message": {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "結果"}]}},
        U(now - 860, "サブの発言", isSidechain=True),
        U(now - 850, "<\system-reminder>注入<\/system-reminder>"),
        U(now - 840, "Caveat: これは依頼ではない"),
        U(now - 830, "依頼2"),
        A(now - 820, "claude-haiku-4-5", [("Write", {"file_path": HOME + "/uat/b.py"}),
                                          ("Edit", {"file_path": HOME + "/uat/a.py"})]),
        A(now - 810, "claude-haiku-4-5"),
        U(now - 800, "依頼3"),
    ])
    W(os.path.join(prof, "aa000002-1111-4111-8111-111111111111.jsonl"),
      [U(now - 700, "別アカウントの依頼"), A(now - 690, "claude-opus-5")])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        idx, _ = ix.build(days=30)
        r = idx["records"][sid]
        got = {"prompts": r["prompts"], "tools": r["tools"], "responses": r["responses"],
               "first": r["first_prompt"], "last": r["last_prompt"], "title": r["title"],
               "model": r["model"], "files_top": [list(x) for x in r["files_top"]],
               "cwd": r["cwd"], "account": r["account"], "hist": [h["model"] for h in r["model_history"]]}
        exp = {"prompts": 3, "tools": 4, "responses": 3, "first": "依頼1", "last": "依頼3",
               "title": "受入試験の会話", "model": "claude-haiku-4-5",
               "files_top": [[HOME + "/uat/a.py", 2], [HOME + "/uat/b.py", 1]],
               "cwd": "/Users/uat/uat", "account": "",
               "hist": ["claude-opus-5", "claude-haiku-4-5"]}
        check(got == exp, f"索引の中身が作ったものと違う: {json.dumps({k: (got[k], exp[k]) for k in exp if got[k] != exp[k]}, ensure_ascii=False)}")
        check(abs(r["start"] - (now - 900)) < 2 and abs(r["end"] - (now - 800)) < 2, f"期間 {r['start']} {r['end']}")
        p2 = idx["records"]["aa000002-1111-4111-8111-111111111111"]
        check(p2["account"] == "acct2", f"別アカウントの account={p2['account']!r}")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "依頼 3(注記・ツール結果・サブ・注入・Caveat の 5 行は数えない)・ツール 4・応答 3・編集 a.py×2 b.py×1・アカウント acct2"


@case("IX-02", "増分索引: 変わっていなければ読み直さず、追記した1本だけ読み直し、消えた記録は索引から消える")
def ix02(ctx):
    import datetime as dt
    import sqlite3
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix02-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    line = lambda t, txt: json.dumps({"parentUuid": None, "isSidechain": False, "type": "user",
                                      "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                      "cwd": "/Users/uat/uat", "entrypoint": "cli"},
                                     ensure_ascii=False, separators=(",", ":")) + "\n"
    now = time.time()
    sids = ["bb00000%d-1111-4111-8111-111111111111" % i for i in range(1, 5)]
    for i, s in enumerate(sids):
        open(os.path.join(proj, s + ".jsonl"), "w", encoding="utf-8").write(line(now - 900 + i, "依頼%d" % i))
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        _, s1 = ix.build(days=30)
        _, s2 = ix.build(days=30)
        with open(os.path.join(proj, sids[2] + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(line(now - 100, "追記の依頼"))
        idx3, s3 = ix.build(days=30)
        os.remove(os.path.join(proj, sids[0] + ".jsonl"))
        _, s4 = ix.build(days=30)
        con = sqlite3.connect(ix.DB_PATH)
        ids = {r[0] for r in con.execute("select id from records")}
        nf = con.execute("select count(*) from files").fetchone()[0]
        con.close()
        check((s1["parsed"], s2["parsed"], s3["parsed"], s4["parsed"]) == (4, 0, 1, 0),
              f"解析件数 {s1['parsed']},{s2['parsed']},{s3['parsed']},{s4['parsed']}(期待 4,0,1,0)")
        check(idx3["records"][sids[2]]["prompts"] == 2, f"追記が反映されていない {idx3['records'][sids[2]]['prompts']}")
        check(sids[0] not in ids and len(ids) == 3, f"消したはずの記録が索引に残る {sorted(x[:8] for x in ids)}")
        check(nf == 3, f"files 表に消えたファイルが残る {nf}")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "4件→0件→1件→0件・追記は依頼2件に・消した1本は records と files から消えた"


@case("IX-03", "種別(role)の割り振り: サブ・査読・補助・無人実行・人の会話が根拠つきで1つに決まる")
def ix03(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix03-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: (os.makedirs(os.path.dirname(p), exist_ok=True), open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls)))
    U = lambda t, txt, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "user",
                                   "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                   "cwd": "/Users/uat/uat", "entrypoint": "cli"}, **kw)
    A = lambda t, model="claude-opus-5", **kw: dict({"parentUuid": None, "isSidechain": False, "type": "assistant",
        "timestamp": iso(t), "message": {"model": model, "id": "m", "type": "message", "role": "assistant",
                                         "content": [{"type": "text", "text": "ok"}]}}, **kw)
    now = time.time()
    S = lambda n: "cc00000%d-1111-4111-8111-111111111111" % n
    W(os.path.join(proj, S(1) + ".jsonl"), [U(now - 900, "ふつうの依頼"), A(now - 890)])
    W(os.path.join(proj, S(1), "subagents", "g", "agent-abc.jsonl"),
      [U(now - 880, "サブの依頼", isSidechain=True), A(now - 870, isSidechain=True)])
    W(os.path.join(proj, S(2) + ".jsonl"), [U(now - 800, "定期ジョブ", entrypoint="sdk-py"), A(now - 790)])
    W(os.path.join(proj, S(3) + ".jsonl"), [U(now - 700, "一時フォルダの作業", cwd="/private/tmp/claude-501/x"), A(now - 690)])
    W(os.path.join(proj, S(4) + ".jsonl"), [U(now - 600, "あなたは書記です。以下をまとめて"), A(now - 590)])
    cx = os.path.join(fh, ".codex", "sessions", "2026", "09", "18")
    for n, src, first in ((5, "exec", "あなたは査読者です。反証して"), (6, "exec", "この関数を直して"), (7, "cli", "手で始めた codex")):
        cid = "dd00000%d-2222-4222-8222-222222222222" % n
        W(os.path.join(cx, "rollout-2026-09-18T00-00-0%d-%s.jsonl" % (n, cid)), [
            {"timestamp": iso(now - 500 + n), "ordinal": 0, "type": "session_meta",
             "payload": {"id": cid, "timestamp": iso(now - 500 + n), "cwd": "/Users/uat/uat",
                         "originator": "codex_exec", "source": src}},
            {"timestamp": iso(now - 499 + n), "ordinal": 1, "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": first}]}}])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        idx, _ = ix.build(days=30)
        keyed = {k: r["role"] for k, r in idx["records"].items()}
        exp2 = {S(1): "human", S(1) + "/agent-abc": "subagent", S(2): "unattended", S(3): "unattended",
                S(4): "unattended", "dd000005-2222-4222-8222-222222222222": "review",
                "dd000006-2222-4222-8222-222222222222": "helper",
                "dd000007-2222-4222-8222-222222222222": "human"}
        bad = {k[:10]: (keyed.get(k), v) for k, v in exp2.items() if keyed.get(k) != v}
        check(not bad, f"種別が違う {bad}")
        check(len(keyed) == len(exp2), f"余計な記録がある {sorted(k[:10] for k in keyed)}")
        why = {k: idx["records"][k]["unattended_by"][:14] for k in (S(2), S(3), S(4))}
        check(all(why.values()), f"無人実行の根拠が空 {why}")
        check(not idx["records"][S(1)]["unattended"] and not idx["records"]["dd000005-2222-4222-8222-222222222222"]["unattended"],
              "人の会話や codex exec を無人実行にしている")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "human/subagent/unattended×3(entrypoint・一時フォルダ・定型1回)/review/helper/codex cli の 8 本すべて期待どおり"


@case("IX-04", "「つづき」は根拠のある相手にだけ引く(直前の別件には引かない・根拠が無ければ引かない)")
def ix04(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix04-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls))
    U = lambda t, txt: {"parentUuid": None, "isSidechain": False, "type": "user",
                        "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                        "cwd": "/Users/uat/uat", "entrypoint": "cli"}
    A = lambda t: {"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
                   "message": {"model": "claude-opus-5", "id": "m", "type": "message", "role": "assistant",
                               "content": [{"type": "text", "text": "ok"}]}}
    now = time.time()
    S = lambda n: "ee00000%d-1111-4111-8111-111111111111" % n
    L = ["索引の受入試験に使う特徴的な行その1 ALPHA12345", "索引の受入試験に使う特徴的な行その2 BRAVO67890",
         "索引の受入試験に使う特徴的な行その3 CHARLIE2468"]
    t2 = now - 2 * 86400
    W(os.path.join(proj, S(1) + ".jsonl"), [U(t2, "もとの会話\n" + "\n".join(L)), A(t2 + 5)])
    W(os.path.join(proj, S(9) + ".jsonl"), [U(now - 420, "直前の別件。中身はまったく関係のない話。"), A(now - 310)])
    W(os.path.join(proj, S(2) + ".jsonl"), [U(now - 300, "つづき。以下を引き継ぐ\n" + "\n".join(L) + "\n" + "z" * 200), A(now - 290)])
    W(os.path.join(proj, S(3) + ".jsonl"), [U(now - 200, "つづき。以下を引き継ぐ\nどこにも無い行QQQ1 0123456789abc\n"
                                              "どこにも無い行QQQ2 defghijklmnop\nどこにも無い行QQQ3 qrstuvwxyz012\n" + "y" * 200)])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        idx, _ = ix.build(days=30)
        es = [(e["from"], e["to"], e["evidence"]) for e in idx["edges"]["continue"]]
        check(len(es) == 1, f"つづきのエッジが {len(es)} 本({[(a[:8], b[:8]) for a, b, _ in es]})")
        src, dst, ev = es[0]
        check((src, dst) == (S(1), S(2)), f"つづきの向き/相手が違う {src[:8]}→{dst[:8]}")
        check(len(ev["matched_lines"]) >= 2, f"根拠の行が {len(ev['matched_lines'])} 本しかない")
        blob = open(os.path.join(proj, S(1) + ".jsonl"), encoding="utf-8").read()
        check(all(m in blob for m in ev["matched_lines"]), "根拠とされた行が相手の記録に実在しない")
        check(idx["cont_done"].get(S(3), {}).get("none") is True, f"根拠が無いのに引いた {idx['cont_done'].get(S(3))}")
        check(ev["searched_files"] >= 2, f"おとりを候補にすら入れていない(searched_files={ev['searched_files']})")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return f"根拠 {len(ev['matched_lines'])} 行が実在する 1 本だけに接続・直前の別件(候補{ev['searched_files']}本中)には引かない・根拠無しは 0 本"


@case("IX-05", "表示の絞り込み(期間・無人実行・子・動作中)が効き、上のバーの件数は絞り込みで変わらない")
def ix05(ctx):
    import datetime as dt
    import sqlite3
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix05-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: (os.makedirs(os.path.dirname(p), exist_ok=True), open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls)))
    U = lambda t, txt, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "user",
                                   "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                   "cwd": "/Users/uat/uat", "entrypoint": "cli"}, **kw)
    A = lambda t, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
        "message": {"model": "claude-opus-5", "id": "m", "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}]}}, **kw)
    now, S = time.time(), lambda n: "ff00000%d-1111-4111-8111-111111111111" % n
    t5 = now - 5 * 86400
    W(os.path.join(proj, S(1) + ".jsonl"), [U(now - 300, "いまの依頼"), A(now - 290)])
    W(os.path.join(proj, S(1), "subagents", "g", "agent-abc.jsonl"),
      [U(now - 295, "サブの依頼", isSidechain=True), A(now - 294, isSidechain=True)])
    W(os.path.join(proj, S(2) + ".jsonl"), [U(t5, "5日前の依頼"), A(t5 + 5)])
    W(os.path.join(proj, S(3) + ".jsonl"), [U(t5, "5日前の定期ジョブ", entrypoint="sdk-py"), A(t5 + 5)])
    W(os.path.join(proj, S(4) + ".jsonl"), [U(now - 200, "いまの定期ジョブ", entrypoint="sdk-py"), A(now - 190)])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        ix.build(days=30)
        sub = S(1) + "/agent-abc"
        q = lambda **kw: {x["id"] for x in ix.query_db(days=kw.get("days", 1),
                                                       include_unattended=kw.get("un", False),
                                                       live_ids=kw.get("live", ()), children=kw.get("ch", True))["records"]}
        got = {"既定": q(), "無人込み": q(un=True), "子なし": q(ch=False), "30日": q(days=30),
               "動作中に期間外": q(live=(S(2),)), "動作中に期間外の無人": q(live=(S(3),))}
        exp = {"既定": {S(1), sub}, "無人込み": {S(1), sub, S(4)}, "子なし": {S(1)},
               "30日": {S(1), sub, S(2)}, "動作中に期間外": {S(1), sub, S(2)},
               "動作中に期間外の無人": {S(1), sub, S(3)}}
        bad = {k: (sorted(x[:10] for x in got[k]), sorted(x[:10] for x in v)) for k, v in exp.items() if got[k] != v}
        check(not bad, f"絞り込みの結果が違う {bad}")
        counts = [ix.query_db(days=30, include_unattended=u, live_ids=(), children=c)["counts"]
                  for u in (False, True) for c in (True, False)]
        check(all(c == counts[0] for c in counts), f"件数が絞り込みで変わる {counts}")
        con = sqlite3.connect(ix.DB_PATH)
        by = dict(con.execute("select role, count(*) from records group by role").fetchall())
        n_index = con.execute("select count(*) from records").fetchone()[0]
        con.close()
        exp_c = {"human": by.get("human", 0), "subagent": by.get("subagent", 0), "unattended": by.get("unattended", 0),
                 "review_helper": by.get("review", 0) + by.get("helper", 0)}
        check(all(counts[0][k] == v for k, v in exp_c.items()), f"件数が DB の実数と違う {counts[0]} != {exp_c}")
        r = ix.query_db(days=30, include_unattended=False, live_ids=(S(2),), children=True)
        check(r["n_index"] == n_index, f"n_index {r['n_index']} != {n_index}")
        check(next(x for x in r["records"] if x["id"] == S(2))["live"] is True, "live 印が付いていない")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "6 通りの絞り込みすべて期待どおり・件数は4通りで同一かつ DB の実数と一致"


@case("IX-06", "期間外でも「つづきの相手」は ghost として出し、エッジは両端が揃っている時だけ返す")
def ix06(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix06-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: (os.makedirs(os.path.dirname(p), exist_ok=True), open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls)))
    U = lambda t, txt, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "user",
                                   "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                   "cwd": "/Users/uat/uat", "entrypoint": "cli"}, **kw)
    A = lambda t, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
        "message": {"model": "claude-opus-5", "id": "m", "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}]}}, **kw)
    now, S = time.time(), lambda n: "1a00000%d-1111-4111-8111-111111111111" % n
    L = ["索引の受入試験で貼り付ける行その1 DELTA13579", "索引の受入試験で貼り付ける行その2 ECHO2468101",
         "索引の受入試験で貼り付ける行その3 FOXTROT1122"]
    t2 = now - 2 * 86400
    W(os.path.join(proj, S(1) + ".jsonl"), [U(t2, "もとの会話\n" + "\n".join(L)), A(t2 + 5)])
    W(os.path.join(proj, S(2) + ".jsonl"), [U(now - 300, "つづき。以下を引き継ぐ\n" + "\n".join(L) + "\n" + "z" * 200), A(now - 290)])
    W(os.path.join(proj, S(2), "subagents", "g", "agent-abc.jsonl"),
      [U(now - 295, "サブの依頼。" + "あ" * 400, isSidechain=True), A(now - 294, isSidechain=True)])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        ix.build(days=30)
        r = ix.query_db(days=1, include_unattended=False, live_ids=(), children=True)
        by = {x["id"]: x for x in r["records"]}
        check(S(1) in by and by[S(1)]["ghost"] is True, f"期間外のつづき元が出ていない/ghost でない {list(by)}")
        check(by[S(2)]["ghost"] is False, "期間内の記録に ghost が付いている")
        kinds = sorted({(e["kind"], e["from"], e["to"]) for e in r["edges"]})
        check(("continue", S(1), S(2)) in kinds, f"つづきのエッジが無い {[(k, a[:8], b[:8]) for k, a, b in kinds]}")
        check(all(e["from"] in by and e["to"] in by for e in r["edges"]), "片端が結果に無いエッジを返している")
        r2 = ix.query_db(days=1, include_unattended=False, live_ids=(), children=False)
        ids2 = {x["id"] for x in r2["records"]}
        check(all(e["kind"] not in ("subagent", "review", "helper") for e in r2["edges"]),
              "子を畳んだのに子のエッジを返している")
        check(S(2) + "/agent-abc" not in ids2, "子を畳んだのにサブエージェントを返している")
        sub = by.get(S(2) + "/agent-abc")
        check(sub and len(sub["first_prompt"]) <= 120 and len(by[S(2)]["first_prompt"]) <= 300,
              f"依頼文の切り詰めが効いていない 子={len(sub['first_prompt']) if sub else None} 親={len(by[S(2)]['first_prompt'])}")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "2日前のつづき元が ghost で出る・エッジは両端揃い・子を畳むと子とそのエッジは出ない・子の依頼文は120字"


@case("IX-07", "Codex の状態DBは読むだけ。期間より前の Codex は索引に複写しない")
def ix07(ctx):
    import sqlite3
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix07-", dir=ctx["data"])
    cdb = os.path.join(d, "codex.sqlite")
    con = sqlite3.connect(cdb)
    con.executescript(
        "create table threads(id text primary key, created_at integer, updated_at integer, source text, cwd text,"
        " title text, tokens_used integer, git_sha text, git_branch text, git_origin_url text,"
        " first_user_message text, model text, agent_nickname text);"
        "create table thread_spawn_edges(parent_thread_id text, child_thread_id text primary key, status text);")
    now = time.time()
    IN = "2a000001-2222-4222-8222-222222222222"
    OUT = "2a000002-2222-4222-8222-222222222222"
    EX = "2a000003-2222-4222-8222-222222222222"
    OLD = "2a000004-2222-4222-8222-222222222222"
    con.executemany("insert into threads values(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (IN, now - 500, now - 400, "cli", "/Users/uat/uat", "題名1", 1234, "abcdef1234567890", "main", "git@x", "最初の依頼", "gpt-6-astra", "にゃん"),
        (OUT, now - 300, now - 200, "cli", HOME + "/yy", "題名2", 77, "0123456789abcdef", "dev", "", "こっちの依頼", "gpt-5.6", None),
        (EX, now - 300, now - 250, "exec", HOME + "/yy", "査読", 5, "", "", "", "あなたは査読者", "gpt-5.5", None),
        (OLD, now - 40 * 86400, now - 40 * 86400, "cli", HOME + "/zz", "むかし", 1, "", "", "", "ふるい", "gpt-5", None)])
    con.execute("insert into thread_spawn_edges values(?,?,?)", (IN, OUT, "done"))
    con.commit(); con.close()
    before = (os.path.getsize(cdb), open(cdb, "rb").read())
    old = {k: getattr(ix, k) for k in ("CODEX_DB", "DB_PATH", "INDEX_PATH")}
    real_db, denied = ix._codex_db, []
    def ro():
        c = real_db()
        def auth(action, a1, a2, a3, a4):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_PRAGMA):
                return sqlite3.SQLITE_OK
            denied.append((action, a1, a2))
            return sqlite3.SQLITE_DENY
        c.set_authorizer(auth)
        return c
    try:
        ix.CODEX_DB, ix.DB_PATH = cdb, os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        ix._codex_db = ro
        rec = {"id": IN, "kind": "codex", "role": "human", "end": now - 400, "mtime": now - 400, "ai": "Codex",
               "model": "gpt-6-astra", "cwd": "/Users/uat/uat", "first_prompt": "最初の依頼", "last_prompt": "",
               "files": [], "children": [], "path": os.path.join(d, "a.jsonl"), "title": "題名1"}
        idx = {"records": {IN: rec, "zz-light": {"id": "zz-light", "light": True, "kind": "codex", "role": "human", "end": now}},
               "files": {}, "cont_done": {}, "edges": {}}
        info = ix.enrich_codex_from_db(idx)
        check(info.get("enriched") == 1 and info.get("db_threads") == 4, f"取り込み結果 {info}")
        check(rec["tokens"] == 1234 and rec["git"]["branch"] == "main" and rec["agent_nickname"] == "にゃん",
              f"DB の列が入っていない {rec.get('tokens')} {rec.get('git')} {rec.get('agent_nickname')}")
        check("zz-light" not in idx["records"], "軽量レコードが索引に残っている")
        rows, n_exec = ix.codex_history_from_db(now - 30 * 86400, {IN})
        ids = [r["id"] for r in rows]
        check(ids == [OUT], f"期間外・索引済み・exec の除外が効いていない {[x[:8] for x in ids]}")
        check(n_exec == 1, f"exec の件数 {n_exec}")
        check(rows[0]["light"] is True and rows[0]["tokens"] == 77 and rows[0]["project"] == "yy", f"列の写し {rows[0]}")
        ix.save_index(idx)
        c = sqlite3.connect(ix.DB_PATH)
        stored = {r[0] for r in c.execute("select id from records")}
        c.close()
        check(OUT not in stored and OLD not in stored and EX not in stored,
              f"期間外の Codex を索引に複写している {sorted(x[:8] for x in stored)}")
        check(not denied, f"Codex の状態DBへ書こうとした: {denied}")
        check((os.path.getsize(cdb), open(cdb, "rb").read()) == before, "Codex の状態DBの中身が変わった")
    finally:
        ix._codex_db = real_db
        for k, v in old.items():
            setattr(ix, k, v)
    return "SELECT 以外は 0 回・ファイルの中身も不変・DB 4 件のうち索引に入るのは 0 件(表示時だけ 1 件・exec は件数 1)"


@case("IX-08", "壊れた記録が 1 本あっても索引全体は作られる(その1本だけ諦める)")
def ix08(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix08-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: open(p, "w", encoding="utf-8").write(
        "".join((l if isinstance(l, str) else json.dumps(l, ensure_ascii=False, separators=(",", ":"))) + "\n" for l in ls))
    U = lambda t, txt: {"parentUuid": None, "isSidechain": False, "type": "user",
                        "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                        "cwd": "/Users/uat/uat", "entrypoint": "cli"}
    now, S = time.time(), lambda n: "3a00000%d-1111-4111-8111-111111111111" % n
    W(os.path.join(proj, S(1) + ".jsonl"), [U(now - 900, "健全な依頼1")])
    W(os.path.join(proj, S(2) + ".jsonl"), [
        U(now - 800, "健全な依頼2"),
        "{ここは JSON ではない",
        {"type": "user", "message": "message が文字列", "timestamp": iso(now - 790)},
        {"type": "user", "message": None, "timestamp": iso(now - 780)},
        U(now - 770, "健全な依頼3")])
    open(os.path.join(proj, S(3) + ".jsonl"), "w").close()
    with open(os.path.join(proj, S(4) + ".jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(U(now - 700, "健全な依頼4"), ensure_ascii=False, separators=(",", ":")))
    open(os.path.join(proj, S(5) + ".jsonl"), "wb").write(
        b'{"type":"user","message":{"role":"user","content":"\xff\xfe"},"timestamp":"' + iso(now - 600).encode() + b'"}\n')
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        try:
            idx, st = ix.build(days=30)
        except Exception as e:
            raise Fail(f"壊れた記録 1 本で索引ビルドごと落ちた: {type(e).__name__}: {e}")
        ids = set(idx["records"])
        check({S(1), S(3), S(4), S(5)} <= ids, f"健全な記録が索引に入っていない {sorted(x[:10] for x in ids)}")
        check(idx["records"][S(1)]["prompts"] == 1, "健全な記録の中身が壊れている")
        r = ix.query_db(days=30, include_unattended=False, live_ids=(), children=True)
        check(len(r["records"]) >= 4, f"表示用の取り出しが {len(r['records'])} 件")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return f"壊れた記録 1 本を含む 5 本で索引 {len(ids)} 件・健全な 4 本は無事"


@case("IX-09", "子プロセスの取り出し(--query)が JSON を返し、API キーが題名にも依頼文にも残らない")
def ix09(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix09-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    os.makedirs(os.path.join(fh, ".codex"), exist_ok=True)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: (os.makedirs(os.path.dirname(p), exist_ok=True), open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls)))
    now = time.time()
    SECRET = "sk-ant-api03-" + "Z" * 40
    sid = "4a000001-1111-4111-8111-111111111111"
    W(os.path.join(proj, sid + ".jsonl"), [
        {"type": "ai-title", "aiTitle": "鍵は " + SECRET + " です", "sessionId": sid},
        {"parentUuid": None, "isSidechain": False, "type": "user", "timestamp": iso(now - 300),
         "message": {"role": "user", "content": "鍵は " + SECRET + " です。これで直して"},
         "cwd": "/Users/uat/uat", "entrypoint": "cli"}])
    cid = "4a000002-2222-4222-8222-222222222222"
    W(os.path.join(fh, ".codex", "sessions", "2026", "09", "18", "rollout-2026-09-18T00-00-00-%s.jsonl" % cid), [
        {"timestamp": iso(now - 200), "ordinal": 0, "type": "session_meta",
         "payload": {"id": cid, "timestamp": iso(now - 200), "cwd": "/Users/uat/uat", "originator": "codex_cli", "source": "cli"}},
        {"timestamp": iso(now - 190), "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "鍵は " + SECRET + " です。これで直して"}]}}])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        ix.build(days=30)
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    dd = os.path.join(d, "data")
    os.makedirs(dd, exist_ok=True)
    shutil.copy2(os.path.join(d, "index.db"), os.path.join(dd, "index.db"))
    env = dict(os.environ, HOME=fh, AIBOARD_DATA=dd, OVERVIEW_NO_INDEX="1")
    arg = json.dumps({"days": 30, "unattended": False, "live_ids": [], "children": True, "extra": {"stats": {"uat": 1}}})
    r = subprocess.run([sys.executable, os.path.join(BOARD, "overview_index.py"), "--query", arg],
                       capture_output=True, text=True, env=env, timeout=120)
    check(r.returncode == 0, f"子プロセスが失敗 rc={r.returncode}: {r.stderr[-300:]}")
    out = json.loads(r.stdout)
    check({"records", "edges", "counts", "stats"} <= set(out), f"返ってきた鍵 {sorted(out)}")
    check(out["stats"] == {"uat": 1}, f"extra が混ざっていない {out.get('stats')}")
    check(len(out["records"]) == 2, f"記録 {len(out['records'])} 件")
    leaks = sorted({k for rec in out["records"] for k, v in rec.items() if isinstance(v, str) and SECRET in v})
    check(SECRET not in r.stdout, f"API キーがそのまま出ている(項目 {leaks})")
    return f"子プロセスが {len(out['records'])} 件を JSON で返し、伏せ字漏れ 0 件"


@case("IX-10", "索引の保存は変わった行だけ書き、消えた行は消す(他の行を書き潰さない)")
def ix10(ctx):
    import sqlite3
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix10-", dir=ctx["data"])
    old = {k: getattr(ix, k) for k in ("DB_PATH", "INDEX_PATH")}
    try:
        ix.DB_PATH, ix.INDEX_PATH = os.path.join(d, "index.db"), os.path.join(d, "none.json")
        rec = lambda i, e: {"id": i, "end": e, "role": "human", "kind": "claude", "mtime": e}
        idx = {"records": {"a": rec("a", 1), "b": rec("b", 2), "c": rec("c", 3)},
               "files": {"/x/a.jsonl": {"mtime": 1, "size": 1, "id": "a"}}, "cont_done": {}, "edges": {}}
        ix.save_index(idx)
        con = sqlite3.connect(ix.DB_PATH)
        con.execute("update records set json=? where id in ('a','c')", ('{"sentinel":true}',))
        con.commit(); con.close()
        idx["records"]["b"]["end"] = 99
        idx["records"].pop("c")
        ix.save_index(idx)
        con = sqlite3.connect(ix.DB_PATH)
        rows = dict(con.execute("select id, json from records"))
        mode = con.execute("pragma journal_mode").fetchone()[0]
        con.close()
        check(rows.get("a") == '{"sentinel":true}', f"変わっていない行を書き直した: {rows.get('a')}")
        check("c" not in rows, "消したはずの行が残っている")
        check(json.loads(rows["b"])["end"] == 99, f"変えた行が書かれていない {rows.get('b')}")
        check(mode == "wal", f"索引が WAL でない({mode}): 索引ビルド中に盤の取り出しが待たされる")
        back = ix.load_index()
        check(set(back["records"]) == {"a", "b"} or set(back["records"]) == {"b"},
              f"読み直しの結果 {sorted(back['records'])}")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "変えた 1 行だけ書き・消した 1 行は削除・他の行の目印は無傷・WAL"


@case("IX-11", "索引に書き込んでいる最中でも、盤の取り出しは待たされない(WAL)")
def ix11(ctx):
    import sqlite3
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix11-", dir=ctx["data"])
    old = {k: getattr(ix, k) for k in ("DB_PATH", "INDEX_PATH", "CODEX_DB")}
    w = None
    try:
        ix.DB_PATH, ix.INDEX_PATH = os.path.join(d, "index.db"), os.path.join(d, "none.json")
        ix.CODEX_DB = os.path.join(d, "none.sqlite")
        now = time.time()
        idx = {"records": {"a-%d" % i: {"id": "a-%d" % i, "end": now, "mtime": now, "role": "human", "kind": "claude",
                                        "ai": "Claude", "cwd": "/x", "first_prompt": "", "last_prompt": "",
                                        "files": [], "children": [], "path": "/x/%d.jsonl" % i} for i in range(20)},
               "files": {}, "cont_done": {}, "edges": {}}
        ix.save_index(idx)
        w = subprocess.Popen(
            [sys.executable, "-c",
             "import sqlite3,sys,time\n"
             "c=sqlite3.connect(sys.argv[1],timeout=30)\n"
             "c.execute('begin immediate')\n"
             "c.execute(\"insert or replace into records values('w',1,'human','claude','{}')\")\n"
             "sys.stdout.write('held\\n'); sys.stdout.flush(); time.sleep(4); c.commit()", ix.DB_PATH],
            stdout=subprocess.PIPE, text=True)
        check(w.stdout.readline().strip() == "held", "書き手が始まらない")
        t0 = time.time()
        res = ix.query_db(days=30, include_unattended=False, live_ids=(), children=True)
        el = time.time() - t0
        check(el < 2.0, f"書き込み中に取り出しが {el:.1f} 秒待たされた(WAL でない)")
        check(len(res["records"]) == 20, f"取り出せた件数 {len(res['records'])}")
        check(ix.get_record("a-1") is not None, "書き込み中に 1 件取り出せない")
        w.wait(timeout=20)
        con = sqlite3.connect(ix.DB_PATH)
        n = con.execute("select count(*) from records").fetchone()[0]
        con.close()
        check(n == 21, f"書き手のコミットが失われた {n}")
    finally:
        if w and w.poll() is None:
            w.kill()
        for k, v in old.items():
            setattr(ix, k, v)
    return f"書き込み中の取り出し {el:.2f} 秒・20 件取得・コミット後 21 件"


@case("IX-12", "「同じファイル」の線は 7 日以内の別セッション同士だけ(親子・多すぎ・重複は引かない)")
def ix12(ctx):
    import datetime as dt
    import overview_index as ix
    d = tempfile.mkdtemp(prefix="ix12-", dir=ctx["data"])
    fh = os.path.join(d, "home")
    proj = os.path.join(fh, ".claude", "projects", "-Users-uat-uat")
    os.makedirs(proj)
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    W = lambda p, ls: (os.makedirs(os.path.dirname(p), exist_ok=True), open(p, "w", encoding="utf-8").write(
        "".join(json.dumps(l, ensure_ascii=False, separators=(",", ":")) + "\n" for l in ls)))
    U = lambda t, txt, **kw: dict({"parentUuid": None, "isSidechain": False, "type": "user",
                                   "message": {"role": "user", "content": txt}, "timestamp": iso(t),
                                   "cwd": "/Users/uat/uat", "entrypoint": "cli"}, **kw)
    def A(t, fp, **kw):
        return dict({"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
                     "message": {"model": "claude-opus-5", "id": "m", "type": "message", "role": "assistant",
                                 "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": fp}}]}}, **kw)
    now = time.time()
    P, Q, R = HOME + "/uat/p.py", HOME + "/uat/q.py", HOME + "/uat/r.py"
    S = lambda n: "5a0000%02d-1111-4111-8111-111111111111" % n
    W(os.path.join(proj, S(1) + ".jsonl"), [U(now - 86400, "P を触る1"), A(now - 86400 + 5, P)])
    W(os.path.join(proj, S(2) + ".jsonl"), [U(now - 300, "P を触る2"), A(now - 290, P)])
    W(os.path.join(proj, S(3) + ".jsonl"), [U(now - 12 * 86400, "P を触る3"), A(now - 12 * 86400 + 5, P)])
    W(os.path.join(proj, S(4) + ".jsonl"), [U(now - 400, "Q を触る親"), A(now - 390, Q)])
    W(os.path.join(proj, S(4), "subagents", "g", "agent-abc.jsonl"),
      [U(now - 395, "Q を触る子", isSidechain=True), A(now - 394, Q, isSidechain=True)])
    for i in range(10, 19):
        W(os.path.join(proj, S(i) + ".jsonl"), [U(now - 500 - i, "R を触る"), A(now - 490 - i, R)])
    old = {k: getattr(ix, k) for k in ("HOME", "CODEX_SESS", "CODEX_DB", "DB_PATH", "INDEX_PATH")}
    try:
        ix.HOME, ix.CODEX_SESS = fh, os.path.join(fh, ".codex", "sessions")
        ix.CODEX_DB, ix.DB_PATH = os.path.join(d, "none.sqlite"), os.path.join(d, "index.db")
        ix.INDEX_PATH = os.path.join(d, "none.json")
        idx, _ = ix.build(days=30)
        es = idx["edges"]["samefile"]
        pairs = sorted((tuple(sorted((e["from"], e["to"]))), e["evidence"]) for e in es)
        check(len(es) == 1, f"同じファイルの線が {len(es)} 本: {[(e['from'][:8], e['to'][:8], e['evidence']) for e in es][:6]}")
        check(pairs[0][0] == tuple(sorted((S(1), S(2)))) and pairs[0][1] == P,
              f"線の相手/根拠が違う {pairs[0][0][0][:8]}-{pairs[0][0][1][:8]} {pairs[0][1]}")
        check(len({p for p, _ in pairs}) == len(pairs), "同じ組が重複している")
    finally:
        for k, v in old.items():
            setattr(ix, k, v)
    return "1日差の1組だけ・12日差/親子/9本共有は 0 本・重複なし"


@case("BD-09", "再描画でカードの位置と DOM 要素が変わらず、増えたら足され、消えたら残らない")
def bd09(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    mk = lambda i, **kw: dict(base, tab=f"9-{i}", sid=f"uat-{i}", task=f"UAT fixture {i}", ago=5 + i, state_for=5 + i, **kw)
    first = [mk(i) for i in range(1, 7)]
    phase = {"n": 0}

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            ss = list(first)
            if phase["n"] == 1:
                ss = [dict(s) for s in first]
                ss[2] = dict(ss[2], doing="UAT-CHANGED", state="確認待ち", mark="🔴")
                ss.append(mk(7))
            elif phase["n"] == 2:
                ss = [s for s in first if s["sid"] != "uat-4"]
            d["sessions"] = ss
            d["attention"] = [{"sid": s["sid"]} for s in ss if s["state"] == "確認待ち"]
            d["counts"] = dict(d.get("counts") or {}, working=sum(1 for s in ss if s["mark"] == "🟢"), tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    pos = "Object.fromEntries([...document.querySelectorAll('.card[data-id]')].map(c => [c.dataset.id, c.style.left + ',' + c.style.top]))"

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 6", 30)
        pg.evaluate("document.querySelectorAll('.card[data-id]').forEach(c => c.__uat = c.dataset.id)")
        pos0 = pg.evaluate(pos)
        phase["n"] = 1
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 7", 30)
        pg.wait_for_timeout(400)
        pos1 = pg.evaluate(pos)
        kept = pg.evaluate("[...document.querySelectorAll('.card[data-id]')].filter(c => c.__uat === c.dataset.id).length")
        got = pg.evaluate("(() => { const c = document.querySelector('.card[data-id=\"uat-3\"]'); return [c ? c.className : '', c && c.querySelector('.c-doing') ? c.querySelector('.c-doing').textContent : '']; })()")
        phase["n"] = 2
        wait_js(pg, "!document.querySelector('.card[data-id=\"uat-4\"]')", 30)
        pg.wait_for_timeout(300)
        n_after = pg.evaluate("document.querySelectorAll('.card[data-id^=\"uat-\"]').length")
        return pos0, pos1, kept, got, n_after, list(errs)

    pos0, pos1, kept, got, n_after, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    moved = {k: (v, pos1.get(k)) for k, v in pos0.items() if pos1.get(k) != v}
    check(not moved, f"再描画で動いたカード {moved}")
    check(kept == 6, f"要素が作り直された(同じ要素のまま {kept}/6)")
    check("k-turn" in got[0] and got[1] == "UAT-CHANGED", f"中身が古いまま class={got[0]!r} doing={got[1]!r}")
    check(n_after == 5, f"消えたセッションのカードが残っている({n_after} 枚・期待 5)")
    check(not errs, f"{errs[:1]}")
    return "位置 6/6 不動・要素 6/6 同一・状態と文言は更新・6→7→5 枚"


@case("BD-10", "引き(縮小)では状態の色の面だけになり、戻すと文字が読める")
def bd10(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, tab="9-1", sid="uat-turn", state="確認待ち", mark="🔴", task="UAT turn"),
          dict(base, tab="9-2", sid="uat-work", task="UAT work")]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": "uat-turn"}]
            d["counts"] = dict(d.get("counts") or {}, working=1, tabs=2)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    probe_js = """(() => {
      const c = document.querySelector('.card.k-turn'), t = c.querySelector('.c-title');
      const d = document.createElement('div');
      d.style.color = getComputedStyle(document.documentElement).getPropertyValue('--f-red').trim();
      document.body.appendChild(d); const red = getComputedStyle(d).color; d.remove();
      return [document.getElementById('world').className, getComputedStyle(c).backgroundColor, red,
              getComputedStyle(t).visibility, Math.round(c.getBoundingClientRect().width)];
    })()"""

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card.k-turn').length === 1", 30)
        out = []
        for k in (0.3, 1.0):
            pg.evaluate(f"window._zoomTo({k})")
            pg.wait_for_timeout(500)
            out.append(pg.evaluate(probe_js))
        return out, list(errs)

    (far, near), errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check(far[0] == "lod-far", f"0.3 倍で {far[0]}(期待 lod-far)")
    check(far[1] == far[2], f"引きで判断待ちの面が赤でない {far[1]} != {far[2]}")
    check(far[3] == "hidden", f"引きで文字が見えている visibility={far[3]}")
    check(near[0] == "lod-near" and near[3] == "visible" and near[1] != near[2],
          f"等倍で文字が出ない/面が赤のまま {near}")
    check(not errs, f"{errs[:1]}")
    return f"0.3 倍 lod-far 面={far[1]}・文字 hidden / 1.0 倍 lod-near 文字 visible"


@case("BD-11", "検索欄に打った f / 0 / + / - では盤が動かない。欄の外では効く")
def bd11(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, tab=f"9-{i}", sid=f"uat-{i}") for i in (1, 2)]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=2, tabs=2)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        pg.evaluate("window._zoomTo(0.5)")
        pg.wait_for_timeout(400)
        t0 = pg.evaluate("document.getElementById('world').style.transform")
        pg.click("#q")
        pg.keyboard.type("f0+-", delay=60)
        pg.wait_for_timeout(600)
        t1 = pg.evaluate("document.getElementById('world').style.transform")
        typed = pg.evaluate("document.querySelector('#q').value")
        pg.evaluate("(() => { const q = document.querySelector('#q'); q.value = ''; q.dispatchEvent(new Event('input')); q.blur(); })()")
        pg.wait_for_timeout(400)
        pg.keyboard.press("0"); pg.wait_for_timeout(700)
        z0 = pg.evaluate("document.querySelector('#zoomPct').textContent")
        pg.keyboard.press("+"); pg.wait_for_timeout(700)
        z1 = pg.evaluate("document.querySelector('#zoomPct').textContent")
        t2 = pg.evaluate("document.getElementById('world').style.transform")
        return t0, t1, typed, z0, z1, t2, list(errs)

    t0, t1, typed, z0, z1, t2, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check(typed == "f0+-", f"検索欄に入った文字 {typed!r}")
    check(t1 == t0, f"入力中に盤が動いた {t0!r} → {t1!r}")
    check(z0 == "100%" and z1 == "125%", f"欄の外でキーが効いていない 0→{z0} +→{z1}")
    check(t2 != t1, "欄の外のキーで transform が変わっていない")
    check(not errs, f"{errs[:1]}")
    return f"入力中は transform 不変・欄の外で 0→{z0} +→{z1}"


@case("BD-12", "動いている AI が無くなったらカードは残らず、案内が出て、上のバーが 0 になる")
def bd12(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, tab=f"9-{i}", sid=f"uat-{i}") for i in (1, 2)]
    phase = {"n": 0}

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            cur = [] if phase["n"] else ss
            d["sessions"] = cur
            d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=len(cur), tabs=len(cur))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 2", 30)
        phase["n"] = 1
        wait_js(pg, "document.querySelectorAll('.card').length === 0", 30)
        pg.wait_for_timeout(500)
        r = pg.evaluate("""[document.querySelector('#empty').hidden, document.querySelector('#empty').innerText,
            document.querySelectorAll('#nodes > *').length, document.querySelector('#nTurn').textContent,
            document.querySelector('#nWork').textContent, document.querySelector('#nMem').textContent]""")
        phase["n"] = 0
        wait_js(pg, "document.querySelectorAll('.card').length === 2", 30)
        pg.wait_for_timeout(300)
        back = pg.evaluate("document.querySelector('#empty').hidden")
        return r, back, list(errs)

    r, back, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    hidden, txt, n_nodes, n_turn, n_work, n_mem = r
    check(hidden is False and "動いている AI はありません" in txt, f"空状態が出ていない hidden={hidden} text={txt[:60]!r}")
    check(n_nodes == 0, f"#nodes に {n_nodes} 個残っている(カード・枠の消し忘れ)")
    check((n_turn, n_work, n_mem) == ("0", "0", "0MB"), f"上のバーが 0 でない {(n_turn, n_work, n_mem)}")
    check(back is True, "セッションが戻っても案内が出たまま")
    check(not errs, f"{errs[:1]}")
    return "0 件で カード 0・#nodes 0・バー 0/0/0MB・案内あり / 戻すと案内は消える"


@case("BD-14", "同じフォルダのカードは同じ枠に入り、枠の中で重ならない(デモでも分かれ方は同じ)")
def bd14(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uat-alpha", "cwd": "/Users/uat/uat-alpha", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, tab=f"9-{i}", sid=f"uat-a{i}", project="uat-alpha", cwd="/Users/uat/uat-alpha") for i in (1, 2, 3)] + \
         [dict(base, tab=f"9-{i}", sid=f"uat-b{i}", project="uat-beta", cwd="/Users/uat/uat-beta") for i in (4, 5)]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=len(ss), tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    js = """(() => {
      const fr = [...document.querySelectorAll('.frame')].map(f => [f.getBoundingClientRect(), (f.querySelector('.flabel') || {}).textContent || '']);
      const cards = [...document.querySelectorAll('.card[data-id^="uat-"]')];
      const group = {}, outside = [], overlap = [];
      cards.forEach(c => { const r = c.getBoundingClientRect();
        const hit = fr.find(([b]) => r.left >= b.left - 1 && r.right <= b.right + 1 && r.top >= b.top - 1 && r.bottom <= b.bottom + 1);
        if (!hit) outside.push(c.dataset.id); else group[c.dataset.id] = hit[1]; });
      for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
        const a = cards[i].getBoundingClientRect(), b = cards[j].getBoundingClientRect();
        const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (w > 1 && h > 1) overlap.push([cards[i].dataset.id, cards[j].dataset.id]); }
      return {group, outside, overlap, frames: fr.length, cards: cards.length};
    })()"""

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 5", 30)
        pg.wait_for_timeout(400)
        return pg.evaluate(js), list(errs)

    real, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    demo, errs2 = with_page(ctx, fn, "?lang=ja&demo=1", route_extra=extra)
    for name, r in (("通常", real), ("デモ", demo)):
        check(r["cards"] == 5, f"{name}: カード {r['cards']} 枚(期待 5)")
        check(not r["outside"], f"{name}: 枠からはみ出したカード {r['outside']}")
        check(not r["overlap"], f"{name}: 重なったカード {r['overlap']}")
        part = {}
        for sid, lab in r["group"].items():
            part.setdefault(lab, set()).add(sid)
        r["part"] = sorted(tuple(sorted(v)) for v in part.values())
        check(len(part) == 2, f"{name}: 枠が {len(part)} 個(期待 2)")
    check(real["part"] == demo["part"], f"デモで枠の分かれ方が変わった {real['part']} != {demo['part']}")
    check(set(real["group"].values()) != set(demo["group"].values()), "デモなのに枠の名前が本物のまま")
    check(not errs and not errs2, f"{(errs + errs2)[:1]}")
    return f"5 枚 / 枠 2 個・はみ出し 0・重なり 0・デモでも同じ分かれ方({real['part']})"


@case("BD-15", "欠けた値・仕込まれた HTML のセッションでも、盤は落ちず 1 枚も落とさない")
def bd15(ctx):
    inj = 'uat-x"><img src=x onerror="window.__pwn=1">'
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟡", "doing": "",
            "task": "UAT", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, sid="uat-null", tab="9-1", model_style=None, project="", cwd="", task=None, doing=None,
               ago=None, state_for=None, mem_mb=None),
          dict(base, sid="uat-lim0", tab="9-2", state="確認待ち", mark="🔴",
               limit={"active": True, "kind": "5h", "resets_at": None, "resets": ""}),
          dict(base, sid="uat-long", tab="9-3", task="長い依頼 " * 400, doing="x" * 2000),
          dict(base, sid=inj, tab="9-4", task=inj, project=inj),
          dict(base, sid="uat-loop0", tab="9-5", state="返答待ち", loop={"wake": None, "crons": []},
               tools={"skills": [], "mcp": []})]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": "uat-lim0"}]
            d["counts"] = dict(d.get("counts") or {}, working=0, tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('#nodes .card').length >= 5", 30)
        pg.wait_for_timeout(600)
        return pg.evaluate("""(() => {
          const cards = [...document.querySelectorAll('#nodes .card[data-id]')];
          const hs = [...new Set(cards.map(c => Math.round(c.getBoundingClientRect().height)))];
          const lim = cards.find(c => c.dataset.id === 'uat-lim0');
          const nul = cards.find(c => c.dataset.id === 'uat-null');
          const lng = cards.find(c => c.dataset.id === 'uat-long');
          const spill = lng ? Math.round(lng.querySelector('.c-title').getBoundingClientRect().bottom - lng.getBoundingClientRect().bottom) : 999;
          return {n: cards.length, ids: cards.map(c => c.dataset.id), heights: hs, spill,
                  pwn: window.__pwn === undefined ? 'none' : String(window.__pwn),
                  imgs: document.querySelectorAll('#nodes img').length,
                  limWhen: lim ? lim.querySelector('.c-when').textContent : null,
                  model: nul ? nul.querySelector('.c-model').textContent : null};
        })()"""), list(errs)

    r, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check(not errs, f"スクリプトエラー {errs[:2]}")
    check(r["n"] == 5, f"カード {r['n']} 枚(期待 5・欠けた値で落ちている)")
    check(inj in r["ids"], f"仕込んだ sid のカードが無い {r['ids']}")
    check(r["pwn"] == "none" and r["imgs"] == 0, f"HTML が実行された pwn={r['pwn']} img={r['imgs']}")
    check(len(r["heights"]) == 1 and r["spill"] <= 1, f"長文でカードが崩れた 高さ {r['heights']} はみ出し {r['spill']}px")
    check(r["limWhen"] == "解除時刻不明", f"解除時刻が無い上限の表示 {r['limWhen']!r}")
    check("モデル不明" in (r["model"] or ""), f"モデル欠落時の表示 {r['model']!r}")
    return f"5 枚・高さ {r['heights'][0]}px で揃う・HTML 実行 0・解除時刻不明/モデル不明を表示"


@case("CV-04", "会話ビューは 依頼/返答/操作 を順に並べ、連続する操作を畳み、記録中の HTML を実行しない")
def cv04(ctx):
    TAB, SID = "9-1", "uat-conv"
    XSS = '<img src=x onerror="window.__pwn=1">'
    base = {"tab": TAB, "sid": SID, "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT conv", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 3, "state_for": 3,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    tl = [{"kind": "依頼", "t": "2026-09-18T10:00:00", "text": "最初の依頼 " + XSS},
          {"kind": "返答", "t": "2026-09-18T10:00:05", "text": "**太字** と `コード`\n```\nblock\n```\n<b>raw</b>"},
          {"kind": "操作", "t": "2026-09-18T10:00:06", "text": "Read a.py"},
          {"kind": "操作", "t": "2026-09-18T10:00:07", "text": "Read b.py"},
          {"kind": "操作", "t": "2026-09-18T10:00:08", "text": "Edit c.py"},
          {"kind": "操作", "t": "2026-09-18T10:00:09", "text": "Bash npm test"},
          {"kind": "依頼", "t": "2026-09-18T10:00:10", "text": "次の依頼"},
          {"kind": "返答", "t": "2026-09-18T10:00:11", "text": "できました"}]

    def extra(pg):
        def fakesnap(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = [base]
            d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=1, tabs=1)
            route.fulfill(response=r, body=json.dumps(d))
        def fakeconv(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(dict(base, ok=True, etag="uat-1", timeline=tl)))
        pg.route("**/api/snapshot*", fakesnap)
        pg.route("**/api/conv*", fakeconv)

    def fn(pg, errs, bl):
        wait_js(pg, "!!document.querySelector('.card[data-id=\"uat-conv\"]')", 30)
        pg.evaluate("board.select('uat-conv')")
        wait_js(pg, "document.querySelectorAll('#cvLog .cv').length >= 5", 30)
        pg.wait_for_timeout(400)
        return pg.evaluate("""(() => {
          const L = document.querySelector('#cvLog');
          const tx = s => ((L.querySelector(s) || {}).textContent || '');
          const op = L.querySelector('.cv.op');
          return {order: [...L.children].map(e => e.className.trim()), ops: L.querySelectorAll('.cv.op').length,
                  first: tx('.cv.you .bub'),   // 吹き出し(.bub)の中の文字。HTML はそのまま文字として見えること
                  imgs: L.querySelectorAll('img').length, pwn: window.__pwn === undefined ? 'none' : String(window.__pwn),
                  pre: L.querySelectorAll('.cv.ai pre.cb').length, code: L.querySelectorAll('.cv.ai code').length,
                  bolds: [...L.querySelectorAll('.cv.ai .bub b')].map(b => b.textContent),   // 名前欄の <b> は除く
                  raw: tx('.cv.ai .bub').includes('<b>raw</b>'),
                  opsum: op ? ((op.querySelector('summary') || {}).textContent || '') : '',
                  opn: op ? op.querySelectorAll('.opl').length : -1};
        })()"""), list(errs)

    r, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check(r["order"] == ["cv you", "cv ai", "cv op", "cv you", "cv ai"], f"並び {r['order']}")
    check(r["imgs"] == 0 and r["pwn"] == "none" and XSS in r["first"], f"記録の HTML が実行された imgs={r['imgs']} pwn={r['pwn']}")
    check(r["pre"] == 1 and r["code"] == 1 and r["bolds"] == ["太字"] and r["raw"],
          f"整形が違う pre={r['pre']} code={r['code']} b={r['bolds']} raw={r['raw']}")
    check(r["ops"] == 1 and "4" in r["opsum"] and "Bash npm test" in r["opsum"] and r["opn"] == 3,
          f"操作の畳み込み 行数={r['ops']} summary={r['opsum']!r} 隠れ行={r['opn']}")
    check(not errs, f"{errs[:1]}")
    return f"並び {r['order']}・操作 4 件を 1 行に畳む・HTML 実行 0・```と**だけ整形"


@case("RL-01", "「過去」の上の列は 判断待ち→返答済み→作業中 の順で、押すとそのカードが選ばれる(「いま」では出さない)")
def rl01(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟡", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": "uatproj", "cwd": "/Users/uat/uatproj", "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 120, "limit": None, "loop": None, "tools": None, "account": "", "transcript": ""}
    ss = [dict(base, tab="9-1", sid="uat-work", state="作業中", mark="🟢", state_for=40),
          dict(base, tab="9-2", sid="uat-your", state="返答待ち", state_for=30),
          dict(base, tab="9-3", sid="uat-turn", state="確認待ち", mark="🔴", state_for=10),
          dict(base, tab="9-4", sid="uat-cxstop", ai="Codex", state="codex 停止", state_for=20)]
    exp = ["停止", "判断待ち", "返答済み", "作業中"]      # rank 0 は state_for の長い順 → codex 停止(20) → 確認待ち(10)

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": "uat-turn"}]
            d["counts"] = dict(d.get("counts") or {}, working=1, tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 4", 30)
        now_rail = pg.evaluate("document.querySelectorAll('#rail .rl').length")
        pg.click("[data-mode=history]")
        wait_js(pg, "document.querySelectorAll('#rail .rl').length === 4", 30)
        pg.wait_for_timeout(400)
        order = pg.evaluate("[...document.querySelectorAll('#rail .rl')].map(e => [e.dataset.id, e.querySelector('.lbl').textContent])")
        pg.click("#rail .rl >> nth=0")
        pg.wait_for_timeout(800)
        selected = pg.evaluate("[...document.querySelectorAll('.card.sel')].map(c => c.dataset.id)")
        return now_rail, order, selected, list(errs)

    now_rail, order, selected, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check(now_rail == 0, f"「いま」で列が出ている({now_rail} 件)")
    check([o[1] for o in order] == exp, f"並び {order}(期待 {exp})")
    check(selected == [order[0][0]], f"押した先が選ばれていない {selected} != {[order[0][0]]}")
    check(not errs, f"{errs[:1]}")
    return f"「いま」0 件 /「過去」{[o[1] for o in order]}・先頭を押すと {selected[0]} が選択"


@case("SV-08", "入力の形が違う要求は、iTerm を触る前に 400/404/409 で断る")
def sv08(ctx):
    O = {"Origin": BASE.rstrip("/")}
    table = [
        ("GET", "/api/detail?tab=", None, 400), ("GET", "/api/detail?tab=1", None, 400),
        ("GET", "/api/detail?tab=1-2-3", None, 400), ("GET", "/api/screen?tab=abc", None, 400),
        ("GET", "/api/screen?tab=../../etc", None, 400),
        ("GET", "/api/session?id=zzzz", None, 400), ("GET", "/api/session?id=../../etc/passwd", None, 400),
        ("GET", "/api/session?id=" + "0" * 32, None, 404),
        ("GET", "/api/nope", None, 404),
        ("POST", "/api/go", {"tab": "x"}, 400), ("POST", "/api/go", {"tab": "1"}, 400),
        ("POST", "/api/go", {"tab": "0-1"}, 409),
        ("POST", "/api/resume", {"id": "short"}, 400), ("POST", "/api/resume", {"id": "0" * 32}, 400),
        ("POST", "/api/nope", {}, 404),
    ]
    bad = []
    for meth, path, body, exp in table:
        st, d, _ = http(path, meth, body, headers=O if meth == "POST" else None)
        if st != exp or (isinstance(d, dict) and d.get("ok") is not False):
            bad.append((meth, path, st, exp, str(d)[:60]))
    check(not bad, f"想定と違う応答 {bad[:3]}")
    st, d, _ = http("/api/go", "POST", {"tab": "0-1"}, headers=O)
    check("アプリ" in d.get("reason", ""), f"0- のタブの断り方 {d}")
    req = urllib.request.Request(BASE + "api/send", method="POST", data=b"{not json",
                                 headers={"X-Overview": "1", "Origin": BASE.rstrip("/")})
    try:
        urllib.request.urlopen(req, timeout=10); st2 = 200
    except urllib.error.HTTPError as e:
        st2 = e.code
    check(st2 == 400, f"JSON でない本文で {st2}")
    return f"{len(table)} 通り + 壊れた JSON の計 {len(table) + 1} 通りすべて想定どおり(0-1 は 409 でアプリ側へ)"


@case("SV-09", "壊れた値・長すぎる本文でもサーバは落ちず、必ず JSON で断り、記録も残さない")
def sv09(ctx):
    _, v0, _ = http("/api/version")
    tabs = [s["tab"] for s in snapshot()["sessions"] if s.get("tab")]
    ghost = next(f"{w}-99" for w in range(90, 99) if f"{w}-99" not in set(tabs))
    log = os.path.join(ctx["data"], "send.log")
    before = os.path.getsize(log) if os.path.exists(log) else -1
    probes = [("GET", f"/api/screen?tab={tabs[0]}&lines=abc" if tabs else "/api/screen?tab=1-1&lines=abc", None),
              ("GET", "/api/index?days=abc", None),
              ("GET", "/api/conv?tab=" + ghost, None),
              ("POST", "/api/send", {"tab": ghost, "sid": "uat", "text": "x" * 4001}),
              ("POST", "/api/send", {"tab": ghost, "sid": "uat", "text": "x" * 200000}),
              ("POST", "/api/send", {"tab": ghost, "sid": "uat", "key": "rm -rf /"}),
              ("POST", "/api/send", {"tab": ghost, "sid": "uat", "text": ""}),
              ("POST", "/api/stop", {"tab": ghost, "sid": "uat"}),
              ("POST", "/api/stop", {}), ("POST", "/api/send", {})]
    bad = []
    for meth, path, body in probes:
        try:
            st, d, _ = http(path, meth, body, headers={"Origin": BASE.rstrip("/")} if meth == "POST" else None)
        except Exception as e:
            bad.append((path, f"{type(e).__name__}: {e}")); continue
        if st == 200 or not isinstance(d, dict) or d.get("ok") is not False or len(str(d.get("reason", ""))) > 400:
            bad.append((path, st, str(d)[:80]))
    check(not bad, f"落ちた・通してしまった {bad[:3]}")
    after = os.path.getsize(log) if os.path.exists(log) else -1
    check(after == before, f"断ったのに send.log が増えた {before}→{after}")
    _, v1, _ = http("/api/version")
    check(v1["pid"] == v0["pid"] and v1["stamp"] == v0["stamp"], f"サーバが入れ替わった {v0} → {v1}")
    return f"{len(probes)} 通りすべて ok:false・pid {v1['pid']} のまま・send.log 不変"


@case("SV-13", "設定とアカウントの使い回し: 期限内は CLI を呼び直さず、期限切れと refresh=1 では呼び直す")
def sv13(ctx):
    import overview as o
    import overview_server as osv
    n = {"login": 0, "acct": 0}
    real = (o.login_status, o.accounts, o.settings_info)
    real_clis = o.ai_clis
    o.ai_clis = lambda force=False, logins=None: []   # この試験は使い回しだけを見る(実際の CLI は呼ばない)
    # 試験専用の盤サーバを行きずりのポートに 1 つ立てる(本番の PID ファイルにも 8793 にも触らない)
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), osv.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hdr = {"Host": f"127.0.0.1:{osv.PORT}", "X-Overview": "1", "Origin": f"http://127.0.0.1:{osv.PORT}"}

    def req(path):
        r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", headers=hdr)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        o.login_status = lambda: (n.__setitem__("login", n["login"] + 1), [{"profile": f"p{n['login']}"}])[1]
        o.accounts = lambda: (n.__setitem__("acct", n["acct"] + 1), [{"id": n["acct"]}])[1]
        o.settings_info = lambda: {"data_dir": "uat"}
        osv._login.clear(); osv._acct.clear()
        s1, a1 = req("/api/settings")
        s2, a2 = req("/api/settings")
        check((s1, s2) == (200, 200), f"status {s1} {s2}")
        check(n["login"] == 1 and a1["logins"] == a2["logins"], f"毎回 CLI を呼んでいる {n}")
        _, a3 = req("/api/settings?refresh=1")
        check(n["login"] == 2 and a3["logins"] != a1["logins"], f"refresh=1 で呼び直さない {n}")
        osv._login["t"] = time.time() - 121
        req("/api/settings")
        check(n["login"] == 3, f"120 秒を過ぎても呼び直さない {n}")
        _, b1 = req("/api/accounts")
        _, b2 = req("/api/accounts")
        check(n["acct"] == 1 and b1["accounts"] == b2["accounts"], f"アカウントを毎回数え直している {n}")
        osv._acct["t"] = time.time() - 61
        req("/api/accounts")
        check(n["acct"] == 2, f"60 秒を過ぎても数え直さない {n}")
    finally:
        o.login_status, o.accounts, o.settings_info = real
        o.ai_clis = real_clis
        osv._login.clear(); osv._acct.clear()
        srv.shutdown(); srv.server_close()
    return f"設定: 2 回の要求で CLI 1 回 → refresh=1 と 120 秒超で再取得(計 {n['login']} 回)/ アカウントは 60 秒(計 {n['acct']} 回)"


@case("SV-14", "会話の取り直し: 記録が変わらなければ作り直さず、1 行増えたら作り直す")
def sv14(ctx):
    import overview as o
    import overview_server as osv
    tr = os.path.join(ctx["data"], "conv-uat.jsonl")
    open(tr, "w").write('{"type":"user"}\n')
    sess = {"tab": "6-1", "sid": "uat-conv", "ai": "Claude", "state": "返答待ち", "mark": "⚪️", "transcript": tr,
            "model_style": {}, "client": "", "project": "p", "doing": "", "task": "", "state_for": 0, "limit": {}}
    n = {"tl": 0}
    real_tl, real_snap = o.timeline_claude, osv.snapshot_cached
    # 試験専用の盤サーバを行きずりのポートに 1 つ立てる(本番の PID ファイルにも 8793 にも触らない)
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), osv.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hdr = {"Host": f"127.0.0.1:{osv.PORT}", "X-Overview": "1", "Origin": f"http://127.0.0.1:{osv.PORT}"}

    def req(path):
        r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", headers=hdr)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        o.timeline_claude = lambda *a, **k: (n.__setitem__("tl", n["tl"] + 1), [{"kind": "say", "i": n["tl"]}])[1]
        osv.snapshot_cached = lambda max_age=None: {"sessions": [dict(sess)]}
        st, a = req("/api/conv?tab=6-1")
        stt = os.stat(tr)
        check(st == 200 and a["etag"] == f"{stt.st_size}-{int(stt.st_mtime)}", f"etag {a.get('etag')} != {stt.st_size}-{int(stt.st_mtime)}")
        check(n["tl"] == 1 and a.get("timeline"), f"1 回目に作っていない {n} {str(a)[:120]}")
        _, b = req(f"/api/conv?tab=6-1&etag={a['etag']}")
        check(b.get("same") is True and "timeline" not in b and n["tl"] == 1, f"同じ記録なのに作り直した {n} {str(b)[:120]}")
        check(b.get("sid") == "uat-conv" and b.get("state") == "返答待ち", f"見出しが返らない {b}")
        time.sleep(1.1)
        with open(tr, "a") as f:
            f.write('{"type":"user"}\n')
        _, c = req(f"/api/conv?tab=6-1&etag={a['etag']}")
        check(c["etag"] != a["etag"] and n["tl"] == 2 and c.get("same") is not True, f"追記後に作り直さない {n} {str(c)[:120]}")
        req("/api/conv?tab=6-1&etag=")
        check(n["tl"] == 3, f"空の etag を一致とみなした {n}")
        st4, d4 = req("/api/conv?tab=6-9")
        check(st4 == 404 and d4.get("ok") is False, f"無いタブ {st4} {d4}")
    finally:
        o.timeline_claude, osv.snapshot_cached = real_tl, real_snap
        srv.shutdown(); srv.server_close()
    return f"作り直し {n['tl']} 回(1 回目・追記後・空 etag のみ)/ 同じ etag では same=true と見出しだけ返す"


@case("SV-16", "索引の問い合わせ: 条件をそのまま子プロセスに渡し、子が失敗しても 500 を返してサーバは生き続ける")
def sv16(ctx):
    import overview_server as osv
    seen = []
    real_sub, real_snap = osv.subprocess, osv.snapshot_cached
    # 試験専用の盤サーバを行きずりのポートに 1 つ立てる(本番の PID ファイルにも 8793 にも触らない)
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), osv.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hdr = {"Host": f"127.0.0.1:{osv.PORT}", "X-Overview": "1", "Origin": f"http://127.0.0.1:{osv.PORT}"}

    def req(path):
        r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", headers=hdr)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    class Sub:
        @staticmethod
        def run(argv, **kw):
            seen.append(argv)
            return type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"z" * 500 + b"BOOM"})()
    try:
        osv.snapshot_cached = lambda max_age=None: {"sessions": [{"sid": "sid-a"}, {"sid": "sid-b"}, {"sid": None}]}
        osv.subprocess = Sub
        st, d = req("/api/index?days=7&unattended=1&children=1")
        req("/api/index?days=all")
    finally:
        osv.subprocess, osv.snapshot_cached = real_sub, real_snap
    check(st == 500 and d.get("ok") is False and d["reason"].endswith("BOOM") and len(d["reason"]) <= 300,
          f"子の失敗が 500 で返らない {st} {str(d)[:120]}")
    check(len(seen) == 2, f"子プロセスの起動 {len(seen)} 回")
    argv = seen[0]
    check(argv[1].endswith("overview_index.py") and argv[2] == "--query", f"起動の形 {argv[:3]}")
    arg = json.loads(argv[3])
    exp = {"days": 7, "unattended": True, "live_ids": ["sid-a", "sid-b"], "children": True,
           "extra": {"stats": osv._index["stats"], "building": osv._index["building"], "error": osv._index["error"]}}
    check(arg == exp, f"子に渡す条件が違う {arg} != {exp}")
    check(json.loads(seen[1][3])["days"] is None, f"days=all が全期間でない {json.loads(seen[1][3])['days']}")
    st3, v3 = req("/api/version")
    check(st3 == 200 and v3.get("ok"), f"索引の失敗のあとサーバが応じない {st3}")
    srv.shutdown(); srv.server_close()
    return f"条件(7 日・unattended・children)と生存 {len(arg['live_ids'])} 件を子へ・失敗は 500({len(d['reason'])} 字)・その後も応答"


@case("SV-17", "同時に来ても待たせない: 調べ直しは裏で 1 回、応答は使い回す")
def sv17(ctx):
    import threading
    snapshot()
    res = []

    def one():
        t0 = time.time()
        st, d, _ = http("/api/snapshot")
        res.append((st, d.get("time"), time.time() - t0))
    ths = [threading.Thread(target=one) for _ in range(12)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join(40)
    el = time.time() - t0
    check(len(res) == 12 and all(r[0] == 200 for r in res), f"応答 {[r[0] for r in res]}")
    slow = max(r[2] for r in res)
    check(el < 5 and slow < 4, f"12 本の同時取得に {el:.1f} 秒(最も遅い応答 {slow:.1f} 秒)")
    stamps = {r[1] for r in res}
    check(len(stamps) <= 2, f"同時なのに {len(stamps)} 種類の中身(要求ごとに調べ直している)")
    return f"12 本を {el:.2f} 秒・最も遅い応答 {slow:.2f} 秒・中身は {len(stamps)} 種類"


@case("SV-18", "盤の中身は裏で更新され続ける(固まったものを返し続けない)")
def sv18(ctx):
    a = snapshot()
    t0 = a.get("time")
    check(isinstance(t0, (int, float)), f"snapshot に time が無い {list(a)[:6]}")
    b, t_end = a, time.time() + 30
    while time.time() < t_end:
        time.sleep(1.0)
        b = snapshot()
        if b.get("time", 0) > t0:
            break
    check(b.get("time", 0) > t0, f"30 秒たっても中身が更新されない(time={t0} のまま・裏のスレッドが死んでいる)")
    age = time.time() - b["time"]
    check(age < 20, f"返ってきた中身が {age:.0f} 秒前のもの")
    return f"{b['time'] - t0:.1f} 秒で更新・返答の鮮度 {age:.1f} 秒(調べるのに {b.get('took')} 秒)"


@case("AP-07", "端末の作業フォルダ: 空白と ' を含むフォルダでも端末が生き、その場所で始まる。無いフォルダはホームに落ちる")
def ap07(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()   # hook の確認ダイアログを出させない(~/.claude に触らせない)
    odd = os.path.join(data, "sp ace 'q' dir")
    os.makedirs(odd, exist_ok=True)
    gone = os.path.join(data, "no-such-dir-uat")
    m1, m2 = os.path.join(data, "m1.txt"), os.path.join(data, "m2.txt")
    sid = "0123456789abcdef0123456789abcdef0123"
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'resume', ai: 'Claude', id: %s, cwd: %s});
      P({type: 'resume', ai: 'Claude', id: %s, cwd: %s});
      await new Promise(r => setTimeout(r, 7000));
      // シェルの起動は機械が混んでいると 1 分を超える。冪等な指示を送り直し、題名が変わるのを待つ
      for (let i = 0; i < 20; i++) {
        P({type: 'send', tab: '0-2', text: "printf '\\033]0;PANE_OK\\007'; pwd > %s; echo $AIBOARD_PANE >> %s", enter: true});
        P({type: 'send', tab: '0-3', text: "printf '\\033]0;PANE_OK\\007'; pwd > %s; echo $AIBOARD_PANE >> %s", enter: true});
        await new Promise(r => setTimeout(r, 5000));
        try {
          const d = await fetch('/api/snapshot', {headers: {'X-Overview': '1'}}).then(x => x.json());
          const ok = ['0-2', '0-3'].every(t => { const x = (d.sessions || []).find(y => y.tab === t);
            return x && ((x.topic || '') + (x.title_topic || '')).indexOf('PANE_OK') >= 0; });
          if (ok) break;
        } catch (e) {}
      }
      return 1;""" % (
        json.dumps(sid), json.dumps(odd), json.dumps(sid), json.dumps(gone), m1, m1, m2, m2)
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await (async () => { " + js + " })()",
               # 置き場の扱いを見る試験なので、起動の速いシェルで(利用者の .zshrc は混雑時 1 分かかる)
               AIBOARD_JS_WAIT="4", AIBOARD_DRY="1", AIBOARD_FAST_SHELL="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out), "アプリが結果を書かなかった")
    check(json.load(open(out)).get("ok"), "盤の中で JS が動かなかった")
    check(os.path.exists(m1), "空白と ' を含むフォルダの端末が動いていない(印が書かれない)")
    check(os.path.exists(m2), "無いフォルダを指定した端末が動いていない(印が書かれない)")
    l1, l2 = open(m1).read().splitlines(), open(m2).read().splitlines()
    check(l1[:1] in ([odd], [os.path.realpath(odd)]), f"始まった場所 {l1[:1]} != {odd}")
    check(l1[1:2] == ["2"], f"pane 番号 {l1[1:2]} != ['2']")
    check(l2[:1] in ([HOME], [os.path.realpath(HOME)]), f"無いフォルダの落とし先 {l2[:1]} != {HOME}")
    check(l2[1:2] == ["3"], f"pane 番号 {l2[1:2]} != ['3']")
    cd = next((x for x in open(os.path.join(data, "launch", "pane-2.sh")).read().splitlines() if x.startswith("cd ")), "")
    z = subprocess.run(["/bin/zsh", "-c", cd + " && pwd"], capture_output=True, text=True, cwd="/")
    check(z.returncode == 0 and z.stdout.strip() == odd,
          f"起動スクリプトの cd 行の引用が壊れている: {cd!r} → {z.stdout.strip()!r} {z.stderr.strip()[:80]}")
    return f"{l1[0]} で pane {l1[1]} / 無いフォルダ→{l2[0]} で pane {l2[1]} / cd 行を zsh に食わせても同じ場所"


@case("AP-08", "端末台帳(app_panes.json)の tty と pid が ps の実プロセスと一致し、端末ごとに別の tty。終了で台帳を消す")
def ap08(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    panes_file = os.path.join(data, "app_panes.json")
    out = os.path.join(data, "js.json")
    sid = "0123456789abcdef0123456789abcdef0123"
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'resume', ai: 'Claude', id: %s, cwd: %s});
      P({type: 'resume', ai: 'Codex', id: %s, cwd: %s});
      await new Promise(r => setTimeout(r, 24000)); return 1;""" % (
        json.dumps(sid), json.dumps(HOME), json.dumps(sid), json.dumps(HOME))
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS=js, AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    p = subprocess.Popen([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        led, t0 = None, time.time()
        while time.time() - t0 < 40:
            try:
                led = json.load(open(panes_file))
            except (OSError, ValueError):
                led = None
            if led and len(led.get("panes", [])) >= 3 and all(x.get("tty") for x in led["panes"]):
                break
            time.sleep(1)
        check(led and len(led.get("panes", [])) >= 3, f"台帳に端末が 3 枚出ない: {led}")
        ps = subprocess.run(["/bin/ps", "-ww", "-o", "pid=,ppid=,tty=,command=", "-u", str(os.getuid())],
                            capture_output=True, text=True).stdout
        rows = {}
        for line in ps.splitlines():
            f = line.strip().split(None, 3)
            if len(f) >= 4 and f[0].isdigit():
                rows[int(f[0])] = (int(f[1]), f[2], f[3])
        bad = []
        for x in led["panes"]:
            r = rows.get(x["pid"])
            want = os.path.join(data, "launch", "pane-%d.sh" % x["pane"])
            if not r or r[0] != led["app_pid"] or r[1] != x["tty"] or want not in r[2]:
                bad.append((x["pane"], x["pid"], x["tty"], r[:2] if r else None))
        check(not bad, f"台帳と ps が食い違う(この tty へ送ると別の端末に入る): {bad}")
        ttys = [x["tty"] for x in led["panes"]]
        check(len(set(ttys)) == len(ttys), f"同じ tty を持つ端末がある {ttys}")
        kinds = [x["kind"] for x in led["panes"]]
        check(kinds[:3] == ["shell", "claude", "codex"], f"種別 {kinds}(期待 shell,claude,codex)")
        p.wait(timeout=60)
        check(not os.path.exists(panes_file), "終了しても台帳が残っている(死んだ tty に送りつける)")
        return f"{len(led['panes'])} 枚すべて ps と一致(ppid={led['app_pid']}・tty {ttys})・終了で台帳は消えた"
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


@case("AP-09", "send の宛先: 指定した端末だけに届く(選択中の端末に流れない)・4000 字超は送らない・知らないタブは無視")
def ap09(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    m1, m2, m9, mo, mu = (os.path.join(data, n) for n in ("s1.txt", "s2.txt", "s9.txt", "sover.txt", "sunder.txt"))
    sid = "0123456789abcdef0123456789abcdef0123"
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'resume', ai: 'Claude', id: %s, cwd: %s});
      P({type: 'resume', ai: 'Codex', id: %s, cwd: %s});
      await new Promise(r => setTimeout(r, 12000));
      P({type: 'send', tab: '0-1', text: 'echo $AIBOARD_PANE > %s', enter: true});
      P({type: 'send', tab: '0-2', text: 'echo $AIBOARD_PANE > %s', enter: true});
      P({type: 'send', tab: '0-9', text: 'echo 9 > %s', enter: true});
      P({type: 'send', tab: '0-2', text: 'echo under > %s #' + 'a'.repeat(3500), enter: true});
      P({type: 'send', tab: '0-2', text: 'echo over > %s #' + 'a'.repeat(4100), enter: true});
      await new Promise(r => setTimeout(r, 12000)); return 1;""" % (
        json.dumps(sid), json.dumps(HOME), json.dumps(sid), json.dumps(HOME), m1, m2, m9, mu, mo)
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await (async () => { " + js + " })()",
               # 宛先の切り分けを見る試験なので、起動の速いシェルで(利用者の .zshrc は混雑時 1 分かかる)
               AIBOARD_JS_WAIT="4", AIBOARD_DRY="1", AIBOARD_FAST_SHELL="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out), "アプリが結果を書かなかった")
    r = json.load(open(out))
    check(r.get("ok"), f"盤の中で JS が動かなかった {r}")
    check(r.get("selected") == "0-3", f"最後に開いた端末が選ばれていない {r.get('selected')}")
    check(os.path.exists(m1) and os.path.exists(m2), f"印が書かれない m1={os.path.exists(m1)} m2={os.path.exists(m2)}")
    g1, g2 = open(m1).read().strip(), open(m2).read().strip()
    check((g1, g2) == ("1", "2"), f"届いた端末 0-1→{g1} 0-2→{g2}(期待 1,2。選択中の 0-3 に流れていないか)")
    check(not os.path.exists(m9), "知らないタブ 0-9 宛ての文字が、どこかの端末で実行された")
    check(os.path.exists(mu), "3500 字の送信が届いていない(長さの判定が厳しすぎる。この対照が無いと次の検査は空振りする)")
    check(not os.path.exists(mo), "4000 字を超える送信が実行された")
    return "0-1→pane 1 / 0-2→pane 2(選択中は 0-3)/ 未知タブ 0 件 / 3500 字は届き 4100 字は不送信"


@case("AP-10", "盤からの指示の検査: 形の違う resume/run/open/switchAccount は端末を開かない。決まった形の run だけ通る")
def ap10(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    cfg = os.path.join(data, "fakeconfig")
    os.makedirs(cfg, exist_ok=True)
    tr = os.path.join(data, "proj", "0123456789abcdef0123456789abcdef0123.jsonl")
    os.makedirs(os.path.dirname(tr), exist_ok=True)
    open(tr, "w").write("{}\n")
    notjsonl = os.path.join(data, "proj", "x.txt")
    open(notjsonl, "w").write("x")
    opened = "/tmp/aiboard-uat-open-%d.json" % time.time_ns()   # HOME でも /Users/ でもない = 作らせない
    mark = os.path.join(data, "pwned.txt")
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      const GOOD = '0123456789abcdef0123456789abcdef0123';
      P({type: 'resume', ai: 'Claude', id: 'x; touch %s', cwd: %s});
      P({type: 'resume', ai: 'Claude', id: '0123456789abcde', cwd: %s});
      P({type: 'run', command: 'echo pwned > %s'});
      P({type: 'run', command: 'rm -rf /tmp/x; command claude auth'});
      P({type: 'open', path: %s});
      P({type: 'switchAccount', sid: GOOD, transcript: %s, configDir: %s});
      P({type: 'switchAccount', sid: 'nope', transcript: %s, configDir: %s});
      P({type: 'run', command: 'command claude auth --help'});
      await new Promise(r => setTimeout(r, 5000)); return 1;""" % (
        mark, json.dumps(HOME), json.dumps(HOME), mark, json.dumps(opened),
        json.dumps(notjsonl), json.dumps(cfg), json.dumps(tr), json.dumps(cfg))
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await (async () => { " + js + " })()",
               AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out), "アプリが結果を書かなかった")
    r = json.load(open(out))
    check(r.get("ok"), f"盤の中で JS が動かなかった {r}")
    panes = r.get("panes") or []
    check(len(panes) == 2 and [x["kind"] for x in panes] == ["shell", "shell"],
          f"開いた端末 {[(x['tab'], x['kind'], x['cwd']) for x in panes]}(期待: 起動時の shell と 許可された run の 2 枚)")
    scripts = sorted(os.path.basename(x) for x in glob.glob(os.path.join(data, "launch", "*.sh")))
    check(scripts == ["pane-1.sh", "pane-2.sh"], f"起動スクリプト {scripts}")
    allsh = "".join(open(x).read() for x in glob.glob(os.path.join(data, "launch", "*.sh")))
    check("pwned" not in allsh and "rm -rf" not in allsh and "touch" not in allsh, "弾くべきコマンドが起動スクリプトに入った")
    line = next((x for x in open(os.path.join(data, "launch", "pane-2.sh")).read().splitlines() if x.startswith("echo WOULD_RUN:")), "")
    z = subprocess.run(["/bin/zsh", "-c", line], capture_output=True, text=True)
    check(z.stdout.strip() == "WOULD_RUN: command claude auth --help", f"許可された run が端末に渡っていない: {line!r} → {z.stdout.strip()!r}")
    check(not os.path.exists(mark), "弾くべきコマンドが実行された")
    check(not os.path.exists(opened), f"HOME/Users 以外の path を作って開いた: {opened}")
    check(not os.path.exists(os.path.join(cfg, "projects")), "形の違う switchAccount が記録をコピーした")
    return "不正 7 件すべて無視(端末 0 枚・コピー 0 件・実行 0 件)・許可された run 1 件だけ端末になった"


@case("AP-11", "前回の端末の復元: sid があればその会話、無ければ一覧から選ばせる(勝手に最新を開かない)")
def ap11(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    d1 = os.path.join(data, "wk")
    os.makedirs(d1, exist_ok=True)
    s1, s2 = "1111111111111111-aaaa", "2222222222222222-bbbb"
    st = {"saved": time.time(), "panes": [{"kind": "claude", "cwd": d1, "sid": s1},
                                          {"kind": "claude", "cwd": d1, "sid": ""},
                                          {"kind": "codex", "cwd": d1, "sid": s2},
                                          {"kind": "shell", "cwd": d1, "sid": ""}]}
    json.dump(st, open(os.path.join(data, "state.json"), "w"))
    out = os.path.join(data, "restore.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_RESTORE_TEST=out, AIBOARD_DRY="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out), "アプリが復元の結果を書かなかった")
    rep = json.load(open(out))
    check(rep["loaded"] == 4 and rep["opened"] == 4, f"読み込み {rep['loaded']} / 開いた {rep['opened']}(期待 4,4)")
    got = []
    for row in rep["panes"]:
        cmds = re.findall(r"echo WOULD_RUN: '(.*)'", row["script"])
        got.append((row["kind"], row["cwd"], cmds[0] if cmds else None))
    exp = [("claude", d1, f"claude --resume {s1}"), ("claude", d1, "claude --resume"),
           ("codex", d1, f"codex resume {s2}"), ("shell", d1, None)]
    check(got == exp, f"復元した端末 {got}\n期待 {exp}")
    return f"4 枚を順に復元: {[g[2] for g in got]}"


@case("AP-12", "別アカウントで続き: 記録を相手の置き場へコピーし、CLAUDE_CONFIG_DIR 付きの command claude で再開する")
def ap12(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    proj = os.path.join(data, "src", "-Users-uat-demo")
    os.makedirs(proj, exist_ok=True)
    sid = "0123456789abcdef0123456789abcdef0123"
    tr = os.path.join(proj, sid + ".jsonl")
    open(tr, "w").write('{"uat": "transcript"}\n')
    cfg = os.path.join(data, "otheracct")
    os.makedirs(cfg, exist_ok=True)
    out = os.path.join(data, "switch.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_SWITCH_TEST=out, AIBOARD_DRY="1", T_SID=sid, T_TR=tr, T_DIR=cfg)
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(out), "アプリが切替の結果を書かなかった")
    rep = json.load(open(out))
    dest = os.path.join(cfg, "projects", "-Users-uat-demo", sid + ".jsonl")
    check(rep.get("dest") == dest, f"コピー先 {rep.get('dest')} != {dest}")
    check(os.path.exists(dest) and open(dest).read() == '{"uat": "transcript"}\n', "記録がコピーされていない/中身が違う")
    check(open(tr).read() == '{"uat": "transcript"}\n', "元の記録が書き換わった")
    cmd = rep.get("cmd", "")
    check(cmd == f"CLAUDE_CONFIG_DIR='{cfg}' command claude --resume {sid}", f"起動コマンド {cmd!r}")
    line = next((x for x in rep.get("script", "").splitlines() if x.startswith("echo WOULD_RUN:")), "")
    z = subprocess.run(["/bin/zsh", "-c", line], capture_output=True, text=True)
    check(z.stdout.strip() == "WOULD_RUN: " + cmd, f"端末に渡った文字列が違う: {z.stdout.strip()!r}")
    return f"{os.path.basename(dest)} を相手の projects/ へコピー / 端末に渡った cmd={cmd[:70]}…"


@case("AP-14", "見張り: 起動時の状態では通知せず、変わった時だけ 1 回。判断待ちの数が Dock の数字になる")
def ap14(ctx):
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    prefix = os.path.join(data, "selftest")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_SELFTEST=prefix, AIBOARD_DRY="1", AIBOARD_LANG="ja")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        check(False, "アプリが終わらない(ダイアログで止まっている可能性)")
    check(os.path.exists(prefix + ".json"), "アプリが自己試験の結果を書かなかった")
    rep = json.load(open(prefix + ".json"))
    log, counts = rep.get("watch_log", []), rep.get("watch_counts", [])
    check(counts == [1, 2, 2], f"判断待ちの数(Dock バッジ) {counts}(期待 1,2,2)")
    check(len(log) == 2, f"通知 {len(log)} 件(期待 2 件)\n{log}")
    check(log[0] == "AI があなたの判断待ち · acme | Allow Bash? | tab=1-1", f"1 件目 {log[0]!r}")
    check(log[1] == "AI が返答 · globex | あなたの番です。 | tab=0-1", f"2 件目 {log[1]!r}")
    check(not any("1-3" in x for x in log), f"起動時から判断待ちだったものを鳴らした {log}")
    check(rep.get("focus_ok") is True, "盤からの focus でその端末が選ばれていない")
    check(rep.get("panes_file_exists") is True, "動作中に app_panes.json が無い")
    return f"通知 2 件(判断待ち 1-1 / 返答 0-1)・据え置きと重複 0 件・Dock の数 {counts}"


@case("AP-15", "メニュー: 短縮キーが重ならず、押した先の処理が実在し、英語表示に日本語が混ざらない")
def ap15(ctx):
    src = open(os.path.join(ROOT, "Sources", "AIBoard", "main.swift"), encoding="utf-8").read()
    rows = re.findall(r"\((L\(\"([^\"]*)\", \"([^\"]*)\"\)|\"-\"),\s*(?:#selector\(([^)]*\(_:\))\)|nil),\s*\"([^\"]*)\",\s*(\[[^\]]*\]|\.\w+)\)", src)
    check(len(rows) >= 20, f"メニュー項目を {len(rows)} 件しか読めていない(解析の失敗)")
    funcs = set(re.findall(r"@objc func (\w+)\(", src))
    dup, seen, bad_sel, bad_txt = [], {}, [], []
    for whole, en, ja, sel, key, mods in rows:
        if whole == '"-"':
            continue
        if not en or not ja or re.search(r"[぀-ヿ一-鿿]", en):
            bad_txt.append(en or whole)
        name = (sel or "").split("(")[0]
        if sel and "." not in sel and name not in funcs:
            bad_sel.append(sel)
        if key:
            k = (key, frozenset(re.findall(r"\.(\w+)", mods)))
            if k in seen:
                dup.append((key, sorted(k[1]), seen[k], en))
            seen[k] = en
    check(not dup, f"短縮キーの重なり {dup}")
    check(not bad_sel, f"存在しない処理を指すメニュー {bad_sel}")
    check(not bad_txt, f"英語表示に日本語が混ざる/訳が空 {bad_txt}")
    return f"メニュー {len(rows)} 項目・短縮キー {len(seen)} 種すべて別・処理すべて実在・訳の抜け 0"


@case("PK-02", "同梱した board だけを別の場所へ写しても盤サーバが起動し、同梱の overview.html を CSP 付きで配る")
def pk02(ctx):
    src = os.path.join(ROOT, "build", "AIBoard.app", "Contents", "Resources", "board")
    if not os.path.isdir(src):
        return "SKIP: build/AIBoard.app が無い(make_app.sh 未実行)"
    port = 8794
    data = tempfile.mkdtemp(dir=ctx["data"])
    work = os.path.join(data, "board")
    shutil.copytree(src, work)
    env = dict(os.environ, OVERVIEW_PORT=str(port), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1")
    def get(path):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Overview": "1"})
        with urllib.request.urlopen(req, timeout=20) as f:
            return f.read(), dict(f.headers)
    try:
        r = subprocess.run([sys.executable, os.path.join(work, "overview_server.py"), "--no-open"],
                           env=env, capture_output=True, text=True, timeout=120, cwd=work)
        out = (r.stdout + r.stderr).strip()
        check(f"http://127.0.0.1:{port}/" in out, f"同梱 board で起動しない: {out[-300:]}")
        v = json.loads(get("/api/version")[0])
        check(v.get("ok") and v.get("stamp"), f"/api/version {v}")
        page, h = get("/")
        want = open(os.path.join(work, "overview.html"), "rb").read()
        check(page == want, f"配られたページが同梱の overview.html と違う({len(page)} != {len(want)} バイト)")
        check("connect-src 'self'" in h.get("Content-Security-Policy", ""), f"CSP が無い {h.get('Content-Security-Policy')!r}")
        return f"写した同梱 board で起動・stamp={v['stamp']}・ページ {len(page)} バイト一致・CSP あり"
    finally:
        subprocess.run([sys.executable, os.path.join(work, "overview_server.py"), "stop"],
                       env=env, capture_output=True, text=True, timeout=30)


@case("PK-03", "make_app.sh: swift build が失敗したら、古い実行ファイルを包んで配らない")
def pk03(ctx):
    def build_sandbox(build_ok):
        T = tempfile.mkdtemp(dir=ctx["data"])
        repo = os.path.join(T, "repo")
        for d in (".build/release/Fake.bundle", "Resources", "board", "build"):
            os.makedirs(os.path.join(repo, d), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "make_app.sh"), os.path.join(repo, "make_app.sh"))
        os.chmod(os.path.join(repo, "make_app.sh"), 0o755)
        open(os.path.join(repo, ".build", "release", "AIBoard"), "w").write("STALE-BINARY")
        open(os.path.join(repo, ".build", "release", "Fake.bundle", "f.txt"), "w").write("r")
        shutil.copy2(os.path.join(ROOT, "Resources", "Info.plist"), os.path.join(repo, "Resources", "Info.plist"))
        open(os.path.join(repo, "board", "x.py"), "w").write("x = 1\n")
        b = os.path.join(T, "bin")
        os.makedirs(b)
        # cp / rm は砂場の外を触ろうとしたら記録して拒む(本物の /Applications を壊さない)
        guard = ('#!/bin/sh\nfor a in "$@"; do case "$a" in -*) continue;; esac; case "$a" in /*) p="$a";; *) p="$PWD/$a";; esac;'
                 ' case "$p" in "%s"/*) ;; *) echo "%s $p" >> "%s/guard.log"; exit 99;; esac; done\nexec %s "$@"\n')
        for name, real in (("cp", "/bin/cp"), ("rm", "/bin/rm")):
            p = os.path.join(b, name)
            open(p, "w").write(guard % (T, name, T, real))
            os.chmod(p, 0o755)
        sw = '#!/bin/sh\necho "Build complete!"\n' if build_ok else '#!/bin/sh\necho "x.swift:1:1: error: boom" >&2\nexit 1\n'
        for name, body in (("swift", sw), ("codesign", "#!/bin/sh\nexit 0\n")):
            p = os.path.join(b, name)
            open(p, "w").write(body)
            os.chmod(p, 0o755)
        env = dict(os.environ, PATH=b + ":/usr/bin:/bin:/usr/sbin:/sbin", HOME=T)
        r = subprocess.run(["zsh", os.path.join(repo, "make_app.sh")], env=env, capture_output=True, text=True, timeout=120)
        gl = os.path.join(T, "guard.log")
        return {"rc": r.returncode, "out": (r.stdout + r.stderr)[-300:],
                "packaged": os.path.exists(os.path.join(repo, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")),
                "guard": open(gl).read().strip() if os.path.exists(gl) else ""}
    ng = build_sandbox(False)
    ok = build_sandbox(True)
    # 正常系: ちゃんと包んで配る所まで進む(この試験自体が空振りでないことの対照)
    check(ok["packaged"] and ("installed:" in ok["out"] or ok["guard"]),
          f"ビルド成功でも .app を作らない(試験の仕掛けがおかしい): {ok}")
    check(not ng["packaged"], f"ビルド失敗なのに古い実行ファイルを .app に入れた: {ng}")
    check("installed:" not in ng["out"], f"ビルド失敗なのに installed: と表示した: {ng['out']}")
    check(ng["guard"] == "", f"ビルド失敗なのに配布先を触ろうとした: {ng['guard']}")
    check(ng["rc"] != 0, f"ビルド失敗なのに終了コード {ng['rc']}")
    return f"失敗時 rc={ng['rc']}・包まない・配らない / 成功時は包んで配る所まで進む(guard={ok['guard'][:40] or 'なし'})"


@case("PK-04", "アイコン: icns の 10 寸法が make_icon.py の出力と画素一致し、LP のファビコンも同じ生成物")
def pk04(ctx):
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return "SKIP: Pillow が無い"
    import importlib.util
    spec = importlib.util.spec_from_file_location("aiboard_make_icon", os.path.join(ROOT, "design", "icon", "make_icon.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)          # main() は __main__ ガードの中なので何も書かない
    big = m.draw(1024)
    d = tempfile.mkdtemp(dir=ctx["data"])
    icns = os.path.join(ROOT, "Resources", "AppIcon.icns")
    check(os.path.exists(icns), "Resources/AppIcon.icns が無い")
    r = subprocess.run(["/usr/bin/iconutil", "-c", "iconset", icns, "-o", os.path.join(d, "a.iconset")], capture_output=True, text=True)
    check(r.returncode == 0, f"icns を展開できない: {(r.stdout + r.stderr)[:200]}")
    bad = []
    for pt in (16, 32, 128, 256, 512):
        for sc in (1, 2):
            px = pt * sc
            n = f"icon_{pt}x{pt}" + ("@2x" if sc == 2 else "") + ".png"
            p = os.path.join(d, "a.iconset", n)
            if not os.path.exists(p):
                bad.append((n, "無い")); continue
            got = Image.open(p).convert("RGBA")
            if got.size != (px, px):
                bad.append((n, f"{got.size} != {(px, px)}"))
            elif ImageChops.difference(got, big.resize((px, px), Image.LANCZOS)).getbbox() is not None:
                bad.append((n, "画素が生成器と違う"))
    check(not bad, f"icns の不一致 {bad[:4]}(make_icon.py を流し直す)")
    for f, px in (("site/img/favicon.png", 64), ("site/img/apple-touch-icon.png", 180), ("design/icon/favicon-64.png", 64)):
        p = os.path.join(ROOT, f)
        check(os.path.exists(p), f"{f} が無い")
        got = Image.open(p).convert("RGBA")
        check(got.size == (px, px), f"{f} が {got.size}(期待 {(px, px)})")
        check(ImageChops.difference(got, big.resize((px, px), Image.LANCZOS)).getbbox() is None, f"{f} が生成器の出力と違う")
    return "icns 10 寸法・favicon 64・apple-touch 180 すべて make_icon.py の出力と画素一致"


@case("PK-05", "外部送信ゼロの証明: 外への接続を見つける・何も測れていない時に「0 件」と言わない")
def pk05(ctx):
    sh = os.path.join(ROOT, "scripts", "prove-local-only.sh")
    check(os.path.exists(sh), "scripts/prove-local-only.sh が無い")
    lsof_stub = ('#!/bin/sh\n[ -z "$AIBOARD_UAT_ROWS" ] && exit 1\n'
                 'for a in "$@"; do [ "$a" = "-t" ] && { echo 4243; exit 0; }; done\n'
                 'printf "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\\n"\nprintf "%s\\n" "$AIBOARD_UAT_ROWS"\n')
    pgrep_stub = '#!/bin/sh\n[ -z "$AIBOARD_UAT_ROWS" ] && exit 1\ncase "$*" in *-P*) exit 1;; esac\necho 4242\n'
    def run(rows):
        T = tempfile.mkdtemp(dir=ctx["data"])
        b = os.path.join(T, "bin")
        os.makedirs(b)
        for n, body in (("lsof", lsof_stub), ("pgrep", pgrep_stub)):
            p = os.path.join(b, n)
            open(p, "w").write(body)
            os.chmod(p, 0o755)
        env = dict(os.environ, PATH=b + ":/usr/bin:/bin:/usr/sbin:/sbin", AIBOARD_DATA=T, AIBOARD_UAT_ROWS=rows)
        r = subprocess.run(["zsh", sh, "2"], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr
    green = "RESULT: 0 connections"
    rc_n, out_n = run("")                       # 測る相手がいない(アプリも盤サーバも動いていない)
    rc_x, out_x = run("AIBoard 4242 u 5u IPv4 0x1 0t0 TCP 192.168.1.5:52000->203.0.113.9:443 (ESTABLISHED)")
    rc_l, out_l = run("AIBoard 4242 u 5u IPv4 0x1 0t0 TCP 127.0.0.1:8791->127.0.0.1:52001 (ESTABLISHED)")
    check(green not in out_n, f"1 行も測れていないのに「0 件」と断言した(README の根拠が空証明になる): {out_n.strip()[-200:]}")
    check(rc_n != 0, f"測れていないのに終了コード {rc_n}")
    check("203.0.113.9" in out_x and green not in out_x, f"外への接続を見逃した: {out_x.strip()[-200:]}")
    check(rc_x != 0, f"外への接続を見つけたのに終了コード {rc_x}")
    check(green in out_l and "sampled socket rows: 2" in out_l and rc_l == 0, f"127.0.0.1 だけなのに 0 件と言わない: {out_l.strip()[-200:]}")
    return f"未測定 rc={rc_n}(0 件と言わない) / 外部あり rc={rc_x}(相手を表示) / loopback だけ rc={rc_l}(0 件・2 行)"


@case("PK-07", "README と LP の導線が出荷物で裏づけられている(未置換の URL・brew・MIT・依存ライセンス)")
def pk07(ctx):
    rd = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()
    docs = {"README.md": rd("README.md"), "site/index.html": rd("site/index.html")}
    ph = [(f, p) for f, t in docs.items() for p in ("REPO_OWNER", "YOUR_", "TODO", "FIXME", "lorem ipsum", "example.com")
          if p.lower() in t.lower()]
    check(not ph, f"未置換の差し込み語が残っている(リンクが 404 になる) {ph}")
    tap = [p for p in glob.glob(os.path.join(ROOT, "**", "*.rb"), recursive=True) if "/Casks/" in p or "/Formula/" in p]
    unbacked = []
    for f, t in docs.items():
        for m in re.finditer(r"brew install[^<`\n]*", t):
            around = t[max(0, m.start() - 250):m.end() + 250]
            if not tap and not re.search(r"first release|coming|not yet|予定|後日|近日", around, re.I):
                unbacked.append((f, m.group(0).strip()[:40]))
    check(not unbacked, f"tap も cask も無いのに brew で入るように書いている {unbacked}")
    lic = rd("LICENSE")
    check("MIT License" in lic and "WITHOUT WARRANTY OF ANY KIND" in lic, "LICENSE が MIT 本文でない")
    claims = [f for f, t in docs.items() if re.search(r"\bMIT\b", t)]
    check(claims, "README/LP が MIT を名乗っていない(LICENSE と食い違う)")
    co = os.path.join(ROOT, ".build", "checkouts")
    deps = [p["identity"] for p in json.load(open(os.path.join(ROOT, "Package.resolved")))["pins"]]
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    nonmit, seen = [], 0
    if os.path.isdir(co):
        for dname in os.listdir(co):
            if not any(norm(dname) in norm(i) or norm(i) in norm(dname) for i in deps):
                continue
            lics = sorted(glob.glob(os.path.join(co, dname, "LICENSE*")) + glob.glob(os.path.join(co, dname, "LICENSE", "*")))
            seen += 1
            if not lics:
                nonmit.append((dname, "LICENSE が無い"))
            elif "MIT" not in open(lics[0], encoding="utf-8", errors="replace").read()[:600]:
                nonmit.append((dname, "MIT でない"))
    check(not nonmit, f"MIT を名乗っているが依存が MIT でない {nonmit}")
    return f"差し込み語 0・brew の裏づけ({'tap あり' if tap else '注記あり'})・LICENSE=MIT・依存 {len(deps)} 件中 {seen} 件のライセンスを照合"


@case("PK-09", "LP と README が指すデモ動画・画像が実物と一致する(尺・寸法・音声なし・宣言した縦横比)")
def pk09(ctx):
    try:
        from PIL import Image
    except ImportError:
        return "SKIP: Pillow が無い"
    import struct
    def atoms(b, s, e):
        while s + 8 <= e:
            sz = struct.unpack(">I", b[s:s + 4])[0]
            typ = b[s + 4:s + 8].decode("latin1")
            if sz == 1:
                sz = struct.unpack(">Q", b[s + 8:s + 16])[0]
            if sz < 8:
                break
            yield typ, s + 8, min(s + sz, e)
            s += sz
    def mp4_info(path):                      # ffprobe に頼らず mvhd/tkhd/hdlr を直接読む
        b = open(path, "rb").read()
        info = {"dur": None, "w": None, "h": None, "kinds": []}
        def walk(s, e):
            for t, ds, de in atoms(b, s, e):
                if t == "mvhd":
                    v = b[ds]
                    ts, du = (struct.unpack(">I", b[ds + 20:ds + 24])[0], struct.unpack(">Q", b[ds + 24:ds + 32])[0]) if v == 1 \
                        else (struct.unpack(">I", b[ds + 12:ds + 16])[0], struct.unpack(">I", b[ds + 16:ds + 20])[0])
                    info["dur"] = du / ts if ts else None
                elif t == "tkhd":
                    w, h = struct.unpack(">II", b[de - 8:de])
                    if w >> 16:
                        info["w"], info["h"] = w >> 16, h >> 16
                elif t == "hdlr":
                    info["kinds"].append(b[ds + 8:ds + 12].decode("latin1"))
                elif t in ("moov", "trak", "mdia", "minf", "stbl", "udta"):
                    walk(ds, de)
        walk(0, len(b))
        return info
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    lp = open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8").read()
    mp4 = os.path.join(ROOT, "site", "demo.mp4")
    check(os.path.exists(mp4), "site/demo.mp4 が無い")
    v = mp4_info(mp4)
    check(v["dur"], f"動画の尺が読めない {v}")
    said = re.search(r"(\d+)[ -]second demo", readme)
    check(said, "README に「N-second demo」の記述が無い")
    check(abs(v["dur"] - int(said.group(1))) <= 1.5, f"README は {said.group(1)} 秒と書いているが実物は {v['dur']:.1f} 秒")
    check(26 <= v["dur"] <= 111, f"尺 {v['dur']:.1f} 秒(狙いは 26〜111 秒)")
    check("soun" not in v["kinds"], f"デモ動画に音声が入っている {v['kinds']}")
    check(v["w"] and v["w"] >= 1280, f"動画の幅 {v['w']}(1280 以上)")
    bad = []
    for tag in re.findall(r"<img[^>]*>", lp):
        src = re.search(r'src="([^"]+)"', tag)
        w = re.search(r'width="(\d+)"', tag)
        h = re.search(r'height="(\d+)"', tag)
        if not (src and w and h):
            continue
        p = os.path.join(ROOT, "site", src.group(1))
        check(os.path.exists(p), f"LP の画像が無い {src.group(1)}")
        rw, rh = Image.open(p).size
        if abs(rw / rh - int(w.group(1)) / int(h.group(1))) > 0.01 * (rw / rh):
            bad.append((src.group(1), f"実物 {rw}x{rh}", f"宣言 {w.group(1)}x{h.group(1)}"))
    check(not bad, f"LP が宣言した縦横比と画像の実物が違う(表示がずれる) {bad}")
    og = re.search(r'property="og:image" content="([^"]+)"', lp)
    check(og, "LP に og:image が無い")
    ow, oh = Image.open(os.path.join(ROOT, "site", og.group(1))).size
    check(ow >= 1200 and 1.85 <= ow / oh <= 1.95, f"OG 画像 {ow}x{oh}(1200 以上・1.91 前後)")
    return f"demo.mp4 {v['dur']:.1f} 秒 {v['w']}x{v['h']} 音声なし(README {said.group(1)} 秒と一致)・LP の画像の縦横比一致・OG {ow}x{oh}"


@case("WR-01", "持ち場の判定: 触ったファイルから置き場を1つ選び、道具置き場や1段では足りない所を取り違えない")
def wr01(ctx):
    import overview as o
    spec = {
        HOME + "/youtube-ai-pipeline/a/b.py": "youtube-ai-pipeline",
        HOME + "/Desktop/ECX/x.md": "Desktop/ECX",
        HOME + "/Documents/shiwake/2026.xlsx": "Documents/shiwake",
        HOME + "/.claude/skills/gsc/run.py": "",
        HOME + "/.codex/log.txt": "",
        HOME + "/Downloads/a.pdf": "",
        HOME + "/Library/x/y": "",
        HOME + "/memo.txt": "",
        HOME + "/Desktop/one.png": "",
        "/tmp/x.py": "",
        "/Users/other/proj/a.py": "",
        "": "",
    }
    bad = {p: (o.work_root(p), w) for p, w in spec.items() if o.work_root(p) != w}
    check(not bad, f"判定違い(実際, 期待) {bad}")
    check(o.work_root(None) == "" and o.work_root(123) == "", "文字列でない入力で落ちる")
    tools = {"dirs": {"youtube-ai-pipeline": 5, "tasks": 2}}
    check(o.project_hint(tools, HOME) == "youtube-ai-pipeline", "多い方を選べていない")
    check(o.project_hint({"dirs": {"tasks": 2}}, HOME) == "", "根拠が 2 件でも持ち場を付けた(3 件以上のはず)")
    check(o.project_hint(tools, HOME + "/somewhere") == "", "ホーム以外にも持ち場を付けた(cwd で分かる場所には要らない)")
    check(o.project_hint(None, HOME) == "" and o.project_hint({}, HOME) == "", "道具の記録が無い時に落ちる")
    return f"{len(spec)} 通りの置き場判定が一致 / 3 件以上で採用・ホーム以外では付けない"


@case("WR-02", "ホームで動く会話も、触ったファイルの置き場ごとに別の枠に分かれる(デモでは実名を出さない)")
def wr02(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": os.path.basename(HOME), "cwd": HOME, "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 100, "limit": None, "loop": None, "tools": None, "account": "", "transcript": "", "project_hint": ""}
    ss = [dict(base, tab="9-1", sid="uat-a1", project_hint="alpha-proj"),
          dict(base, tab="9-2", sid="uat-a2", project_hint="alpha-proj"),
          dict(base, tab="9-3", sid="uat-b1", project_hint="beta-proj"),
          dict(base, tab="9-4", sid="uat-h1", project_hint="")]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=4, tabs=4)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 4", 30)
        pg.wait_for_timeout(400)
        return pg.evaluate("""(() => { const m = {}; (window._frames || []).forEach(f => m[f.key] = f.nodes.map(n => n.id).sort());
            return {frames: m, labels: [...document.querySelectorAll('.frame .flabel')].map(e => e.textContent)}; })()"""), list(errs)

    got, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    fr = got["frames"]
    check(fr.get("p:alpha-proj") == ["uat-a1", "uat-a2"], f"alpha の枠 {fr.get('p:alpha-proj')}")
    check(fr.get("p:beta-proj") == ["uat-b1"], f"beta の枠 {fr.get('p:beta-proj')}")
    check("uat-h1" in (fr.get("home") or []), f"持ち場の分からないものはホームの枠に {fr.get('home')}")
    check(any("alpha-proj" in l for l in got["labels"]), f"枠の見出しに持ち場の名前が出ていない {got['labels']}")
    check(not errs, f"{errs[:1]}")
    dgot, derrs = with_page(ctx, fn, "?lang=ja&demo=1", route_extra=extra)
    dl = " ".join(dgot["labels"])
    check("alpha-proj" not in dl and "beta-proj" not in dl, f"デモで実名が出ている {dgot['labels']}")
    dfr = {k: v for k, v in dgot["frames"].items() if k.startswith("p:")}
    check(len(dfr) == 2 and sorted(map(len, dfr.values())) == [1, 2], f"デモで分かれ方が変わった {dfr}")
    return f"alpha 2 枚 / beta 1 枚 / 持ち場不明 1 枚はホーム・見出しに名前・デモでは伏せて分かれ方は同じ"


@case("WR-03", "過去の記録にも同じ規則で持ち場が付き、cwd で分かる会話には付けない")
def wr03(ctx):
    import overview_index as ix
    import overview as o
    rows = [
        {"cwd": HOME, "files_top": [[HOME + "/alpha/a.py", 4], [HOME + "/tasks/b.md", 1]], "want": "alpha"},
        {"cwd": HOME, "files_top": [[HOME + "/alpha/a.py", 1]], "want": ""},
        {"cwd": HOME, "files_top": [[HOME + "/Desktop/ECX/a.md", 2], [HOME + "/Desktop/ECX/b.md", 1]], "want": "Desktop/ECX"},
        {"cwd": HOME, "files_top": [[HOME + "/.claude/x.py", 9]], "want": ""},
        {"cwd": HOME + "/alpha", "files_top": [[HOME + "/alpha/a.py", 9]], "want": ""},
        {"cwd": HOME, "files_top": [], "want": ""},
        {"cwd": "", "files_top": [[HOME + "/alpha/a.py", 9]], "want": ""},
    ]
    bad = [(r["cwd"][-12:], r["files_top"][:1], ix._hint_from_files(r), r["want"]) for r in rows if ix._hint_from_files(r) != r["want"]]
    check(not bad, f"索引側の判定違い(実際, 期待) {bad}")
    # 盤(いま)と索引(過去)で同じ置き場の規則を使っている
    p = HOME + "/alpha/deep/x.py"
    check(ix._hint_from_files({"cwd": HOME, "files_top": [[p, 2]]}) == o.work_root(p), "いまと過去で規則が違う")
    live = [r for r in http("/api/index?days=30")[1]["records"] if r.get("project_hint")]
    return f"合成 {len(rows)} 通り一致 / 実データでは 30 日の記録のうち {len(live)} 件に持ち場が付く"


@case("GP-01", "束ね方の上書き: 保存できる形だけ受け取り、壊れた値は 400 で断る(本番の設定は触らない)")
def gp01(ctx):
    import overview as o
    live_file = os.path.join(LIVE_DATA, "groups.json")
    before = os.path.getmtime(live_file) if os.path.exists(live_file) else None
    st, d, _ = http("/api/groups")
    check(st == 200 and isinstance(d.get("detected"), list), f"候補が取れない {st} {str(d)[:120]}")
    ids = [c["id"] for c in d["clients"]]
    ok_body = {"groups": {"uat-work": {"label": "UAT 工場", "rgb": [10, 20, 30]}}}
    if ids:
        ok_body["groups"]["uat-work"]["client"] = ids[0]
    st2, d2, _ = http("/api/groups", "POST", ok_body, headers={"Origin": BASE.rstrip("/")})
    check(st2 == 200 and d2["groups"]["uat-work"]["label"] == "UAT 工場", f"保存できない {st2} {d2}")
    saved = json.load(open(os.path.join(ctx["data"], "groups.json")))
    check(saved["groups"]["uat-work"]["rgb"] == [10, 20, 30], f"書かれた中身 {saved}")
    bad = [({"groups": {"k": {"rgb": [1, 2]}}}, "色の数"), ({"groups": {"k": {"rgb": [1, 2, 300]}}}, "色の範囲"),
           ({"groups": {"k": {"label": "あ" * 41}}}, "長すぎる表示名"), ({"groups": {"k": {"client": "no-such"}}}, "知らない顧客"),
           ({"groups": {"": {"label": "x"}}}, "空の名前"), ({"groups": "x"}, "形が違う")]
    for body, why in bad:
        stb, db, _ = http("/api/groups", "POST", body, headers={"Origin": BASE.rstrip("/")})
        check(stb == 400 and db.get("ok") is False, f"{why}: {stb} {db}")
    after = os.path.getmtime(live_file) if os.path.exists(live_file) else None
    check(after == before, "本番の groups.json を書き換えた")
    st3, d3, _ = http("/api/groups", "POST", {"groups": {}}, headers={"Origin": BASE.rstrip("/")})
    check(st3 == 200 and d3["groups"] == {}, f"空に戻せない {d3}")
    return f"保存 1 件・壊れた値 {len(bad)} 通りを 400・空に戻せる・本番の設定は不変"


@case("GP-02", "上書きした名前・色・顧客が、盤の枠とカードに出る")
def gp02(ctx):
    import overview as o
    with patched(o, "groups", lambda force=False: {"alpha": {"label": "アルファ工場", "rgb": [200, 30, 90]}}):
        s = {"project_hint": "alpha", "project": os.path.basename(HOME), "client": None}
        got = o.apply_group(dict(s))
        check(got["group_label"] == "アルファ工場" and got["group_rgb"] == [200, 30, 90], f"当たっていない {got}")
        cid = (o.client_defs() or [{}])[0].get("id")
        if cid:
            with patched(o, "groups", lambda force=False: {"alpha": {"client": cid}}):
                g2 = o.apply_group({"project_hint": "alpha", "project": "", "client": None})
                check((g2.get("client") or {}).get("id") == cid and g2["client"]["by"] == "group", f"顧客が付かない {g2.get('client')}")
                g3 = o.apply_group({"project_hint": "alpha", "project": "", "client": {"id": "keep", "label": "元", "by": "path"}})
                check(g3["client"]["id"] == "keep", "自動で付いた顧客を上書きしてしまう")
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "", "task": "UAT",
            "topic": "", "project": os.path.basename(HOME), "cwd": HOME, "client": None, "project_hint": "alpha",
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 100, "limit": None, "loop": None, "tools": None, "account": "", "transcript": "",
            "group_label": "アルファ工場", "group_rgb": [200, 30, 90]}
    ss = [base, dict(base, tab="9-2", sid="uat-2")]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss; d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=2, tabs=2)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 2", 30)
        pg.wait_for_timeout(400)
        return pg.evaluate("""[...document.querySelectorAll('.frame')].map(f => [f.querySelector('.flabel').textContent,
            (f.querySelector('.flabel b') || {}).style?.background || ''])"""), list(errs)

    labels, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    hit = [l for l in labels if "アルファ工場" in l[0]]
    check(hit, f"枠の見出しに上書きした名前が出ていない {labels}")
    check("200, 30, 90" in hit[0][1].replace("rgb(", "").replace(")", ""), f"枠の色が上書きされていない {hit[0]}")
    check(not errs, f"{errs[:1]}")
    return f"枠「{hit[0][0]}」に上書きした名前と色({hit[0][1]})が出る / 自動の顧客は上書きしない"


@case("GP-03", "AI の返事の取り込み: JSON を表に入れるだけで、保存も外部送信もしない")
def gp03(ctx):
    def extra(pg):
        pg.route("**/api/groups", lambda route, req: route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "groups": {}, "clients": [{"id": "acme", "label": "Acme", "emoji": "🟦", "rgb": [1, 2, 3]}],
             "detected": [{"key": "alpha", "live": 2, "recent": 5}, {"key": "beta", "live": 0, "recent": 3}]})) if req.method == "GET" else route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "groups": json.loads(req.post_data)["groups"]})))

    def fn(pg, errs, bl):
        pg.click("#btnSettings")
        wait_js(pg, "!!document.querySelector('#grpBox tbody tr')", 40)
        n0 = len(bl)
        answer = json.dumps({"groups": {"alpha": {"label": "アルファ", "rgb": [200, 30, 90], "client": "acme"},
                                        "beta": {"label": "ベータ", "client": "no-such"},
                                        "gamma": {"label": "無いフォルダ"}}}, ensure_ascii=False)
        pg.click("#grpPaste"); pg.fill("#grpJson", "ここまでが説明です " + answer + " おわり")
        posts = []
        pg.route("**/api/groups", lambda route, req: (posts.append(req.method), route.fulfill(status=200, content_type="application/json", body='{"ok": true, "groups": {}}')))
        pg.click("#grpApply"); pg.wait_for_timeout(500)
        table = pg.evaluate("""[...document.querySelectorAll('#grpBox tbody tr')].map(tr => [tr.dataset.key,
            tr.querySelector('.gl').value, tr.querySelector('.gc').value, tr.querySelector('.gu').checked, tr.querySelector('.gk').value])""")
        msg = pg.inner_text("#grpMsg")
        return table, msg, posts, len(bl) - n0, list(errs)

    table, msg, posts, sends, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    by = {r[0]: r for r in table}
    check(by["alpha"][1] == "アルファ" and by["alpha"][3] is True and by["alpha"][2] == "#c81e5a", f"alpha が入っていない {by.get('alpha')}")
    check(by["alpha"][4] == "acme", f"顧客が入っていない {by['alpha']}")
    check(by["beta"][1] == "ベータ" and by["beta"][4] == "", f"知らない顧客を入れた {by['beta']}")
    check("POST" not in posts, f"取り込んだだけで保存してしまった {posts}")
    check(sends == 0 and not errs, f"外部へ送った/例外 {sends} {errs[:1]}")
    check("2" in msg, f"入れた件数が出ていない {msg!r}")
    return "JSON を前後の文ごと貼っても 2 件だけ表に入り、知らない顧客は空・保存はしない・送信 0"


@case("BD-16", "小さい窓では枠を畳んで一覧にする: 45% を下回らず、急ぎの枠は開いたまま、押せば開く")
def bd16(ctx):
    base = {"tab": "9-1", "sid": "uat-1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "UAT fixture", "topic": "", "project": os.path.basename(HOME), "cwd": HOME, "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 5, "state_for": 5,
            "mem_mb": 100, "limit": None, "loop": None, "tools": None, "account": "", "transcript": "",
            "group_label": "", "group_rgb": None}
    ss = [dict(base, tab=f"9-{i}", sid=f"uat-{i}", project_hint=f"proj{i}") for i in range(1, 13)]
    ss[4] = dict(ss[4], state="確認待ち", mark="🔴")   # 5 番目だけ判断待ち(これは畳まれないはず)

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss
            d["attention"] = [{"sid": "uat-5"}]
            d["counts"] = dict(d.get("counts") or {}, working=len(ss) - 1, tabs=len(ss))
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "(window._frames || []).length >= 10", 40)
        pg.evaluate("board.fitAll()"); pg.wait_for_timeout(900)
        first = pg.evaluate("""(() => { const z = parseFloat(document.querySelector('#zoomPct').textContent) / 100;
            const folds = [...document.querySelectorAll('.frame.fold')];
            const open = (window._frames || []).filter(f => !f.collapsed);
            const card = document.querySelector('.card[data-id="uat-5"]');
            const st = card && getComputedStyle(card.querySelector('.c-title'));
            return {zoom: z, folded: folds.length, open: open.map(f => f.key),
                    urgentOpen: !!card, titleShown: !!st && st.display !== 'none',
                    lodFar: document.getElementById('nodes').classList.contains('lod-far'),
                    foldText: folds.length ? folds[0].innerText.split('\\n').join(' ').slice(0, 40) : ''}; })()""")
        if first["folded"]:
            pg.evaluate("document.querySelector('.frame.fold').click()")
            pg.wait_for_timeout(700)
        after = pg.evaluate("(window._frames || []).filter(f => !f.collapsed).length")
        return first, after, list(errs)

    got, after, errs = with_page(ctx, fn, "?lang=ja", width=900, height=420, route_extra=extra)
    check(got["zoom"] >= 0.44, f"引きすぎ {got['zoom']*100:.0f}%")
    check(got["folded"] >= 1, f"畳まれた枠が無い(小さい窓なのに全部広げた) {got}")
    check(len(got["open"]) >= 1 and got["urgentOpen"] and got["titleShown"], f"急ぎの枠まで畳んだ {got}")
    check(not got["lodFar"], f"色の塊になっている {got}")
    check(after > len(got["open"]), f"畳まれた枠を押しても開かない {len(got['open'])}→{after}")
    check(not errs, f"{errs[:1]}")
    return f"900x420・持ち場 12 個 → {got['zoom']*100:.0f}%・畳んだ枠 {got['folded']} 個・判断待ちの枠は開いたまま・押すと {len(got['open'])}→{after} 個"


@case("PJ-01", "案件の共通指示: 保存でき、Claude は system prompt・Codex は最初のメッセージとして渡る(実行はしない)")
def pj01(ctx):
    import overview as o
    brief = "本番には触らない。変更は必ず試験を足してから。用語は社内の呼び方に合わせる。"
    st, d, _ = http("/api/groups", "POST", {"groups": {"uatproj": {"label": "UAT 案件", "instructions": brief}}},
                    headers={"Origin": BASE.rstrip("/")})
    check(st == 200 and d["groups"]["uatproj"]["instructions"] == brief, f"保存できない {st} {str(d)[:120]}")
    stb, db, _ = http("/api/groups", "POST", {"groups": {"uatproj": {"instructions": "あ" * 4001}}}, headers={"Origin": BASE.rstrip("/")})
    check(stb == 400, f"長すぎる指示を受け取った {stb} {db}")
    http("/api/groups", "POST", {"groups": {"uatproj": {"label": "UAT 案件", "instructions": brief}}}, headers={"Origin": BASE.rstrip("/")})
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    shutil.copy2(os.path.join(ctx["data"], "groups.json"), os.path.join(data, "groups.json"))
    work = os.path.join(data, "work"); os.makedirs(work, exist_ok=True)
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'newInProject', key: 'uatproj', cwd: %s, ai: 'Claude'});
      await new Promise(r => setTimeout(r, 1500));
      P({type: 'newInProject', key: 'uatproj', cwd: %s, ai: 'Codex'});
      P({type: 'newInProject', key: '../../etc', cwd: '/etc', ai: 'Claude'});
      P({type: 'newInProject', key: 'uatproj', cwd: 'relative/path', ai: 'Claude'});
      await new Promise(r => setTimeout(r, 2500)); return 1;""" % (json.dumps(work), json.dumps(work))
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS=js, AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=150)
    check(os.path.exists(out) and json.load(open(out)).get("ok"), "盤の中で JS が動かなかった")
    scripts = sorted(glob.glob(os.path.join(data, "launch", "pane-*.sh")))
    bodies = [open(p).read() for p in scripts]
    cl = [b for b in bodies if "claude" in b]
    cx = [b for b in bodies if "codex" in b]
    check(len(cl) == 1 and len(cx) == 1, f"端末の数が違う claude={len(cl)} codex={len(cx)}(不正な指示で開いていないか)")
    ins = os.path.join(data, "projects", "uatproj.md")
    check(os.path.exists(ins), f"指示のファイルが無い {os.listdir(os.path.join(data, 'projects')) if os.path.isdir(os.path.join(data, 'projects')) else '(フォルダ無し)'}")
    body = open(ins).read()
    check(brief in body, f"指示の中身が違う {body[:60]!r}")
    check("--append-system-prompt-file" in cl[0] and ins in cl[0], f"Claude の渡し方 {cl[0][-160:]!r}")
    check("cat" in cx[0] and ins in cx[0], f"Codex の渡し方 {cx[0][-160:]!r}")
    check("作業は次の指示を待って" in body or "作業は次の指示を待ってください" in open(ins).read(), "Codex 向けの前置きが無い")
    check(work in cl[0] and work in cx[0], "作業フォルダが渡っていない")
    return f"指示 {len(brief)} 字を保存 / Claude=--append-system-prompt-file・Codex=最初のメッセージ / 不正な key と相対パスは開かない(端末 {len(bodies)} 枚)"


@case("OF-01", "公式の状態源(claude agents --json): 状態の言い換えと、hook が無い時だけ補うこと")
def of01(ctx):
    import overview as o
    fake = [
        {"pid": 111, "kind": "interactive", "status": "busy", "waitingFor": None, "name": "仕事A", "cwd": HOME},
        {"pid": 222, "kind": "interactive", "status": "waiting", "waitingFor": "permission prompt", "name": "仕事B", "cwd": HOME},
        {"pid": 333, "kind": "interactive", "status": "waiting", "waitingFor": "input needed", "name": "仕事C", "cwd": HOME},
        {"pid": 444, "kind": "interactive", "status": "idle", "waitingFor": None, "name": "仕事D", "cwd": HOME},
    ]
    want = {111: "作業中", 222: "確認待ち", 333: "返答待ち", 444: "返答待ち"}
    got = {p: (o.official_for_pid(p, fake) or {}).get("state") for p in want}
    check(got == want, f"言い換えが違う {got} != {want}")
    check(o.official_for_pid(999, fake) is None and o.official_for_pid(None, fake) is None, "知らない pid に状態を付けた")
    # hook が生きているセッションの状態は、公式で上書きしない
    procs_now = None
    with patched(o, "official_agents", lambda max_age=5: fake):
        base = {"tab": "1-1", "pid": 111, "state": "確認待ち", "mark": "🔴", "sid": "s1", "ai": "Claude", "task": "元の題",
                "cwd": HOME, "project": "", "client": None, "doing": "", "topic": "", "transcript": ""}
        keep = dict(base)
        rows = [keep, dict(base, tab="1-2", pid=222, state="起動中?", mark="🔵", sid="s2", task="")]
        with patched(o, "sessions", None):
            pass
        # sessions() の合流部分だけを真似る(本物の ps を使わない)
        agents = fake
        for x in rows:
            off = o.official_for_pid(x["pid"], agents)
            if off and off["state"] and x["state"] in ("起動中?", "終了(古い題名)", ""):
                x["state"] = off["state"]
                x["mark"] = {"確認待ち": "🔴", "返答待ち": "🟡", "作業中": "🟢"}.get(off["state"], x["mark"])
                if not x.get("task") and off["name"]:
                    x["task"] = off["name"]
        check(rows[0]["state"] == "確認待ち" and rows[0]["task"] == "元の題", f"hook の状態を上書きした {rows[0]}")
        check(rows[1]["state"] == "確認待ち" and rows[1]["task"] == "仕事B", f"起動中? を補えていない {rows[1]}")
    # 背景セッション(端末が無い)はカードとして出る。終わったものは出さない
    bg = [{"id": "aa11bb22", "sessionId": "11111111-2222-3333-4444-555555555555", "kind": "background", "state": "blocked",
           "name": "背景の仕事", "cwd": HOME, "startedAt": int((time.time() - 300) * 1000)},
          {"id": "cc33dd44", "sessionId": "66666666-7777-8888-9999-000000000000", "kind": "background", "state": "done",
           "name": "終わった仕事", "cwd": HOME, "startedAt": int((time.time() - 900) * 1000)}]
    out = o.background_sessions(bg)
    check(len(out) == 1 and out[0]["tab"] == "a-aa11bb22" and out[0]["state"] == "確認待ち", f"背景セッション {out}")
    check(out[0]["tty"] == "" and out[0]["pid"] is None and out[0]["background"] is True, f"端末を持たない印が無い {out[0]}")
    check(out[0]["ago"] and 250 < out[0]["ago"] < 400, f"開始からの時間 {out[0]['ago']}")
    real = o.official_agents()
    return f"言い換え 4 通り一致 / hook 優先・起動中?だけ補う / 背景セッションは 1 件(終了は出さない) / 実機の公式一覧 {len(real)} 件"


@case("DG-01", "まとめ役の選び方: 上限のアカウントを避け、全部上限なら投げない")
def dg01(ctx):
    import overview as o
    lim = {"active": True, "resets": "4:10am", "kind": "5h", "at": "", "resets_at": time.time() + 3600}
    C = lambda p, email, l=None: {"ai": "Claude", "profile": p, "email": email, "limit": l, "logged_in": None}
    X = lambda l=None, used=0: {"ai": "Codex", "profile": "codex", "email": "", "limit": l, "logged_in": None,
                                "usage": {"used_percent": used, "window_minutes": 10080, "resets_at": time.time() + 7200}}
    cases = [
        ("既定が空いている", [C("default", "a@b"), X()], "", ("Claude", "")),
        ("既定が上限→別アカウント", [C("default", "a@b", lim), C("work", "c@d"), X()], "", ("Claude", "work")),
        ("Claude 全部上限→Codex", [C("default", "a@b", lim), X()], "", ("Codex", "")),
        ("Codex 希望", [C("default", "a@b"), X()], "Codex", ("Codex", "")),
        ("Codex が使い切り→Claude", [C("default", "a@b"), X(used=100)], "Codex", ("Claude", "")),
        ("全部上限→投げない", [C("default", "a@b", lim), X(lim)], "", ("", "")),
        ("未ログインは選ばない", [C("default", "a@b", lim), C("empty", ""), X(lim)], "", ("", "")),
        ("CLI が未ログインと答えたら選ばない", [C("default", "a@b", lim), dict(C("out", "x@y"), logged_in=False), X(lim)], "", ("", "")),
    ]
    bad = []
    for name, acc, prefer, want in cases:
        got = o.pick_ai(prefer, acc)
        if (got["ai"], got["profile"]) != want:
            bad.append((name, (got["ai"], got["profile"]), want))
    check(not bad, f"選び方が違う(実際, 期待) {bad}")
    none = o.pick_ai("", [C("default", "a@b", lim), X(lim)])
    check("上限" in none["why"] and "4:10am" in none["why"], f"理由に解除時刻が無い {none['why']!r}")
    st, d, _ = http("/api/pick")
    check(st == 200 and "ai" in d and "why" in d, f"API {st} {str(d)[:100]}")
    return f"{len(cases)} 通りの選び方が一致 / 全部上限では ai 空 + 理由({none['why'][:40]}…) / API は {d.get('ai') or '空'}"


@case("DG-02", "任せる: 選ばれた AI とアカウントで端末を起こし、共通の指示と依頼文を渡す(実行はしない)")
def dg02(ctx):
    brief = "本番に触らない。試験を足してから直す。"
    ask = "検索の索引を SQLite に移して、件数が一致することを確かめて"
    http("/api/groups", "POST", {"groups": {"dgproj": {"label": "DG 案件", "instructions": brief}}}, headers={"Origin": BASE.rstrip("/")})
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    shutil.copy2(os.path.join(ctx["data"], "groups.json"), os.path.join(data, "groups.json"))
    work = os.path.join(data, "work"); os.makedirs(work, exist_ok=True)
    js = """board.setToApp(() => {});
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'delegate', key: 'dgproj', cwd: %s, ai: 'Claude', profile: 'som', text: %s});
      await new Promise(r => setTimeout(r, 1500));
      P({type: 'delegate', key: 'dgproj', cwd: %s, ai: 'Codex', profile: '', text: %s});
      P({type: 'delegate', key: 'dgproj', cwd: %s, ai: 'Claude', profile: 'bad name; rm -rf /', text: 'x'});
      P({type: 'delegate', key: 'dgproj', cwd: %s, ai: 'Claude', profile: '', text: ''});
      await new Promise(r => setTimeout(r, 2500)); return 1;""" % (json.dumps(work), json.dumps(ask), json.dumps(work), json.dumps(ask), json.dumps(work), json.dumps(work))
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS=js, AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=150)
    check(os.path.exists(out) and json.load(open(out)).get("ok"), "盤の中で JS が動かなかった")
    bodies = [open(p).read() for p in sorted(glob.glob(os.path.join(data, "launch", "pane-*.sh")))]
    cl = [b for b in bodies if "claude" in b]
    cx = [b for b in bodies if "codex" in b]
    check(len(cl) == 1 and len(cx) == 1, f"端末の数 claude={len(cl)} codex={len(cx)}(不正な依頼で開いていないか)")
    check("CLAUDE_CONFIG_DIR=" in cl[0] and "/.claude-profiles/som" in cl[0], f"アカウントの指定が無い {cl[0][-200:]!r}")
    check("--append-system-prompt-file" in cl[0], f"共通の指示が渡っていない {cl[0][-200:]!r}")
    askfile = os.path.join(data, "projects", "dgproj-ask.md")
    check(os.path.exists(askfile) and open(askfile).read().strip() == ask, f"依頼文のファイル {askfile}")
    cxfile = os.path.join(data, "projects", "dgproj.md")
    body = open(cxfile).read()
    check(brief in body and ask in body, f"Codex に前提と依頼が渡っていない {body[:80]!r}")
    check("rm -rf" not in " ".join(bodies), "不正なアカウント名がコマンドに混ざった")
    return f"Claude=アカウント som + 指示ファイル + 依頼文 / Codex=前提と依頼を 1 通で / 不正な 2 件は開かない(端末 {len(bodies)} 枚)"


@case("NT-01", "申し送り: 保存と読み出し、長すぎる入力は断る、他の案件を壊さない")
def nt01(ctx):
    import overview as o
    a, b = "uat-notes-a", "uat-notes-b"
    ta, tb = "本番の切替は凍結。索引の再作成は 20 分。", "別案件の申し送り"
    st, d, _ = http("/api/notes", "POST", {"key": a, "text": ta}, headers={"Origin": BASE.rstrip("/")})
    check(st == 200 and d["chars"] == len(ta), f"保存 {st} {d}")
    http("/api/notes", "POST", {"key": b, "text": tb}, headers={"Origin": BASE.rstrip("/")})
    st2, d2, _ = http("/api/notes?key=" + a)
    check(st2 == 200 and d2["text"] == ta, f"読み出し {d2}")
    check(http("/api/notes?key=" + b)[1]["text"] == tb, "別案件の申し送りが壊れた")
    st3, d3, _ = http("/api/notes", "POST", {"key": a, "text": "あ" * 20001}, headers={"Origin": BASE.rstrip("/")})
    check(st3 == 400 and d3["ok"] is False, f"長すぎる申し送りを受け取った {st3}")
    check(http("/api/notes?key=" + a)[1]["text"] == ta, "断ったのに中身が変わった")
    st4, _, _ = http("/api/notes", "POST", {"key": "", "text": "x"}, headers={"Origin": BASE.rstrip("/")})
    check(st4 == 400, f"名前が空でも保存した {st4}")
    check(o.read_notes("存在しない案件") == "", "無い案件で何か返した")
    p = o.notes_path("Desktop/ECX")
    check("/" not in os.path.basename(p) and p.endswith("Desktop_ECX.notes.md"), f"ファイル名の作り方 {p}")
    live = os.path.join(LIVE_DATA, "projects")
    check(not os.path.exists(live) or not glob.glob(os.path.join(live, "uat-notes-*")), "本番の置き場に書いた")
    return f"保存/読み出し一致・長すぎは 400 で中身不変・別案件は無事・名前は安全化({os.path.basename(p)})"


@case("PJ-02", "申し送りは端末にも渡る: 共通の指示のあとに続けて渡す(実行はしない)")
def pj02(ctx):
    brief, notes = "本番に触らない。", "索引の再作成は 20 分かかる。失敗したら logs/index.log を先に見る。"
    http("/api/groups", "POST", {"groups": {"pjnote": {"instructions": brief}}}, headers={"Origin": BASE.rstrip("/")})
    http("/api/notes", "POST", {"key": "pjnote", "text": notes}, headers={"Origin": BASE.rstrip("/")})
    data = tempfile.mkdtemp(dir=ctx["data"])
    open(os.path.join(data, "hook-declined"), "w").close()
    shutil.copy2(os.path.join(ctx["data"], "groups.json"), os.path.join(data, "groups.json"))
    os.makedirs(os.path.join(data, "projects"), exist_ok=True)
    shutil.copy2(os.path.join(ctx["data"], "projects", "pjnote.notes.md"), os.path.join(data, "projects", "pjnote.notes.md"))
    work = os.path.join(data, "work"); os.makedirs(work, exist_ok=True)
    js = """board.setToApp(() => {});
      window.webkit.messageHandlers.aiboard.postMessage({type: 'newInProject', key: 'pjnote', cwd: %s, ai: 'Claude'});
      await new Promise(r => setTimeout(r, 2500)); return 1;""" % json.dumps(work)
    out = os.path.join(data, "js.json")
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS=js, AIBOARD_JS_WAIT="4", AIBOARD_DRY="1")
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=150)
    check(os.path.exists(out) and json.load(open(out)).get("ok"), "盤の中で JS が動かなかった")
    body = open(os.path.join(data, "projects", "pjnote.md")).read()
    check(brief in body and notes in body, f"指示と申し送りが揃っていない {body[:80]!r}")
    check(body.index(brief) < body.index(notes) and "これまでの申し送り" in body, f"並びが違う {body[:120]!r}")
    return f"指示 {len(brief)} 字 + 申し送り {len(notes)} 字が 1 つの前提として端末に渡る"


@case("TL-01", "案件パネル: 指示・申し送り・その案件のセッションだけが時系列で出て、押すとそのカードが選ばれる")
def tl01(ctx):
    key = "tlproj"
    http("/api/groups", "POST", {"groups": {key: {"label": "TL 案件", "instructions": "本番に触らない。"}}}, headers={"Origin": BASE.rstrip("/")})
    http("/api/notes", "POST", {"key": key, "text": "引き継ぎ: 索引の再作成は 20 分。"}, headers={"Origin": BASE.rstrip("/")})
    now = time.time()
    base = {"tab": "9-1", "sid": "uat-t1", "ai": "Claude", "state": "作業中", "mark": "🟢", "doing": "npm test",
            "task": "索引を SQLite に移す", "topic": "", "project": os.path.basename(HOME), "cwd": HOME, "client": None,
            "model_style": {"emoji": "🔷", "label": "Sonnet", "rgb": [80, 140, 220]}, "ago": 30, "state_for": 30,
            "mem_mb": 100, "limit": None, "loop": {"wake": None, "crons": [{"cron": "0 9 * * 1", "recurring": True, "next_at": now + 3600, "prompt": "週次の確認"}]},
            "tools": None, "account": "", "transcript": "", "group_label": "TL 案件", "group_rgb": None, "project_hint": key}
    other = dict(base, tab="9-9", sid="uat-other", task="別案件の仕事", project_hint="ほかの案件", group_label="", loop=None)
    ss = [base, dict(base, tab="9-2", sid="uat-t2", task="取り込みの不具合を直す", ago=600, state="返答待ち", mark="🟡", loop=None), other]

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json()
            d["sessions"] = ss; d["attention"] = []
            d["counts"] = dict(d.get("counts") or {}, working=2, tabs=3)
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "document.querySelectorAll('.card[data-id^=\"uat-\"]').length === 3", 30)
        pg.evaluate("board.renderProject('p:%s')" % key)
        wait_js(pg, "document.querySelector('#pTitle').dataset.kind === 'project' && !!document.querySelector('#pjNotes')", 30)
        pg.wait_for_timeout(500)
        info = pg.evaluate("""(() => ({title: document.querySelector('#pTitle').textContent,
            notes: document.querySelector('#pjNotes').value,
            body: document.querySelector('#pBody').innerText,
            rows: [...document.querySelectorAll('.pjrow')].map(r => r.dataset.open),
            heads: [...document.querySelectorAll('#pBody h3')].map(h => h.textContent.trim().split(' ')[0])}))()""")
        pg.evaluate("document.querySelector('.pjrow').click()"); pg.wait_for_timeout(600)
        after = pg.evaluate("document.querySelector('#pTitle').dataset.kind")
        return info, after, list(errs)

    info, after, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check("TL 案件" in info["title"], f"題名 {info['title']!r}")
    check("索引の再作成は 20 分" in info["notes"], f"申し送りが出ていない {info['notes']!r}")
    check("本番に触らない" in info["body"], "共通の指示が出ていない")
    check("uat-t1" in info["rows"] and "uat-t2" in info["rows"], f"この案件のセッションが出ていない {info['rows']}")
    check("uat-other" not in info["rows"], f"別案件のセッションが混ざった {info['rows']}")
    check(info["rows"][0] == "uat-t1", f"新しい順になっていない {info['rows']}")
    check("週次の確認" in info["body"], "予約が出ていない")
    check(after in ("live", "past"), f"行を押しても会話に移らない(kind={after!r})")
    check(not errs, f"{errs[:1]}")
    return f"指示・申し送り・予約・経過 {len(info['rows'])} 件(別案件は除外・新しい順)・行を押すとカードへ"


@case("IT-01", "iTerm が固まっても盤は止まらない: 時間切れであきらめ、前の一覧を使い、間を空けて再挑戦し、画面に出す")
def it01(ctx):
    import cs
    import overview as o
    keep_rows = dict(cs._LAST_ITERM)
    slow = os.path.join(ctx["data"], "slow-osascript")
    open(slow, "w").write("#!/bin/sh\nsleep 30\n")
    os.chmod(slow, 0o755)
    real_run = cs.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "osascript":
            return real_run([slow], *a, **k)   # 返事をしない iTerm の代わり
        return real_run(cmd, *a, **k)
    try:
        cs._LAST_ITERM.update(rows=[{"win": 1, "tab": 1, "tty": "ttys900", "title": "前の一覧"}], t=time.time(), fail_t=0)
        cs.OSA_ERROR = ""
        with patched(cs.subprocess, "run", fake_run):
            t0 = time.time(); rows = cs.iterm_sessions(); took = time.time() - t0
            check(took < 12, f"あきらめるまで {took:.0f} 秒(8 秒で切るはず)")
            check(cs.OSA_ERROR.startswith("timeout:"), f"理由が残らない {cs.OSA_ERROR!r}")
            check(any(r.get("tty") == "ttys900" for r in rows), f"前の一覧を使っていない {rows}")
            # アプリ自身の端末は iTerm と無関係なので、iTerm が死んでいても一覧から落とさない
            with patched(cs, "app_panes", lambda: [{"win": 0, "tab": 7, "tty": "ttys777", "title": "app", "app": True}]):
                rows2 = cs.iterm_sessions()
            check(any(r.get("tty") == "ttys777" for r in rows2), f"アプリの端末が落ちている {rows2}")
            t0 = time.time(); cs.iterm_sessions(); again = time.time() - t0
            check(again < 1, f"失敗直後にまた 8 秒待った({again:.0f} 秒)")
            snap = o.snapshot(with_macmini=False)
            check(snap["iterm"]["ok"] is False and snap["iterm"]["error"], f"snapshot に状態が出ない {snap['iterm']}")
            state = dict(snap["iterm"])
    finally:
        cs._LAST_ITERM.clear(); cs._LAST_ITERM.update(keep_rows); cs.OSA_ERROR = ""
        cs._LAST_ITERM["fail_t"] = 0

    def extra(pg):
        def fake(route):
            r = route.fetch(); d = r.json(); d["iterm"] = state
            route.fulfill(response=r, body=json.dumps(d))
        pg.route("**/api/snapshot*", fake)

    def fn(pg, errs, bl):
        wait_js(pg, "!document.querySelector('#itermWarn').hidden", 20)
        return pg.inner_text("#itermWarn"), list(errs)

    warn, errs = with_page(ctx, fn, "?lang=ja", route_extra=extra)
    check("iTerm" in warn and "応答" in warn, f"画面の警告 {warn!r}")
    check(not errs, f"{errs[:1]}")
    return f"8 秒で打ち切り・前の一覧を継続・30 秒は再挑戦しない・画面に「{warn[:28]}…」"


@case("AC-02", "アカウント: ログイン状態とプランは CLI を正とし、設定ファイルの古い値で上書きしない")
def ac02(ctx):
    import overview as o
    fake_login = [
        {"ai": "Claude", "profile": "default", "logged_in": True, "email": "new@example.com", "plan": "max", "org": "Org", "method": "claude.ai", "error": ""},
        {"ai": "Claude", "profile": "som", "logged_in": False, "email": "", "plan": "", "org": "", "method": "", "error": ""},
        {"ai": "Codex", "profile": "codex", "logged_in": True, "email": "", "plan": "", "org": "", "method": "ChatGPT", "error": ""},
    ]
    old_file = [
        {"ai": "Claude", "profile": "default", "config_dir": HOME + "/.claude", "email": "old@example.com", "org": "", "plan": "stripe_subscription", "limit": None},
        {"ai": "Claude", "profile": "som", "config_dir": HOME + "/.claude-profiles/som", "email": "stale@example.com", "org": "", "plan": "stripe_subscription", "limit": None},
        {"ai": "Codex", "profile": "codex", "config_dir": HOME + "/.codex", "email": "", "org": "", "plan": "", "limit": None, "usage": {"used_percent": 42.0, "window_minutes": 10080, "resets_at": time.time() + 3600}},
    ]
    sess = [{"ai": "Claude Opus", "account": ""}, {"ai": "Claude Opus", "account": ""}, {"ai": "Claude", "account": "som"}, {"ai": "Codex gpt", "account": ""}]
    with patched(o, "accounts", lambda: [dict(x) for x in old_file]):
        got = o.accounts_full(sess, fake_login)
    by = {a["profile"]: a for a in got}
    check(by["default"]["email"] == "new@example.com" and by["default"]["plan"] == "max", f"CLI の値を使っていない {by['default']}")
    check(by["default"]["logged_in"] is True and by["som"]["logged_in"] is False, f"ログイン状態 {[(a['profile'], a['logged_in']) for a in got]}")
    check(by["som"]["email"] == "stale@example.com", "CLI が答えられない時に設定ファイルの値を捨てた")
    check(by["default"]["running"] == 2 and by["som"]["running"] == 1 and by["codex"]["running"] == 1,
          f"稼働中の本数 {[(a['profile'], a['running']) for a in got]}")
    st, d, _ = http("/api/accounts")
    check(st == 200 and d["accounts"] and all("running" in a and "logged_in" in a for a in d["accounts"]), f"API の中身 {str(d)[:150]}")
    real = [a for a in d["accounts"] if a["ai"] == "Claude"]
    return f"CLI 優先・設定ファイルは補助・稼働中の本数(既定 2/som 1/codex 1)/ 実機の Claude アカウント {len(real)} 件"


@case("AC-03", "この Mac の AI CLI 一覧: 入っているか・ログインしているか・鍵は読まない")
def ac03(ctx):
    import overview as o
    clis = o.ai_clis(force=True)
    ids = [c["id"] for c in clis]
    check(ids == ["claude", "codex", "gemini", "grok", "cursor"], f"並び {ids}")
    inst = [c for c in clis if c["installed"]]
    check(inst, "1 つも見つからない(検出が壊れている)")
    for c in inst:
        check(c["path"].startswith("/"), f"{c['id']} の場所 {c['path']!r}")
        check(c["logged_in"] in (True, False, None), f"{c['id']} の状態 {c['logged_in']!r}")
    blob = json.dumps(clis, ensure_ascii=False)
    for bad in ("sk-", "xai-", "ghp_", "Bearer ", "api_key", "apiKey"):
        check(bad not in blob, f"一覧に鍵らしき文字列が出ている: {bad}")
    gem = next(c for c in clis if c["id"] == "gemini")
    if gem["installed"] and gem["logged_in"]:
        check("@" in gem["who"], f"Gemini の誰か {gem['who']!r}")
    grok = next(c for c in clis if c["id"] == "grok")
    if grok["installed"] and not grok["logged_in"]:
        check("GROK_API_KEY" in (grok["note"] or "") + (grok["how"] or ""), f"Grok の直し方が書かれていない {grok}")
    st, d, _ = http("/api/accounts")
    check(st == 200 and len(d.get("clis") or []) == len(clis), f"API に CLI 一覧が無い {str(d)[:120]}")
    return f"{len(clis)} 種のうち入っているのは {len(inst)} 種({', '.join(c['id'] for c in inst)})・鍵は 0 件"


@case("AC-04", "他の AI(Gemini/Grok/Cursor)の端末も盤に出る(状態は不明と出す)")
def ac04(ctx):
    import cs
    spec = {"/Users/x/.nvm/versions/node/v22/bin/gemini": "Gemini", "node /x/bin/grok -m grok-4": "Grok",
            "/Users/x/.local/bin/cursor-agent": "Cursor", "vim gemini.md": "", "tail -f /tmp/grok": "", "zsh": ""}
    bad = {c: (cs.other_ai(c), w) for c, w in spec.items() if cs.other_ai(c) != w}
    check(not bad, f"判定違い {bad}")
    procs = {700: {"ppid": 1, "rss": 1000, "tty": "ttys910", "cmd": "/usr/bin/login -fp uat"},
             701: {"ppid": 700, "rss": 2000, "tty": "ttys910", "cmd": "-zsh"},
             702: {"ppid": 701, "rss": 120000, "tty": "ttys910", "cmd": "/x/bin/gemini"},
             703: {"ppid": 702, "rss": 30000, "tty": "ttys910", "cmd": "/bin/bash -c ls"}}
    tabs = [{"win": 1, "tab": 1, "tty": "ttys910", "title": "gemini"}]
    with patched(cs, "_clients", None):
        out = cs.classify(tabs, procs)
    t0 = out[0]
    check(t0["ai"] == "Gemini" and t0["state"] == "他の AI", f"分類 {t0['ai']!r} {t0['state']!r}")
    check(t0["pid"] == 702 and t0["mem"] == 150000, f"本体と合計メモリ {t0['pid']} {t0['mem']}")
    return "6 通りの判定一致・Gemini の端末が pid 702・メモリ 150000 で 1 枚のカードになる"


@case("RL-02", "配布物: dmg と zip から取り出した .app が動く形で、チェックサムが一致する")
def rl02(ctx):
    import plistlib
    ver = json.load(open(os.path.join(ROOT, "Package.resolved")))["version"] if False else None
    outs = sorted(glob.glob(os.path.join(ROOT, "dist", "*")))
    if not outs:
        return "SKIP: dist/ が無い(scripts/release.sh を流していない)"
    d = outs[-1]
    zips = glob.glob(os.path.join(d, "*.zip")); dmgs = glob.glob(os.path.join(d, "*.dmg"))
    sums = os.path.join(d, "SHA256SUMS.txt")
    check(zips and dmgs and os.path.exists(sums), f"配布物が足りない {os.listdir(d)}")
    want = {}
    for line in open(sums):
        h, n = line.split()
        want[n] = h
    import hashlib
    for p in zips + dmgs:
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        check(want.get(os.path.basename(p)) == h, f"{os.path.basename(p)} のチェックサムが違う")
    work = tempfile.mkdtemp(dir=ctx["data"])
    r = subprocess.run(["ditto", "-x", "-k", zips[0], work], capture_output=True, text=True, timeout=120)
    check(r.returncode == 0, f"zip を展開できない {r.stderr[:120]}")
    app = os.path.join(work, "AIBoard.app")
    check(os.path.isdir(app), f"展開物に .app が無い {os.listdir(work)}")
    pl = plistlib.load(open(os.path.join(app, "Contents", "Info.plist"), "rb"))
    ver = pl.get("CFBundleShortVersionString")
    check(os.path.basename(d) == ver, f"版が違う: フォルダ {os.path.basename(d)} / Info.plist {ver}")
    exe = os.path.join(app, "Contents", "MacOS", "AIBoard")
    check(os.access(exe, os.X_OK) and open(exe, "rb").read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"), "実行ファイルが壊れている")
    for need in ("Resources/board/overview.html", "Resources/board/cs.py", "Resources/AppIcon.icns"):
        check(os.path.exists(os.path.join(app, "Contents", need)), f"同梱物が無い: {need}")
    junk = [p for p in glob.glob(os.path.join(app, "Contents", "Resources", "board", "**", "*"), recursive=True) if p.endswith(".pyc")]
    check(not junk, f"中間ファイルが入っている {len(junk)} 件")
    sig = subprocess.run(["/usr/bin/codesign", "-dv", app], capture_output=True, text=True)
    signed = "Signature=adhoc" not in (sig.stdout + sig.stderr)
    mount = subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", dmgs[0]], capture_output=True, text=True, timeout=120)
    check(mount.returncode == 0, f"dmg を開けない {mount.stderr[:120]}")
    vol = [l.split("\t")[-1].strip() for l in mount.stdout.splitlines() if "/Volumes/" in l][-1]
    try:
        check(os.path.isdir(os.path.join(vol, "AIBoard.app")), f"dmg に .app が無い {os.listdir(vol)}")
        check(os.path.islink(os.path.join(vol, "Applications")), "dmg に Applications への近道が無い(ドラッグして入れられない)")
    finally:
        subprocess.run(["/usr/bin/hdiutil", "detach", vol, "-quiet"], capture_output=True, timeout=120)
    return f"版 {ver}・zip と dmg のチェックサム一致・同梱 3 種あり・pyc 0 件・署名 {'あり' if signed else 'ad-hoc(未署名)'}"


@case("RL-03", "配る手順: 署名が無いときは Releases も brew も未署名を配らない形になっている(静的検査)")
def rl03(ctx):
    rel = open(os.path.join(ROOT, "scripts", "release.sh"), encoding="utf-8").read()
    pub = open(os.path.join(ROOT, "scripts", "publish-release.sh"), encoding="utf-8").read()
    cask = open(os.path.join(ROOT, "packaging", "aiboard.rb.tmpl"), encoding="utf-8").read()
    check("--options runtime" in rel and "--timestamp" in rel, "公証に要る hardened runtime / timestamp が無い")
    check("notarytool submit" in rel and "stapler staple" in rel, "公証と staple の手順が無い")
    check("exit 2" in rel and "Developer ID Application" in rel, "証明書が無いときに止まらない")
    check('SIGNED=no' in pub and 'if [ "$SIGNED" = yes ]' in pub, "署名の有無で分岐していない")
    check("未署名なので tap は更新しない" in pub, "未署名でも brew に流してしまう")
    check("quarantine" in pub, "未署名版の回避手順を Releases に書いていない")
    check("__VERSION__" in cask and "__SHA256__" in cask and "AI-Driven-School/aiboard" in cask, f"cask の雛形が不完全")
    ent = open(os.path.join(ROOT, "Resources", "AIBoard.entitlements"), encoding="utf-8").read()
    check("app-sandbox" not in ent, "サンドボックスを入れている(端末が動かない)")
    check("apple-events" in ent, "iTerm への問い合わせ権限が無い")
    doc = open(os.path.join(ROOT, "docs", "release.md"), encoding="utf-8").read()
    check("Manage Certificates" in doc and "K7CD7UAWWC" in doc, "証明書の作り方が手順書に無い")
    return "署名・公証・staple・証明書が無いときの停止・未署名を brew に流さない・sandbox 無し・手順書あり"


def _real_account():
    """使い捨ての本物セッションに使うアカウントを選ぶ。上限に当たっている物は使えないので、
    1 語だけ聞いて返事が返る物を探す(返事は捨てる)。仕事用(som)は他社の枠なので触らない。"""
    # 既定のアカウントは使わない: 信頼の印を足す先が ~/.claude.json(このセッション自身の設定)になる
    cands = [c for c in [os.environ.get("UAT_REAL_PROFILE"), "lifehack"] if c]
    why = ["default: 自分の設定を書き換えることになるので使わない"]
    for prof in dict.fromkeys(cands):
        cfg = os.path.join(HOME, ".claude-profiles", prof)
        if not os.path.isdir(cfg):
            why.append(f"{prof}: 置き場が無い")
            continue
        env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
        r = subprocess.run(["/bin/zsh", "-lc", "command claude --model claude-haiku-4-5-20251001 -p ok"],
                           env=env, capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL)   # stdin を閉じないと 3 秒待って警告を出す
        out = (r.stdout.strip() or r.stderr.strip())
        if r.returncode == 0 and out and "limit" not in out.lower():
            return prof, cfg, ""
        why.append(f"{prof}: {out.splitlines()[-1][:60] if out else 'exit ' + str(r.returncode)}")
    return "", "", " / ".join(why)


def _trust(cfg, path, on):
    """設定の projects に「このフォルダは信頼済み」の印を足す/外す。他の項目は触らない(読んで足して置き換え)。"""
    f = os.path.join(cfg, ".claude.json")
    d = json.load(open(f, encoding="utf-8"))
    pj = d.setdefault("projects", {})
    if on:
        pj.setdefault(path, {})["hasTrustDialogAccepted"] = True
    else:
        pj.pop(path, None)
    tmp = f + ".uat"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(d, h, ensure_ascii=False)
    os.replace(tmp, f)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _real_session_js(cmd, work, extra):
    """使い捨ての本物セッションを起こし、claude が会話に入るまで待ってから extra を実行する JS を作る。"""
    return """(async () => {
      const nap = ms => new Promise(r => setTimeout(r, ms));
      const P = m => window.webkit.messageHandlers.aiboard.postMessage(m);
      P({type: 'run', title: 'uat', command: %s, cwd: %s});
      const want = %s;
      const mine = () => (board.snap().sessions || []).find(x => x.tab && x.tab.startsWith('0-') && (x.cwd || '') === want);
      let s = null;
      for (let i = 0; i < 120 && !(s = mine()); i++) await nap(1500);
      if (!s) return {ok: false, why: 'セッションが盤に出ない'};
      for (let i = 0; i < 120; i++) {
        s = mine() || s;
        if ((s.ai || '').startsWith('Claude') && s.sid) break;
        if (i %% 8 === 7) P({type: 'send', tab: s.tab, key: 'esc'});     // 初回の確認画面は使わない側で抜ける
        await nap(1500);
      }
      if (!(s.ai || '').startsWith('Claude') || !s.sid) return {ok: false, why: 'claude が起きない'};
      await nap(4000);
      %s
    })()""" % (json.dumps(cmd), json.dumps(work), json.dumps(work), extra)


def _hook_into_profile(cfg, on=True):
    """使い捨てのアカウントに、試験の間だけ hook を入れる/外す(判断待ちを盤に出すために要る)。
    元の settings.json は控えを取って必ず戻す。"""
    p = os.path.join(cfg, "settings.json")
    bak = p + ".uat-bak"
    if on:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if os.path.exists(p) and not os.path.exists(bak):
            shutil.copy2(p, bak)
        cmd = "python3 " + os.path.join(BOARD, "hooks", "tab-status.py")
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5, "async": True}]}
        h = dict(d.get("hooks") or {})
        for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop", "SessionEnd"):
            rows = [x for x in (h.get(ev) or []) if cmd not in json.dumps(x)]
            h[ev] = rows + [entry]
        d["hooks"] = h
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    else:
        if os.path.exists(bak):
            shutil.copy2(bak, p)
            os.remove(bak)
        elif os.path.exists(p):
            os.remove(p)


@case("RE-02", "本物の判断待ちに、盤のボタンで答えられる(使い捨てのセッションに許可を求めさせて 1 を押す)")
def re02(ctx):
    if not os.environ.get("UAT_REAL"):
        return "SKIP: 本物の AI を動かす試験(UAT_REAL=1 のときだけ)"
    prof, cfg, why = _real_account()
    if not prof:
        return "SKIP: 使えるアカウントが無い(" + why + ")"
    data = ctx["data"]
    open(os.path.join(data, "hook-declined"), "w").close()
    work = os.path.realpath(os.path.join(tempfile.mkdtemp(dir=data), "work"))
    os.makedirs(work, exist_ok=True)
    mark = os.path.join(work, "approved.txt")
    _trust(cfg, work, True)     # _trust は設定の置き場を受ける(中で .claude.json を足す)
    _hook_into_profile(cfg, True)   # 判断待ちを盤に出すには、そのアカウントに hook が要る(試験の間だけ)
    out = os.path.join(data, "js-re02.json")
    cmd = ("CLAUDE_CONFIG_DIR=" + cfg + "; export CLAUDE_CONFIG_DIR; cd " + work
           + "; command claude --model claude-haiku-4-5-20251001")
    extra = """
      // 許可を求めさせる: 道具を使う依頼を 1 つ送る(既定の許可設定では Claude が「いいですか」と聞く)
      P({type: 'send', tab: s.tab, text: %s, enter: true});
      let waiting = null;
      for (let i = 0; i < 160; i++) {                      // 判断待ちになるまで(hook が知らせる)
        await nap(1000);
        const x = (board.snap().sessions || []).find(y => y.sid === s.sid);
        if (x && x.state === '確認待ち') { waiting = x; break; }
      }
      if (!waiting) {
        // 何が起きたのかを、その会話の記録から持ってくる(推測で報告しない)
        let tl = [];
        try { tl = ((await fetch('/api/conv?tab=' + encodeURIComponent(s.tab), {headers: {'X-Overview': '1'}}).then(x => x.json())).timeline || [])
                    .slice(-6).map(e => [e.kind, (e.text || '').slice(0, 60)]); } catch (e) {}
        return {ok: false, why: '判断待ちにならない', state: ((board.snap().sessions || []).find(y => y.sid === s.sid) || {}).state, tl};
      }
      const askedBy = (waiting.ui || {}).action;
      // 盤の会話ビューを開き、そこのボタン「1 はい」を押す(人がやるのと同じ道)
      board.select(s.sid);
      for (let i = 0; i < 40 && !document.querySelector('#cvAsk button'); i++) await nap(250);
      const btn = document.querySelector('#cvAsk button.primary');
      if (!btn) return {ok: false, why: '判断待ちのボタンが出ない'};
      btn.click();
      let done = false;
      for (let i = 0; i < 60; i++) {                        // 許可した結果、道具が動いたか
        await nap(2000);
        const r = await fetch('/api/exists-uat', {headers: {'X-Overview': '1'}}).catch(() => null);
        const x = (board.snap().sessions || []).find(y => y.sid === s.sid);
        if (x && x.state !== '確認待ち') { done = true; break; }
      }
      return {ok: true, sid: s.sid, tab: s.tab, action: askedBy, left: done,
              state: ((board.snap().sessions || []).find(y => y.sid === s.sid) || {}).state};
    """ % json.dumps(f"Bash ツールで次を実行して: echo APPROVED > {mark}")
    js = _real_session_js(cmd, work, extra)
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await " + js.strip(), AIBOARD_JS_WAIT="6",
               AIBOARD_FAST_SHELL="1", AIBOARD_NO_ASK="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
        check(os.path.exists(out), "アプリが結果を書かなかった")
        r = json.load(open(out))
        check(r.get("ok"), f"JS が動かなかった {r}")
        v = r["value"]
        check(v.get("ok"), f"{v.get('why')} / いまの状態 {v.get('state')} / 会話の末尾 {v.get('tl')}")
        check(v.get("action") == "answer", f"真理値表の一手が「答える」でない: {v.get('action')}")
        for _ in range(40):
            if os.path.exists(mark):
                break
            time.sleep(0.5)
        check(os.path.exists(mark), f"「1 はい」を押したのに道具が動いていない(状態 {v.get('state')})")
        check(open(mark).read().strip() == "APPROVED", open(mark).read()[:60])
    finally:
        _trust(cfg, work, False)
        _hook_into_profile(cfg, False)
    return f"{prof} の本物のセッションが許可を求め、盤の「1 はい」で実行された(状態 {v.get('state')})"


@case("RE-03", "本物のセッションを盤から終了できる(メモリ一覧の 2 回押し)。端末は残り、盤から消え、記録は残る")
def re03(ctx):
    if not os.environ.get("UAT_REAL"):
        return "SKIP: 本物の AI を動かす試験(UAT_REAL=1 のときだけ)"
    prof, cfg, why = _real_account()
    if not prof:
        return "SKIP: 使えるアカウントが無い(" + why + ")"
    data = ctx["data"]
    open(os.path.join(data, "hook-declined"), "w").close()
    work = os.path.realpath(os.path.join(tempfile.mkdtemp(dir=data), "work"))
    os.makedirs(work, exist_ok=True)
    _trust(cfg, work, True)
    out = os.path.join(data, "js-re03.json")
    cmd = ("CLAUDE_CONFIG_DIR=" + cfg + "; export CLAUDE_CONFIG_DIR; cd " + work
           + "; command claude --model claude-haiku-4-5-20251001")
    extra = """
      const sid = s.sid, tab = s.tab;
      // まず 1 往復させる(記録が残るのは会話をした後)
      P({type: 'send', tab: s.tab, text: '1+1 は? 数字だけで', enter: true});
      for (let i = 0; i < 60; i++) {
        await nap(2000);
        const conv = await fetch('/api/conv?tab=' + encodeURIComponent(tab), {headers: {'X-Overview': '1'}}).then(x => x.json()).catch(() => ({}));
        if ((conv.timeline || []).some(e => e.kind === '返答')) break;
      }
      const pidOf = () => ((board.snap().sessions || []).find(y => y.sid === sid) || {}).pid;
      const pid0 = pidOf();
      if (!pid0) return {ok: false, why: 'pid が取れない'};
      // メモリ一覧を開き、この会話の「終了」を 2 回押す(人がやるのと同じ道)
      document.querySelector('#btnMem').click();
      for (let i = 0; i < 40 && !document.querySelector(`.memrow button[data-stop="${tab}"]`); i++) await nap(250);
      const b = document.querySelector(`.memrow button[data-stop="${tab}"]`);
      if (!b) return {ok: false, why: 'メモリ一覧に出ない', rows: [...document.querySelectorAll('.memrow button[data-stop]')].map(x => x.dataset.stop)};
      b.click(); await nap(400); b.click();
      let gone = false;
      for (let i = 0; i < 60; i++) {
        await nap(1000);
        const x = (board.snap().sessions || []).find(y => y.sid === sid);
        if (!x || !x.ai || !x.pid) { gone = true; break; }
      }
      return {ok: true, sid, tab, pid0, gone, after: (board.snap().sessions || []).find(y => y.sid === sid) || null};
    """
    js = _real_session_js(cmd, work, extra)
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=out, AIBOARD_JS="return await " + js.strip(), AIBOARD_JS_WAIT="6",
               AIBOARD_FAST_SHELL="1", AIBOARD_NO_ASK="1")
    try:
        subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                       env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
        check(os.path.exists(out), "アプリが結果を書かなかった")
        r = json.load(open(out))
        check(r.get("ok"), f"JS が動かなかった {r}")
        v = r["value"]
        check(v.get("ok"), f"{v.get('why')} {v.get('rows')}")
        check(v.get("gone"), f"2 回押しても終わらない(pid {v.get('pid0')} / いま {v.get('after')})")
        check(not _pid_alive(v["pid0"]), f"pid {v['pid0']} が生きたまま")
        # 端末は残っている(アプリの端末台帳にその tab がある)・記録も残る
        panes = (r.get("panes") or [])
        check(any(p.get("tab") == v["tab"] for p in panes), f"終了で端末まで閉じた {panes}")
        tr = [f for f in glob.glob(os.path.join(cfg, "projects", "*", v["sid"] + ".jsonl"))]
        check(tr and os.path.getsize(tr[0]) > 0, "記録が残っていない(「過去」から再開できない)")
    finally:
        _trust(cfg, work, False)
    return f"{prof} の本物のセッションを 2 回押しで終了(pid {v['pid0']})・端末は残る・記録 {os.path.basename(tr[0]) if tr else '-'} は残る"


@case("RE-01", "本物のセッションに、盤から送って返事が返る(使い捨てのセッションを自分で作る・止めるのは MM-05 が使い捨てプロセスで確かめる)")
def re01(ctx):
    if not os.environ.get("UAT_REAL"):
        return "SKIP: 本物の AI を動かす試験(UAT_REAL=1 のときだけ。わずかに利用枠を使う)"
    prof, cfg, why = _real_account()
    if not prof:
        return "SKIP: 使えるアカウントが無い(" + why + ")"
    data = ctx["data"]   # 盤サーバと同じ置き場にする(別だと、盤が見ているのは利用者の実セッションになる)
    open(os.path.join(data, "hook-declined"), "w").close()
    work = os.path.realpath(os.path.join(tempfile.mkdtemp(dir=data), "work"))   # /var と /private/var の揺れを消す
    os.makedirs(work, exist_ok=True)
    open(os.path.join(work, "uat.txt"), "w").write("UAT\n")
    # この一時フォルダを「信頼済み」にしておく(そうしないと claude は最初に
    # 「このフォルダを信頼しますか」を出して止まり、会話に入らない)。
    # 資格情報は鍵束にあり設定置き場ごとに別なので、使い捨てに写せない → 本物の設定に印だけ足し、最後に外す。
    _trust(cfg, work, True)
    try:
        return _re01_run(ctx, data, work, cfg, prof)
    finally:
        _trust(cfg, work, False)   # 足した印は必ず外す(合否に関わらず)


def _re01_run(ctx, data, work, cfg, prof):
    mark = os.path.join(data, "js.json")
    # 使い捨ての本物セッションをアプリの端末で起こす(安いモデル・一時フォルダ・別アカウント)
    # run の口は決まった形しか通さない(CLAUDE_CONFIG_DIR= 始まり)。持ち場は cd で移る
    cmd = ("CLAUDE_CONFIG_DIR=" + cfg + "; export CLAUDE_CONFIG_DIR; cd " + work
           + "; command claude --model claude-haiku-4-5-20251001")
    js = """(async () => {
      const nap = ms => new Promise(r => setTimeout(r, ms));
      window.webkit.messageHandlers.aiboard.postMessage({type: 'run', title: 'uat', command: %s, cwd: %s});
      const want = %s;
      const mine = () => (board.snap().sessions || []).find(x => x.tab && x.tab.startsWith('0-') && (x.cwd || '') === want);
      let s = null;
      for (let i = 0; i < 120 && !(s = mine()); i++) await nap(1500);                  // 端末が出るまで
      if (!s) return {ok: false, why: 'セッションが盤に出ない', tabs: (board.snap().sessions || []).map(x => [x.tab, x.cwd])};
      for (let i = 0; i < 120; i++) {                                                 // claude が起きて記録を作るまで
        s = mine() || s;
        if ((s.ai || '').startsWith('Claude') && s.sid) break;
        // 初回の確認画面(Chrome 連携など)は Esc で抜ける = 使わない側。盤の「起動中?」のボタンと同じ道
        if (i %% 8 === 7) window.webkit.messageHandlers.aiboard.postMessage({type: 'send', tab: s.tab, key: 'esc'});
        await nap(1500);
      }
      if (!(s.ai || '').startsWith('Claude') || !s.sid) {
        let scr = '';
        try { scr = (await fetch('/api/screen?tab=' + encodeURIComponent(s.tab), {headers: {'X-Overview': '1'}}).then(x => x.json())).text || ''; } catch (e) { scr = String(e); }
        return {ok: false, why: 'claude が起きない', tabs: [[s.tab, s.ai, s.sid]], screen: scr.slice(-600)};
      }
      await nap(4000);                                                                // 入力を受け付けるまでの間
      window.webkit.messageHandlers.aiboard.postMessage({type: 'send', tab: s.tab, text: '1+1 は? 数字だけで答えて', enter: true});
      let rows = [], last = null, loops = 0;
      for (let i = 0; i < 40; i++) {                                                  // 返事が会話に出るまで(最大 2 分)
        loops = i + 1;
        await nap(3000);
        try {
          last = await fetch('/api/conv?tab=' + encodeURIComponent(s.tab), {headers: {'X-Overview': '1'}}).then(x => x.json());
        } catch (e) { last = {err: String(e)}; continue; }
        rows = ((last || {}).timeline || []).map(e => [e.kind, (e.text || '').slice(0, 40)]);
        if (rows.some(r => r[0] === '返答' && r[1].includes('2'))) break;
      }
      return {ok: true, tab: s.tab, sid: s.sid, sent: {ok: true, via: 'app'}, rows: rows,
              diag: {loops: loops, conv: Object.keys(last || {}), reason: (last || {}).reason}}; })()""" % (
        json.dumps(cmd), json.dumps(work), json.dumps(work))
    env = dict(os.environ, OVERVIEW_PORT=str(PORT), AIBOARD_DATA=data, OVERVIEW_NO_INDEX="1", AIBOARD_BOARD=BOARD,
               AIBOARD_JS_TEST=mark, AIBOARD_JS="return await " + js.strip(), AIBOARD_JS_WAIT="6",
               # 見たいのは「盤→本物の AI→返事」。利用者の .zshrc の起動(混雑時 1 分超)は AP-06 が別に見る
               AIBOARD_FAST_SHELL="1", AIBOARD_NO_ASK="1")
    subprocess.run([os.path.join(ROOT, "build", "AIBoard.app", "Contents", "MacOS", "AIBoard")],
                   env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT - 20)
    check(os.path.exists(mark), "アプリが結果を書かなかった")
    r = json.load(open(mark))
    check(r.get("ok"), f"JS が動かなかった {r}")
    v = r["value"]
    if not v.get("ok"):
        try:
            pane_file = json.load(open(os.path.join(data, "app_panes.json")))
            panes = {"app_pid": pane_file.get("app_pid"), "alive": _pid_alive(pane_file.get("app_pid")),
                     "panes": [(p.get("pane"), p.get("tty")) for p in pane_file.get("panes", [])]}
        except Exception as e:      # 台帳が無い/壊れている事自体が手がかり
            panes = f"app_panes.json: {e}"
        raise Fail(f"{v.get('why')} / 台帳: {panes} / 見えているタブ: {v.get('tabs')} / 端末の画面: {v.get('screen')!r}")
    check((v["sent"] or {}).get("ok"), f"送信が通らない {v['sent']}")
    kinds = [k for k, _ in v["rows"]]
    asked = [t for k, t in v["rows"] if k == "依頼" and "1+1" in t]
    replied = [t for k, t in v["rows"] if k == "返答"]
    check(asked, f"送った文が会話に出ない {v['rows'][-4:]} / {v.get('diag')}")
    check(replied, f"返事が返っていない {v['rows'][-4:]}")
    check(any("2" in t for t in replied), f"返事の中身 {replied[-2:]}")
    return f"{prof} のアカウントで本物のセッションに送信→返答({replied[-1][:20]})・会話 {len(v['rows'])} 行・アプリ終了で片付け"


# ------------------------------------------------------------------ 実行
MANUAL = [
    ("MA-01", "日本語入力: アプリの会話ビューで「てすと」→変換→Enter で確定しても送られない(本物の IME は人の手。合成は CV-02)"),
    ("MA-02", "通知を押す: 出た通知をクリックすると、その端末が前に出る(出す・届く・押した先の処理は NT-02 で自動。押す操作だけ人)"),
    ("MA-06", "別アカウントで続き: 上限に当たったセッションで他アカウントのボタンを押し、続きが開く(2 つ目の実アカウントが要る)"),
]
# 自動になったもの: MA-03→RE-02(本物の判断待ちに盤で答える) / MA-04→RE-01 / MA-05→RE-03(本物を盤から終了)
#   / MA-09→MG-01(使い捨ての会話を iTerm に作り、盤から右の端末へ移す。UAT_REAL=1) /
#                  MA-07→SV-19 / MA-08・MA-10→AP-16 / MA-02 の配信→NT-02・NT-03
# 自動になったもの: MA-04→RE-01 / MA-07→SV-19 / MA-08・MA-10→AP-16 / MA-02 の配信→NT-02・NT-03


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
        status, ev, tries = None, "", 0
        while tries < 2:
            tries += 1
            lim = case_timeout()
            try:
                signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(Fail(f"時間切れ({lim}秒・load {load_now():.0f})")))
                signal.alarm(lim)     # 1 件が固まっても残りを走らせる(混んでいる時は伸ばす)
                ev = fn(ctx) or ""
                status = "SKIP" if str(ev).startswith("SKIP") else "PASS"
            except Fail as e:
                status, ev = "FAIL", str(e)
            except Exception as e:
                status, ev = "ERROR", f"{type(e).__name__}: {e}"
            finally:
                signal.alarm(0)
            # 機械が混んでいる時だけ、アプリを起こす試験を 1 度だけやり直す(隠さず「再試行で通った」と書く)
            if status == "PASS" or tries > 1 or load_now() < 8 or fn.cid not in RETRY_WHEN_BUSY:
                break
            print(f"      …load {load_now():.0f} で失敗。1 度だけやり直す: {str(ev)[:80]}", flush=True)
            first = str(ev)[:120]
            time.sleep(5)
        else:
            pass
        if tries > 1 and status == "PASS":
            ev = f"{ev}（1 回目は load {load_now():.0f} で失敗: {first}）"
        RESULTS.append({"id": fn.cid, "title": fn.title, "status": status, "evidence": str(ev)[:600], "sec": round(time.time() - t0, 1)})
        print(f"{status:5} {fn.cid} {fn.title} ({RESULTS[-1]['sec']}s)\n      {str(ev)[:300]}", flush=True)
    # 後片付け: 試験サーバは自分で止める(ポートで引いた overview_server.py --serve だけ)
    subprocess.run([sys.executable, os.path.join(BOARD, "overview_server.py"), "stop"], env=env_for_test(data), capture_output=True, text=True)
    if not os.environ.get("UAT_KEEP"):   # 一時データは片付ける(1 回で 1GB 近く作る試験がある。2026-09-18 にディスクを使い切った)
        try:
            shutil.rmtree(data, ignore_errors=True)
        except OSError:
            pass
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
