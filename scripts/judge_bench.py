#!/usr/bin/env python3
"""判定器を実物のモデルで測る(手元の Ollama / LM Studio の OpenAI 互換口)。

製品と同じ文面(judge._prompt)・同じ呼び方(judge._call)で、2 つの判断を問う:
  client:   顧客の付いていないセッションに、どの顧客が近いか
            正解の代わり = フォルダの場所で決まる規則(clients.json の paths)。場所で当たったものを「その顧客」、
            どの規則にも当たらないものを「none」とする。**規則との一致率**であって正しさではない
            (none 側には、規則に書いていないだけの顧客の仕事が混ざりうる)
  priority: 対応すべきもののうち、最初に見せる 1 件
            正解の代わり = 盤の規則(attention の順: 確認待ち・停止 → 長く放置した返答待ち → 起動途中、同順位は古い方)。
            製品は規則で並べた順のまま渡すので「並べた順」と、理解しているかを見る「混ぜた順」の 2 通り

  python3 scripts/judge_bench.py [--models mistral:latest,llama3:latest] [--n 30] [--url http://127.0.0.1:11434/v1/chat/completions]

外へは何も出さない(URL は 127.0.0.1 / localhost だけ受け付ける)。明細は ~/aiboard-private/judge/ に残し、
画面には顧客名もフォルダ名も出さない(数字だけ)。
"""
import argparse
import glob
import json
import os
import random
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "board"))
import cs            # noqa: E402
import judge         # noqa: E402
import overview      # noqa: E402

HOME = os.path.expanduser("~")
PRODUCT_TIMEOUT = judge.DEFAULTS["timeout"]


def transcripts(days=30):
    cut = time.time() - days * 86400
    out = []
    for p in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(p) >= cut:
                out.append(p)
        except OSError:
            pass
    return sorted(out)


def cwd_of(path):
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if '"cwd"' in line:
                    try:
                        return json.loads(line).get("cwd") or ""
                    except ValueError:
                        pass
                if i > 60:
                    break
    except OSError:
        pass
    return ""


def client_cases(n, rng):
    """場所で顧客が決まるもの n 件と、どの規則にも当たらないもの n 件。"""
    import clients
    pos, neg = [], []
    files = transcripts()
    rng.shuffle(files)
    for p in files:
        if len(pos) >= n and len(neg) >= n:
            break
        cwd = cwd_of(p)
        if not cwd:
            continue
        task = (cs.first_user_prompt(p) or "")[:120]
        if not task:
            continue
        by_path = clients.classify(cwd=cwd)
        any_hit = clients.classify(cwd=cwd, texts=(task,))
        ctx = {"project": os.path.basename(cwd), "cwd": os.path.basename(cwd), "task": overview.redact(task)}
        if by_path and len(pos) < n:
            pos.append({"ctx": ctx, "want": by_path["id"]})
        elif not any_hit and len(neg) < n:
            neg.append({"ctx": ctx, "want": "none"})
    return pos, neg


def priority_cases(n, rng):
    """盤の attention と同じ規則で「最初の 1 件」が決まる組を n 個。文は製品と同じ形。"""
    kinds = [("確認待ち", 0), ("codex 停止", 0), ("返答待ち", 1), ("確認画面で停止", 2), ("起動中?", 2)]
    words = ["テストを直す", "記事の下書き", "請求書の集計", "ビルドの修正", "画像の生成", "移行スクリプト", "レビュー", "ログの調査"]
    out = []
    for i in range(n):
        k = rng.randint(3, 5)
        items = []
        for j in range(k):
            st, rank = rng.choice(kinds)
            dur = rng.choice([30, 120, 600, 1800, 3600, 7200]) if rank != 1 else rng.choice([1800, 3600, 7200, 14400])
            items.append({"id": f"s{i}-{j}", "rank": rank, "for": dur,
                          "text": f"{st} {overview.fmt_dur(dur)} proj{j} {rng.choice(words)}"})
        items.sort(key=lambda x: (x["rank"], -x["for"]))
        if len(items) > 1 and (items[0]["rank"], items[0]["for"]) == (items[1]["rank"], items[1]["for"]):
            continue      # 規則でも決まらない組は使わない
        out.append({"sorted": items, "want": items[0]["id"]})
    return out


def ask(url, model, kind, options, ctx, timeout):
    t0 = time.time()
    try:
        choice, raw = judge._call(url, model, "", judge._prompt(kind, options, ctx), timeout)
        err = ""
    except Exception as e:          # 測定なので、失敗も 1 件として数える
        choice, raw, err = None, "", f"{type(e).__name__}: {e}"[:120]
    return {"choice": choice, "raw": raw, "err": err, "ms": int((time.time() - t0) * 1000)}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="mistral:latest,llama3:latest,phi3:instruct")
    ap.add_argument("--url", default="http://127.0.0.1:11434/v1/chat/completions")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=60.0, help="測るための上限(製品の既定は 4 秒。4 秒以内の割合も出す)")
    a = ap.parse_args()
    if not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", a.url):
        sys.exit("手元のモデルだけ測る(127.0.0.1 / localhost)")
    rng = random.Random(20260919)
    pos, neg = client_cases(a.n, rng)
    pri = priority_cases(a.n, rng)
    copts = [{"id": c["id"], "text": f'{c.get("label") or c["id"]} {" ".join((c.get("keywords") or [])[:6])}'}
             for c in overview.client_defs()]
    outdir = os.path.join(HOME, "aiboard-private", "judge")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = open(os.path.join(outdir, f"bench-{stamp}.jsonl"), "w", encoding="utf-8")
    print(f"題材: client 場所で決まる {len(pos)} 件 + 規則に当たらない {len(neg)} 件(顧客の候補 {len(copts)})/ priority {len(pri)} 組")
    summary = {}
    for model in [m for m in a.models.split(",") if m]:
        warm = ask(a.url, model, "priority", pri[0]["sorted"], {"count": 3}, a.timeout)   # 読み込みの時間は別に数える
        rows = []
        for c in pos + neg:
            r = ask(a.url, model, "client", copts, c["ctx"], a.timeout)
            rows.append({"task": "client_pos" if c["want"] != "none" else "client_neg", "want": c["want"], **r})
        for p in pri:
            for order in ("sorted", "shuffled"):
                opts = list(p["sorted"])
                if order == "shuffled":
                    rng.shuffle(opts)
                r = ask(a.url, model, "priority", [{"id": o["id"], "text": o["text"]} for o in opts],
                        {"count": len(opts)}, a.timeout)
                rows.append({"task": "priority_" + order, "want": p["want"], "first": opts[0]["id"], **r})
        for r in rows:
            log.write(json.dumps({"model": model, **r}, ensure_ascii=False) + "\n")
        s = {"warm_ms": warm["ms"]}
        for t in ("client_pos", "client_neg", "priority_sorted", "priority_shuffled"):
            rs = [r for r in rows if r["task"] == t]
            ms = [r["ms"] for r in rs]
            s[t] = {"n": len(rs), "agree": sum(1 for r in rs if r["choice"] == r["want"]),
                    "invalid": sum(1 for r in rs if not r["err"] and r["choice"] is None),
                    "error": sum(1 for r in rs if r["err"]),
                    "within_4s": sum(1 for m in ms if m <= PRODUCT_TIMEOUT * 1000),
                    "p50_ms": pct(ms, .5), "p90_ms": pct(ms, .9)}
            if t.startswith("priority"):
                s[t]["picked_first"] = sum(1 for r in rs if r["choice"] == r["first"])
        summary[model] = s
        print(f"\n== {model}(読み込み込みの初回 {warm['ms']}ms)")
        for t, v in s.items():
            if t == "warm_ms":
                continue
            extra = f"・先頭を選んだ {v['picked_first']}" if "picked_first" in v else ""
            print(f"  {t:18} 一致 {v['agree']}/{v['n']}・答えが壊れた {v['invalid']}・失敗 {v['error']}・"
                  f"4 秒以内 {v['within_4s']}/{v['n']}・p50 {v['p50_ms']}ms p90 {v['p90_ms']}ms{extra}")
    log.close()
    # 規則の側(比べる相手): client は規則に当たらない限り何も付けない、priority は並べた順の先頭
    print("\n== 規則(比べる相手): client は場所で当たるものを当て、当たらないものは付けない(= 定義上 一致)。"
          "priority は並べた順の先頭(= 定義上 一致)。遅れ 0ms")
    json.dump({"at": stamp, "url": a.url, "n": a.n, "summary": summary},
              open(os.path.join(outdir, f"bench-{stamp}.summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n明細: {outdir}/bench-{stamp}.jsonl")


if __name__ == "__main__":
    main()
