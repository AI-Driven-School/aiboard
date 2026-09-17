"""run_uat.py の試験一覧から docs/uat/test-cases.md を作る(実データは含めない)。"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("run_uat", os.path.join(ROOT, "scripts", "uat", "run_uat.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
groups = {"SV": "盤サーバ(防御・起動・更新)", "LP": "loop / skill / MCP の判定", "LM": "上限", "HK": "hook の設置", "ST": "設定", "MM": "メモリ・止める",
          "I18N": "言語", "BD": "盤の表示", "CV": "会話ビュー", "AP": "アプリ(macOS)"}
how = {"SV": "API を直接叩く", "LP": "関数を実データ・合成データで呼び、別実装の数え上げと照合", "LM": "関数を呼ぶ・実ログと照合", "HK": "一時の settings.json に対して実行",
       "ST": "画面と API と CLI(claude mcp list)を照合", "MM": "画面操作(送信は横取り)・dry・使い捨てプロセス", "I18N": "ソースの静的検査", "BD": "Playwright で画面を開き API と照合",
       "CV": "Playwright(送信は横取り)", "AP": "ビルドしたアプリを試験用ポートで起動し、ページ内で JS を実行(送信は差し替え)"}
L = ["# AIBoard 受け入れ試験(UAT)テストケース", "",
     "実行: `python3 scripts/uat/run_uat.py`(自動分)。結果は `~/aiboard-private/uat/` に書く(実セッションの題名を含むためリポジトリに入れない)。",
     "この表は `python3 scripts/uat/make_cases_doc.py` で試験コードから作る。", "",
     "## 試験の約束", "",
     "- 本番の盤(8791 番・`~/.aiboard`)には触れない。盤サーバは 8793 番・一時データ置き場で動かす",
     "- 実セッションへは何も送らない。送信・終了・再開・端末移動は横取り(ブラウザ)か差し替え(アプリ)",
     "- 「終了」の実シグナルは、試験が自分で起こした使い捨てプロセスにだけ送る。実セッションへは dry(送り先の解決だけ)",
     "- 判定は画面の文字でなく、API・ps・会話ログの別実装の数え上げと突き合わせる", "",
     f"## 自動({len(m.CASES)} 件)", "", "| ID | 区分 | 確認すること | 方法 |", "|---|---|---|---|"]
for fn in m.CASES:
    g = fn.cid.split("-")[0]
    L.append(f"| {fn.cid} | {groups.get(g, g)} | {fn.title} | {how.get(g, '')} |")
L += ["", f"## 手動({len(m.MANUAL)} 件・実機で本人が確認)", "", "自動で確かめられないもの(実際の日本語入力・通知の配信・キーボードの入力先・実セッションへの送信と終了・実ログインでのアカウント切替)。", "",
      "| ID | 手順と期待 |", "|---|---|"] + [f"| {i} | {t} |" for i, t in m.MANUAL]
open(os.path.join(ROOT, "docs", "uat", "test-cases.md"), "w").write("\n".join(L) + "\n")
print(len(m.CASES), "auto /", len(m.MANUAL), "manual")
