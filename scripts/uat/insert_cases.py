"""確認済みのケース(JSON)を run_uat.py に差し込む。

  python3 scripts/uat/insert_cases.py cases.json

cases.json は [{"id","title","code"}] の列。code は @case(...) から始まる完成した関数。
差し込み先は run_uat.py の「実行」区切りの直前。既にある id は差し替える(重複を作らない)。
構文が壊れていれば書き込まない。
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(ROOT, "scripts", "uat", "run_uat.py")
ANCHOR = "# ------------------------------------------------------------------ 実行"


def existing_ids(src):
    return set(re.findall(r'@case\("([A-Z0-9-]+)"', src))


def block_of(src, cid):
    """既にある同じ id の関数ブロック(前の空行含む)を返す。無ければ None。"""
    m = re.search(r'\n\n@case\("' + re.escape(cid) + r'"[\s\S]*?(?=\n\n@case\(|\n\n# ---|\Z)', src)
    return m


def main():
    cases = json.load(open(sys.argv[1], encoding="utf-8"))
    src = open(TARGET, encoding="utf-8").read()
    assert src.count(ANCHOR) == 1, "差し込み先が見つからない"
    added, replaced = [], []
    for c in cases:
        code = c["code"].rstrip() + "\n"
        if not code.lstrip().startswith("@case("):
            raise SystemExit(f'{c["id"]}: @case( で始まっていない')
        ast.parse(code)   # 1 件ずつ構文を確かめる
        m = block_of(src, c["id"])
        if m:
            src = src[:m.start()] + "\n\n" + code.rstrip() + src[m.end():]
            replaced.append(c["id"])
        else:
            src = src.replace(ANCHOR, code + "\n\n" + ANCHOR, 1)
            added.append(c["id"])
    ast.parse(src)        # 全体の構文
    open(TARGET, "w", encoding="utf-8").write(src)
    print(json.dumps({"added": added, "replaced": replaced, "total_cases": len(existing_ids(src))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
