#!/usr/bin/env python3
"""clients.py — その作業が「どの顧客のプロダクトか」を判定する(1か所に集約)。

使う側: ~/.claude/hooks/tab-status.py(タブの色とバッジ) / cs / 全体像ダッシュボード。
ルールは ~/.claude/tools/clients.json。同じ判定を各所に書かないこと。

  classify(cwd="", account="", texts=()) -> dict | None
    判定順: ①フォルダ ②Claudeアカウント ③依頼文・話題の言葉
    返り値: {"id","label","emoji","rgb","by"}  by は何で当たったか(path/account/keyword:<語>)

  python3 clients.py <cwd> [account] [text...]   で手元確認できる。
"""
import json
import os
import sys

import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import aiboard_paths  # noqa: E402
RULES = aiboard_paths.clients_file()
_cache = None


def rules():
    global _cache
    if _cache is None:
        with open(RULES, encoding="utf-8") as f:
            _cache = json.load(f)["clients"]
    return _cache


def classify(cwd="", account="", texts=()):
    cwd = cwd or ""
    for c in rules():
        for p in c.get("paths", []):
            if p and p in cwd:
                return _hit(c, "path")
    for c in rules():
        if account and account in c.get("accounts", []):
            return _hit(c, "account")
    # 言葉での判定は「1つの顧客だけ」が出てくるときに限る。
    # 「A社、B社、C社 は顧客」のように複数の顧客名を並べた文は、
    # その顧客の作業ではなく顧客の話をしているだけなので判定しない(2026-09-17 誤判定の実例)。
    hits = []
    for t in texts:
        if not t:
            continue
        low = t.lower()
        found = {c["id"]: (c, k) for c in rules() for k in c.get("keywords", []) if k and k.lower() in low}
        if len(found) == 1:
            hits.append(next(iter(found.values())))
    if hits:
        c, k = hits[0]
        return _hit(c, f"keyword:{k}")
    return None


def _hit(c, by):
    return {"id": c["id"], "label": c["label"], "emoji": c.get("emoji", ""),
            "rgb": c.get("rgb"), "by": by}


if __name__ == "__main__":
    a = sys.argv[1:]
    print(classify(a[0] if a else "", a[1] if len(a) > 1 else "", a[2:]))
