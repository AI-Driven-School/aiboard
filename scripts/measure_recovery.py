#!/usr/bin/env python3
"""「止まった仕事を、どれだけ取り戻せているか」の基準値を測る。

**数え方はこのファイルに書かない。** board/recovery.py（盤の台帳が使うもの）をそのまま import する。
2026-09-23 まではここに同じ関数を複製していた。複製と正典は**同じ答えを出していた**ので
（1,649 件 / auth 1,040 / 対話 470 で一致）、これはバグではなく、次に定義を変えたときに
片方だけ直る危険があっただけ。消して 1 か所にした。

**この数字は後から再現できない。** 理由は 2 つあり、どちらも 2026-09-23 に実測した:
  1. Claude Code は `cleanupPeriodDays`（既定 30 日）で会話記録を消す。実際、手元の最古の
     mtime は 30 日前ちょうどだった。**測った 30 日ぶんの元データは、30 日後には無い。**
     2026-09-20 に公開した「認証 989 件」を今日の同じ窓で測ると 899 件になるが、
     差の 90 件がどこへ行ったかは確かめようがない（消えた会話を読めないため）。
  2. だから、公開する数字は**測った日を必ず添える**。過去の数字を後から検算する予定があるなら、
     その日の結果をファイルに残すこと（--json）。記録を残さなければ検証不能な主張になる。

定義（recovery.py の docstring が正典）:
  止まり   : `isApiErrorMessage`。連続する同じ種類は 1 件にまとめる（行数は件数ではない）
  復帰     : その後、同じ会話に**人が書いた**時刻。空の発言と盤の定型文は数えない
  止まり時間: 止まり → 復帰。戻らなかったものは合計に入れない
  対話/無人: 記録の entrypoint。sdk-cli（claude -p / SDK）は戻る人が居ないので合算しない

  python3 scripts/measure_recovery.py [--days 30] [--json 出力先]
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "board"))
import recovery as R                                    # noqa: E402  定義の正典

QUICK = R.QUICK
KINDS = R.KINDS
board_actions = R.board_action_times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    files = R.transcripts(a.days)
    events = R.scan(days=a.days)          # 変わっていない会話は読み直さない（正典側のキャッシュ）
    # scan() が選ぶのは「mtime が N 日以内のファイル」。古い会話を今日 1 行でも触ると、
    # その会話の何か月前の止まりまで入ってしまう（2026-09-23 実測: 1,649 件中 26 件が窓の外）。
    # 「直近 N 日」と名乗る以上、**出来事の時刻**で切る。mtime ≧ 出来事の時刻なので取りこぼしは無い。
    cut = time.time() - a.days * 86400
    events = [e for e in events if e.get("at") and e["at"] >= cut]
    tag = {"sdk-cli": "無人", "cli": "対話"}
    for e in events:
        e["ep"] = tag.get(e.get("ep"), "不明")
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
