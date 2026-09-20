#!/usr/bin/env python3
"""止まった「回数」ではなく「出来事」を数え、止まってから人が戻るまでの時間を測る。

- 同じ会話で連続する API エラー行は 1 つの出来事にまとめる(間に人の発言が無ければ同じ止まり)
- 出来事ごとに「次に人が書いた時刻」までの差を取る。無ければ「戻っていない」
"""
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
CUT = 30 * 86400


def ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def kind_of(txt, q):
    if q.get("status") == "rejected":
        return {"five_hour": "limit", "seven_day": "limit"}.get(q.get("rateLimitType") or "", "limit")
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


def main():
    roots = [os.path.join(HOME, ".claude", "projects")] + \
            sorted(glob.glob(os.path.join(HOME, ".claude-profiles", "*", "projects")))
    files = []
    now = __import__("time").time()
    for r in roots:
        for p in glob.glob(os.path.join(r, "*", "*.jsonl")):
            try:
                if os.path.getmtime(p) >= now - CUT:
                    files.append(p)
            except OSError:
                pass
    events, convs = [], set()
    for p in files:
        last_kind, open_ev = "", None
        try:
            fh = open(p, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"isApiErrorMessage":true' not in line.replace(" ", "") and '"type":"user"' not in line.replace(" ", ""):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = ts(d.get("timestamp"))
                if d.get("type") == "user":
                    if re.search(r"\[Request interrupted", line) or d.get("isMeta"):
                        continue
                    c = d.get("message", {}).get("content")
                    human = isinstance(c, str) or (isinstance(c, list) and any(
                        isinstance(b, dict) and b.get("type") == "text" for b in c))
                    if human and open_ev is not None:
                        open_ev["back_at"] = t
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
                    open_ev["lines"] += 1        # 同じ止まりの繰り返し
                    continue
                if open_ev is not None:
                    events.append(open_ev)
                open_ev = {"file": p, "kind": k, "at": t, "lines": 1, "back_at": None}
                last_kind = k
                convs.add(p)
        if open_ev is not None:
            events.append(open_ev)
    by = {}
    for e in events:
        by.setdefault(e["kind"], []).append(e)
    print(f"会話ログ {len(files)} 本 / 止まった出来事 {len(events)} 件 / 止まった会話 {len(convs)} 本")
    print(f"{'種類':10} {'出来事':>6} {'エラー行':>7} {'戻った':>6} {'中央値':>9} {'p90':>9} {'戻っていない':>8}")
    for k in sorted(by, key=lambda x: -len(by[x])):
        es = by[k]
        gaps = [e["back_at"] - e["at"] for e in es if e["back_at"] and e["at"] and e["back_at"] > e["at"]]
        med = statistics.median(gaps) if gaps else 0
        p90 = sorted(gaps)[int(.9 * len(gaps))] if gaps else 0
        print(f"{k:10} {len(es):6} {sum(e['lines'] for e in es):7} {len(gaps):6} "
              f"{med/60:8.1f}分 {p90/60:8.1f}分 {sum(1 for e in es if not e['back_at']):8}")
    allg = [e["back_at"] - e["at"] for e in events if e["back_at"] and e["at"] and e["back_at"] > e["at"]]
    if allg:
        print(f"全体: 戻るまで 中央値 {statistics.median(allg)/60:.1f}分 / p90 {sorted(allg)[int(.9*len(allg))]/60:.1f}分 / "
              f"1 時間以上 {sum(1 for g in allg if g > 3600)} 件 / 1 日以上 {sum(1 for g in allg if g > 86400)} 件")
    json.dump({"files": len(files), "events": len(events), "convs": len(convs),
               "by": {k: {"events": len(v), "lines": sum(e["lines"] for e in v),
                          "never": sum(1 for e in v if not e["back_at"])} for k, v in by.items()}},
              open(sys.argv[1], "w") if len(sys.argv) > 1 else sys.stdout, ensure_ascii=False, indent=1)


main()
