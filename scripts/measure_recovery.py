#!/usr/bin/env python3
"""「止まった仕事を、どれだけ取り戻せているか」の基準値を測る。

盤の「取り戻した仕事」の台帳は、この定義をそのまま使う（測り方が食い違わないように 1 か所に置く）。

  止まり   : 会話記録の `isApiErrorMessage` の行。**連続する同じ種類は 1 件にまとめる**
             （行数は「しつこさ」であって出来事の数ではない。2026-09-20 の反証で判明）
             種類: auth（未ログイン・鍵が無効・OAuth 失効）/ limit（5 時間・7 日・超過）/ credits / transient
  復帰     : その止まりのあと、同じ会話に**人が次に書いた**時刻（道具の結果や中断の印は数えない）
  止まり時間: 止まり → 復帰 の差。戻らなかったものは**合計に入れない**（開いた区間を足すと水増しになる）
  盤の関与  : 盤から送った・盤から止めた・自動再開が動いた記録（~/.aiboard の send.log / stop.log / actions.jsonl）
             と時刻で突き合わせる。**盤を経由したものだけ**を「盤が取り戻した」と数える

  python3 scripts/measure_recovery.py [--days 30] [--json 出力先]
"""
import argparse
import glob
import json
import os
import re
import statistics
import time
from datetime import datetime

HOME = os.path.expanduser("~")
QUICK = 600           # 10 分以内に戻れたら「すぐ気づけた」
KINDS = ("auth", "limit", "credits", "transient")


def ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def kind_of(txt, quota):
    if quota.get("status") == "rejected":
        return "limit"
    if re.search(r"Invalid API key", txt, re.I):
        return "auth"
    if re.search(r"Not logged in|Login expired|OAuth (session )?expired|Please run /login", txt, re.I):
        return "auth"
    if re.search(r"out of usage credits", txt, re.I):
        return "credits"
    if re.search(r"usage limit|rate limit", txt, re.I):
        return "limit"
    if re.search(r"went to sleep|response stopped|reach the API|overloaded|timeout|Connection error", txt, re.I):
        return "transient"
    return ""


def transcripts(days):
    cut = time.time() - days * 86400
    out = []
    for root in [os.path.join(HOME, ".claude", "projects")] + sorted(glob.glob(os.path.join(HOME, ".claude-profiles", "*", "projects"))):
        for p in glob.glob(os.path.join(root, "*", "*.jsonl")):
            try:
                if os.path.getmtime(p) >= cut:
                    out.append(p)
            except OSError:
                pass
    return out


def entrypoint_of(path, head=40):
    """その会話が対話(cli)か無人(sdk-cli = claude -p / SDK)か。無人には「戻る人」が居ないので分けて数える。"""
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if '"entrypoint"' in line:
                    try:
                        return json.loads(line).get("entrypoint") or "不明"
                    except ValueError:
                        return "不明"
                if i > head:
                    break
    except OSError:
        pass
    return "不明"


def stops_in(path):
    """1 つの会話から、止まりの出来事を拾う。返り値: [{kind, at, back_at}]"""
    events, open_ev, last_kind = [], None, ""
    try:
        fh = open(path, errors="replace")
    except OSError:
        return events
    with fh:
        for line in fh:
            flat = line.replace(" ", "")
            if '"isApiErrorMessage":true' not in flat and '"type":"user"' not in flat:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            at = ts(d.get("timestamp"))
            if d.get("type") == "user":
                if d.get("isMeta") or re.search(r"\[Request interrupted", line):
                    continue
                c = d.get("message", {}).get("content")
                human = isinstance(c, str) or (isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "text" for b in c))
                if human and open_ev is not None:
                    open_ev["back_at"] = at
                    events.append(open_ev)
                    open_ev, last_kind = None, ""
                continue
            if not d.get("isApiErrorMessage"):
                continue
            txt = "".join(b.get("text", "") for b in (d.get("message", {}).get("content") or []) if isinstance(b, dict))
            k = kind_of(txt, d.get("quotaLimits") or {})
            if not k:
                continue
            if open_ev is not None and k == last_kind:
                continue                      # 同じ止まりの繰り返し
            if open_ev is not None:
                events.append(open_ev)
            open_ev, last_kind = {"kind": k, "at": at, "back_at": None, "file": path}, k
    if open_ev is not None:
        events.append(open_ev)
    return events


def board_actions():
    """盤を経由した操作の時刻。これがあった止まりだけ「盤が取り戻した」と数える。"""
    out = []
    for name in ("send.log", "stop.log"):
        p = os.path.join(HOME, ".aiboard", name)
        try:
            for line in open(p, errors="replace"):
                if not line.strip().startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                    out.append(time.mktime(time.strptime(d["t"], "%Y-%m-%d %H:%M:%S")))
                except (ValueError, KeyError):
                    pass
        except OSError:
            pass
    p = os.path.join(HOME, ".aiboard", "actions.jsonl")
    try:
        for line in open(p, errors="replace"):
            try:
                out.append(float(json.loads(line)["t"]))
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    files = transcripts(a.days)
    events = []
    for f in files:
        got = stops_in(f)
        if got:
            ep = entrypoint_of(f)
            tag = "無人" if ep == "sdk-cli" else ("対話" if ep == "cli" else "不明")
            for e in got:
                e["ep"] = tag
            events += got
    by_ep = {}
    for e in events:
        b = by_ep.setdefault(e["ep"], {"events": 0, "quick": 0, "never": 0, "hours": 0.0})
        b["events"] += 1
        if not e["back_at"]:
            b["never"] += 1
        elif e["at"] and e["back_at"] > e["at"]:
            g = e["back_at"] - e["at"]
            b["hours"] += g / 3600
            if g <= QUICK:
                b["quick"] += 1
    acts = board_actions()

    def near_board(ev, window=900):
        """止まりから 15 分以内に、盤を経由した操作があったか。"""
        if not ev["at"]:
            return False
        import bisect
        i = bisect.bisect_left(acts, ev["at"])
        return i < len(acts) and acts[i] - ev["at"] <= window

    gaps = lambda es: [e["back_at"] - e["at"] for e in es if e["back_at"] and e["at"] and e["back_at"] > e["at"]]
    rows = {}
    for k in KINDS:
        es = [e for e in events if e["kind"] == k]
        g = gaps(es)
        rows[k] = {
            "events": len(es), "returned": len(g), "never": sum(1 for e in es if not e["back_at"]),
            "quick": sum(1 for x in g if x <= QUICK),
            "median_min": round(statistics.median(g) / 60, 1) if g else None,
            "p90_min": round(sorted(g)[int(.9 * len(g))] / 60, 1) if g else None,
            "stopped_hours": round(sum(g) / 3600, 1),
        }
    allg = gaps(events)
    total = {
        "days": a.days, "files": len(files), "events": len(events),
        "returned": len(allg), "never": sum(1 for e in events if not e["back_at"]),
        "quick": sum(1 for x in allg if x <= QUICK),
        "quick_share": round(sum(1 for x in allg if x <= QUICK) / max(1, len(events)), 3),
        "stopped_hours": round(sum(allg) / 3600, 1),
        "board_touched": sum(1 for e in events if near_board(e)),
        "board_actions_total": len(acts),
    }
    # 週ごと（台帳の単位）
    weeks = {}
    for e in events:
        if not e["at"]:
            continue
        wk = time.strftime("%m/%d", time.localtime(e["at"] - (datetime.fromtimestamp(e["at"]).weekday() * 86400)))
        w = weeks.setdefault(wk, {"events": 0, "quick": 0, "never": 0, "hours": 0.0})
        w["events"] += 1
        if not e["back_at"]:
            w["never"] += 1
        elif e["at"] and e["back_at"] > e["at"]:
            g = e["back_at"] - e["at"]
            w["hours"] += g / 3600
            if g <= QUICK:
                w["quick"] += 1
    print(f"直近 {a.days} 日 / 会話 {len(files)} 本 / 止まりの出来事 {len(events)} 件\n")
    print(f"{'種類':10}{'出来事':>7}{'戻れた':>7}{'10分以内':>9}{'戻らず':>7}{'中央値':>9}{'p90':>9}{'止まり時間':>10}")
    for k in KINDS:
        r = rows[k]
        print(f"{k:10}{r['events']:7}{r['returned']:7}{r['quick']:9}{r['never']:7}"
              f"{(str(r['median_min']) + '分') if r['median_min'] is not None else '-':>9}"
              f"{(str(r['p90_min']) + '分') if r['p90_min'] is not None else '-':>9}{r['stopped_hours']:9}h")
    print(f"\n【基準値】10 分以内に戻れた割合 {100 * total['quick_share']:.1f}%（{total['quick']}/{total['events']}）"
          f" / 戻らなかった {total['never']} 件 / 戻るまでの合計 {total['stopped_hours']}h")
    print(f"盤を経由した操作は通算 {total['board_actions_total']} 回、止まりの 15 分以内にあったのは {total['board_touched']} 件")
    print("\n対話 / 無人の別（無人 = claude -p や SDK。戻る人がそもそも居ない）")
    for k in ("対話", "無人", "不明"):
        b = by_ep.get(k)
        if not b:
            continue
        print(f"  {k:4} 止まり {b['events']:5} / 10 分以内 {b['quick']:4}（{100 * b['quick'] / max(1, b['events']):4.1f}%）"
              f"/ 戻らず {b['never']:5} / 止まり時間 {b['hours']:7.1f}h")
    print("\n週ごと（台帳の単位）")
    for wk in sorted(weeks)[-6:]:
        w = weeks[wk]
        print(f"  {wk} の週: 止まり {w['events']:4} / 10 分以内 {w['quick']:4} / 戻らず {w['never']:4} / 止まり時間 {w['hours']:6.1f}h")
    if a.json:
        json.dump({"total": total, "by_kind": rows, "by_entrypoint": by_ep, "weeks": weeks}, open(a.json, "w"), ensure_ascii=False, indent=1)
        print(f"\n{a.json} に書いた")


if __name__ == "__main__":
    main()
