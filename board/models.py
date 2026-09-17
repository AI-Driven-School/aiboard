#!/usr/bin/env python3
"""models.py — モデルID から「表示名・絵文字・色」を引く(1か所に集約)。

使う側: ~/.claude/hooks/tab-status.py / cs / 全体像ダッシュボード。表は ~/.claude/tools/models.json。

  style("claude-fable-5-1") -> {"label":"Fable 5.1","short":"Fable5.1","vendor":"Claude","emoji":"🟣","rgb":[..],"id":...}
  python3 models.py <model-id>   で手元確認できる。
"""
import json
import os
import re
import sys

TABLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
_cache = None


def table():
    global _cache
    if _cache is None:
        with open(TABLE, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def style(model_id):
    mid = (model_id or "").lower()
    if mid and mid != "<synthetic>":
        for m in table()["models"]:
            if re.search(m["pattern"], mid):
                return {**{k: v for k, v in m.items() if k != "pattern"}, "id": model_id}
    return {**table()["unknown"], "vendor": "", "id": model_id or ""}


if __name__ == "__main__":
    for a in sys.argv[1:] or ["claude-opus-5", "claude-fable-5-1", "gpt-6-astra", "<synthetic>"]:
        print(a, "→", style(a))
