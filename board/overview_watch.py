#!/usr/bin/env python3
"""overview_watch.py — `cs watch`。3秒ごとに描き直す全画面(clear+print)。Ctrl-C で終了。

順番: ヘッダ(時刻・メモリバー・今日の強制終了) → 🔴あなたの番 → 🟢作業中 → 顧客プロダクト → 自社プロジェクト → macmini
幅 80〜200 桁で崩れない(全角幅は cs.width/cs.clip で数える)。色は ANSI 24bit、NO_COLOR があれば色なし。
"""
import os
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cs  # noqa: E402
import overview  # noqa: E402

INTERVAL = 3
NOCOLOR = bool(os.environ.get("NO_COLOR"))


def c(rgb, s, bold=False):
    if NOCOLOR or not rgb:
        return s
    r, g, b = rgb
    return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m{s}\033[0m"


def dim(s):
    return s if NOCOLOR else f"\033[2m{s}\033[0m"


def bar(pct, w, rgb=None):
    n = max(0, min(w, round(w * (pct or 0) / 100)))
    return c(rgb, "█" * n) + dim("░" * (w - n))


ANSI = re.compile(r"\033\[[0-9;]*m")


def clip(s, w):
    """幅 w に収める(ANSI エスケープは幅に数えず、切った後に色を戻す)。"""
    out, cur, i = "", 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out += m.group(0)
            i = m.end()
            continue
        ch = s[i]
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        last = i == len(s) - 1
        if cur + cw > w or (cur + cw > w - 1 and not last):   # 最後の1字は幅 w まで許す。途中なら … の分を空ける
            return out + "…" + ("\033[0m" if "\033[" in out else "")
        out += ch
        cur += cw
        i += 1
    return out


def line_session(s, w, show_state=True):
    """1セッション1行。[タブ] 状態 モデル 顧客 経過 ｜ いま ／ 依頼"""
    tag = ""
    if s.get("client"):
        cl = s["client"]
        tag = c(cl.get("rgb"), f"{cl.get('emoji', '')}{cl['label']}", bold=True) + " "
    ms = s.get("model_style") or {}
    model = c(ms.get("rgb"), f"{ms.get('emoji', '❔')} {ms.get('label', '')}") if ms else ""
    acct = f"({s['account']})" if s.get("account") else ""
    mark, label = overview.shown(s)   # 印と状態名は表の答え(盤のカードと同じ)。幅の計算も同じ文字で
    head_plain = f"{s['tab']:<5}{mark} " + (f"{label:<7}" if show_state else "") + \
                 f"{(ms.get('emoji', '❔') + ' ' + ms.get('label', '')) if ms else '':<14}{acct}"
    head = f"{s['tab']:<5}{mark} " + (f"{label:<7}" if show_state else "") + f"{model}{' ' * max(0, 14 - cs.width((ms.get('emoji', '❔') + ' ' + ms.get('label', '')) if ms else ''))}{acct}"
    elapsed = overview.fmt_dur(s.get("state_for")) if s.get("state_for") is not None else "-"
    rest = w - cs.width(head_plain) - cs.width(elapsed) - 4
    tagp = f"{s['client'].get('emoji', '')}{s['client']['label']} " if s.get("client") else ""
    # 先頭は「いま何の作業か」(フックの Haiku 要約＝タブ題名と同じ)。最後の依頼(「はい」等)は話題にならない
    body = f"{tagp}{s.get('topic') or s.get('task', '')} ▸ {s.get('doing', '')}"
    body = clip(body, max(10, rest))
    if tagp:
        body = body.replace(tagp, tag, 1)
    return f"{head} {dim(elapsed):>{len(elapsed)}}  {body}"


def render(snap, w):
    out = []
    m = snap["machine"]
    js = m.get("jetsam", {})
    now = time.strftime("%H:%M:%S")
    if m.get("ok"):
        pct = m["used_pct"]
        rgb = (220, 60, 60) if pct >= 85 else (230, 170, 40) if pct >= 70 else (60, 170, 90)
        detail = f" ({m['used_gb']}/{m['total_gb']}GB 圧縮{m['compressed_gb']}GB swap{(m['swap_used_mb'] or 0) / 1024:.1f}GB)" if w >= 120 else ""
        memtxt = f"メモリ {bar(pct, 20 if w >= 120 else 10, rgb)} {pct}%{detail}"
    else:
        memtxt = c((220, 60, 60), f"メモリ 未測定: {m.get('reason')}")
    kill = f"強制終了 今日{js.get('today', '?')}回/24h {js.get('last24h', '?')}回"
    if js.get("last_ago") is not None:
        kill += f"(最後 {overview.fmt_dur(js['last_ago'])}前)"
    if not js.get("ok", True):
        kill += c((220, 60, 60), f" ※{js.get('reason')}")
    cnt = snap["counts"]
    out.append(f"{now}  {memtxt}  {kill}")
    # モデル別の集計
    models = {}
    for s in snap["sessions"]:
        ms = s.get("model_style")
        if not ms or s["mark"] == "⚪":
            continue
        k = f"{ms.get('emoji', '❔')}{ms.get('label', '')}"
        models.setdefault(k, {"rgb": ms.get("rgb"), "n": 0, "busy": 0})
        models[k]["n"] += 1
        models[k]["busy"] += 1 if overview.is_working(s) else 0
    mtxt = "  ".join(c(v["rgb"], f"{k}×{v['n']}") + dim(f"(作業中{v['busy']})") for k, v in models.items())
    out.append(f"タブ{cnt['tabs']}  🔴あなたの番 {len(snap['attention'])}  🟢作業中 {cnt['working']}  🟡返答待ち {cnt['waiting']}   {mtxt}")
    out.append(dim("─" * w))
    # あなたの番
    att = snap["attention"]
    out.append(c((230, 80, 80), f"🔴 あなたの番 ({len(att)})", bold=True))
    if not att:
        out.append(dim("  なし"))
    for s in att:
        out.append("  " + line_session(s, w - 2, show_state=False) + "  " + dim(s["why"]))
    # 作業中
    work = [s for s in snap["sessions"] if overview.is_working(s)]
    out.append(c((80, 190, 100), f"🟢 作業中 ({len(work)})", bold=True))
    for s in work:
        out.append("  " + line_session(s, w - 2, show_state=False))
    att_tabs = {s["tab"] for s in att}
    others = [s for s in snap["sessions"] if not overview.is_working(s) and s["mark"] != "⚪" and s["tab"] not in att_tabs]
    if others:
        out.append(c((200, 180, 60), f"🟡 返答待ち ({len(others)})", bold=True))
        for s in others:
            out.append("  " + line_session(s, w - 2, show_state=False))
    out.append(dim("─" * w))
    # 顧客プロダクト
    out.append(c((200, 200, 220), "■ 顧客プロダクト", bold=True))
    if not snap["clients"]:
        out.append(dim("  (いま動いている顧客の作業は無い)"))
    for g in snap["clients"]:
        st = " ".join(f"{k}{v}" for k, v in g["states"].items())
        tr = f"今日の依頼 {g['today_requests']}件" + ("+" if g["today_requests_partial"] else "")
        out.append("  " + c(g.get("rgb"), f"{g.get('emoji', '')}{g['label']}", bold=True) +
                   f"  {g['sessions']}セッション({st})  {tr}  更新 {overview.fmt_dur(g['last_update_ago'])}前")
        out.append("    " + clip(dim("最新の依頼: ") + g["latest_task"], w - 4))
    out.append(c((200, 200, 220), "● 自社プロジェクト", bold=True))
    for g in snap["projects"]:
        st = " ".join(f"{k}{v}" for k, v in g["states"].items())
        tr = f"今日の依頼 {g['today_requests']}件" + ("+" if g["today_requests_partial"] else "")
        out.append(clip(f"  {g['name']:<18} {g['sessions']}セッション({st})  {tr}  更新 {overview.fmt_dur(g['last_update_ago'])}前", w))
        out.append("    " + clip(dim("最新の依頼: ") + g["latest_task"], w - 4))
    out.append(dim("─" * w))
    # macmini
    mm = snap["macmini"]
    if mm.get("ok"):
        age = overview.fmt_dur(time.time() - mm["fetched"])
        out.append(c((120, 200, 220), f"▣ macmini ({mm['host']}) 取得 {age}前  load {mm.get('load1')}  稼働 {mm.get('uptime')}", bold=True))
        y = mm["ytfactory"]
        ylast = time.strftime("%m-%d %H:%M", time.localtime(y["out_log_mtime"])) if y.get("out_log_mtime") else "?"
        state = ("⏸ PAUSE中(publish停止)" if y.get("paused") else "稼働") + f"  最後 {ylast}  exit {y.get('last_exit')}  runs {y.get('runs')}"
        out.append(f"  YouTube工場 {y['label']}: {state}")
        out.append("    " + clip(dim(y.get("note", "")), w - 4))
        for ca in mm.get("cron_ai", []):
            out.append(clip(f"  cron claude -p: {ca['schedule']}  「{ca['prompt']}」", w))
        p = mm["pm2"]
        down = [x["name"] for x in p["list"] if x.get("status") != "online"]
        col = (60, 170, 90) if not down else (230, 170, 40)
        out.append(f"  PM2: " + c(col, f"online {p['online']}/{p['total']}") + (("  停止: " + clip(", ".join(down), w - 30)) if down else ""))
        cw = mm["cron_wrap"]
        fails = [j for j in cw["jobs"] if not j["ok"] and j["ago"] < 86400]
        out.append(f"  cron-wrap: 今日 ok {cw['today']['ok']} / FAIL {cw['today']['FAIL']}  (24h ok {cw['last24h']['ok']} / FAIL {cw['last24h']['FAIL']})  ジョブ {len(cw['jobs'])}本")
        for j in fails[:6]:
            out.append("    " + clip(c((220, 90, 90), f"FAIL {j['name']}") + dim(f" exit={j['exit']} {overview.fmt_dur(j['ago'])}前" + (" 外部送信あり" if j['external'] else "") + (" " + j['ledger_note'][:50] if j['ledger_note'] else "")), w - 4))
        if len(fails) > 6:
            out.append(dim(f"    …ほか {len(fails) - 6} 本"))
    else:
        out.append(c((220, 60, 60), f"▣ macmini 取得失敗: {mm.get('reason')}", bold=True))
        if mm.get("stale"):
            out.append(dim(f"  前回成功分({overview.fmt_dur(mm['stale_age'])}前)があるが、古いので表示しない"))
    out.append(dim(f"取得 {snap['took']}s  Ctrl-C で終了"))
    return "\n".join(clip(l, w) for l in out)


def draw_once(width=None):
    w = width or (os.get_terminal_size().columns if sys.stdout.isatty() else 120)
    w = max(80, min(200, w))
    snap = overview.snapshot()
    return render(snap, w), snap["took"]


def main(args=()):
    once = "--once" in args
    width = None
    for a in args:
        if a.startswith("--width="):
            width = int(a.split("=")[1])
    try:
        while True:
            txt, took = draw_once(width)
            if once:
                print(txt)
                return
            sys.stdout.write("\033[H\033[2J" + txt + "\n")
            sys.stdout.flush()
            time.sleep(max(INTERVAL, took * 2))   # 取得に 3 秒近く掛かる重い時は間隔を空けて CPU を食わない
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
