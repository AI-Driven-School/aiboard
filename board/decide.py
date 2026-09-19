"""状態の真理値表 — 「いまどう見せ、次の一手は何か」を 1 か所で決める。

散らばった if を 1 つの表にまとめたもの。表は上から順に見て、**最初に当たった 1 行**が答え（優先順位つき決定表）。
文書 docs/state-truth-table.md はこの表から生成する（二重管理をしない）。試験 DT-01 が全行を突き合わせる。

入力（セッション 1 本について、すべて独立に観測できるもの）:
  proc        : AI のプロセスが居るか            True/False
  stop        : 記録の末尾の止まり方             ""|five_hour|seven_day|overage|credits|login|apikey|oauth|expired|transient
  hook        : hook が言う状態                  ""|working|waiting|replied      ("" は記録なし)
  loop        : /loop が次に自分で起きるか        True/False
  trusted     : そのフォルダの信頼の答えがあるか   True/False/None(関係ない)
  idle        : 端末の出力が止まって何秒か         数値/None(記録を持つ CLI では見ない)
  others      : 同じ場所で動いている他の本数       0,1,2...

出力:
  state  : 画面の状態名        badge: カードの印   sound: 鳴らすか
  popup  : 最前面の小窓に出すか  action: 次の一手（1 つだけ）  why: なぜそう出すか
"""

# 一手の種類 → 画面のボタン（文言は overview.html 側の T() で訳す）
ACTIONS = {
    "answer": "1 / 2 / Esc で答える",
    "trust": "このフォルダを信頼しますか に答える",
    "login": "このアカウントでログインし直す",
    "key": "鍵の棚を開く（ログインでは直らない）",
    "billing": "請求のページを開く（時間では戻らない）",
    "move_or_wait": "別アカウントに移す / 解除時刻に自動で続ける",
    "reply": "返事を書く",
    "resume": "同じ会話を再開する",
    "look": "端末を見る",
    "none": "",
}

# システムが自分でやってよい一手(それ以外は人が押す)。
#   auto="resume_when_reset" だけが自動化できる。ログイン・請求・返事・信頼の確認は**人しかできない**
AUTOABLE = {"move_or_wait": "resume_when_reset"}

# 上から順に、最初に当たった行が答え
ROWS = [
    # (名前, 条件, state, badge, sound, popup, action, why)
    ("認証: 鍵が無効", lambda s: s["stop"] == "apikey",
     "止まっている", "auth", True, True, "key", "ログインし直しても直らない（鍵の設定の問題）"),
    ("認証: ログイン切れ", lambda s: s["stop"] in ("login", "oauth", "expired"),
     "止まっている", "auth", True, True, "login", "実測で最多(30 日 1,012 回)。放っておくと永久に進まない"),
    ("クレジット切れ", lambda s: s["stop"] == "credits",
     "止まっている", "billing", True, True, "billing", "時間では戻らないので待たせない"),
    ("上限", lambda s: s["stop"] in ("five_hour", "seven_day", "overage"),
     "上限", "lim", False, False, "move_or_wait", "時間が解く。移すか待つかの二択だけ出す"),
    ("判断待ち", lambda s: s["hook"] == "waiting",
     "確認待ち", "need", True, True, "answer", "人が答えないと 1 文字も進まない"),
    ("信頼の確認かも", lambda s: s["proc"] and not s["hook"] and s["trusted"] is False,
     "起動中?", "need", False, False, "trust", "画面を読めない端末では断定しない。答える口だけ出す"),
    ("一時的な失敗", lambda s: s["stop"] == "transient",
     "作業中", "", False, False, "none", "自動で戻る(30 日 102 回)。赤くすると狼少年になる"),
    ("作業中", lambda s: s["hook"] == "working",
     "作業中", "", False, False, "none", "AI の番。触らない"),
    ("ループ待機", lambda s: s["hook"] == "replied" and s["loop"],
     "返答待ち", "", False, False, "none", "次に自分で起きるので、あなたの番ではない"),
    ("あなたの番", lambda s: s["hook"] == "replied",
     "返答待ち", "turn", False, False, "reply", "返事を書くまで止まっている"),
    ("記録なしの CLI: 出力が続く", lambda s: s["proc"] and not s["hook"] and s["idle"] is not None and s["idle"] < 5,
     "作業中", "", False, False, "none", "状態の記録が無い CLI は、端末の出力で見分ける"),
    ("記録なしの CLI: 出力が止まった", lambda s: s["proc"] and not s["hook"] and s["idle"] is not None and s["idle"] >= 30,
     "返答待ち", "turn", False, False, "look", "30 秒出ていない＝こちらの番の可能性。断定はしない"),
    ("起動中", lambda s: s["proc"] and not s["hook"],
     "起動中?", "", False, False, "look", "記録がまだ無いだけのことがある。10 秒は待つ"),
    ("終わっている", lambda s: not s["proc"],
     "終了", "", False, False, "resume", "端末は残っている。同じ会話を再開できる"),
]

FIELDS = ("proc", "stop", "hook", "loop", "trusted", "idle", "others")
DEFAULTS = {"proc": True, "stop": "", "hook": "", "loop": False, "trusted": None, "idle": None, "others": 0}


def auto_for(action):
    """その一手をシステムが代わりにやれるか。やれるなら自動処理の名前、やれないなら ""。"""
    return AUTOABLE.get(action, "")


def decide(sess):
    """セッション 1 本について、表を上から見て最初に当たった行を返す。"""
    s = {k: sess.get(k, DEFAULTS[k]) for k in FIELDS}
    for name, cond, state, badge, sound, popup, action, why in ROWS:
        try:
            hit = cond(s)
        except (TypeError, KeyError):
            hit = False
        if hit:
            out = {"row": name, "state": state, "badge": badge, "sound": sound, "popup": popup,
                   "action": action, "action_label": ACTIONS[action], "why": why, "auto": auto_for(action)}
            break
    else:
        out = {"row": "(該当なし)", "state": "終了", "badge": "", "sound": False, "popup": False,
               "action": "none", "action_label": "", "why": "", "auto": ""}
    # 並行は状態でなく見せ方: どの行でも、同じ場所に他が居れば印を足す
    out["parallel"] = int(s["others"] or 0)
    return out


def table_markdown():
    """この表から文書を作る(docs/state-truth-table.md)。文書とコードがずれないように。"""
    L = ["# 状態の真理値表（board/decide.py から生成・手で書き換えない）", "",
         "上から順に見て、**最初に当たった 1 行**が答え。入力はすべて独立に観測できるものだけ。", "",
         "| # | 当たる条件 | 状態 | 印 | 音 | 小窓 | 次の一手 | なぜ |", "|---|---|---|---|---|---|---|---|"]
    conds = [
        "記録の末尾が「API キーが無効」", "記録の末尾が「未ログイン / OAuth 失効 / 期限切れ」",
        "記録の末尾が「クレジット切れ」", "記録の末尾が「上限（5 時間 / 7 日 / 超過）」",
        "hook が「判断待ち」", "プロセスは居る・記録が無い・信頼の答えも無い",
        "記録の末尾が「一時的な失敗」", "hook が「作業中」", "hook が「返答済み」かつ /loop 待機",
        "hook が「返答済み」", "記録が無い CLI で、出力が 5 秒以内", "記録が無い CLI で、出力が 30 秒以上止まった",
        "プロセスは居るが記録がまだ無い", "プロセスが居ない",
    ]
    mark = {"auth": "🔑", "need": "!", "lim": "⏸", "turn": "●", "billing": "¥", "": "—"}
    for i, ((name, _, state, badge, sound, popup, action, why), c) in enumerate(zip(ROWS, conds), 1):
        L.append(f"| {i} | {c} | {state} | {mark.get(badge, badge)} | {'鳴らす' if sound else '—'} | "
                 f"{'出す' if popup else '—'} | {ACTIONS[action] or '—'} | {why} |")
    L += ["", "## システムが自分でやること（既定は全部オフ）", "",
          "| 一手 | 自動でやれるか | 中身 |", "|---|---|---|",
          "| 別アカウントに移す / 解除時刻に自動で続ける | **やれる**（設定でオン） | 解除時刻＋60 秒に同じ会話を `--resume` で開く予約を 1 つ作る |",
          "| ログインし直す / 鍵の棚 / 請求 | やれない | 人しかできない（鍵も課金も AIBoard は触らない） |",
          "| 1 / 2 / Esc・信頼の確認・返事 | やれない | 判断そのものなので、勝手に答えない |",
          "| 同じ会話を再開・端末を見る | やれない | 人が見たい時に見る |",
          "", "やったことは `~/.aiboard/actions.jsonl` に全部残り、盤の「やったこと」で読める。", ""]
    L += ["", "## 重なった時（この順で勝つ）", "",
          "認証 → クレジット → 上限 → 判断待ち → 信頼 → 一時的な失敗 → 作業中 → ループ → あなたの番 → 出力の有無 → 起動中 → 終了", "",
          "理由: **人が直さないと永久に進まないもの**を上に置く。時間や AI 自身が解くものほど下。", "",
          "## 並行（状態ではなく見せ方）", "",
          "同じ場所で 2 本以上動いていれば、どの行でもカードに `⇉N` を足し、会話ビューに相棒を並べる。畳まない。", ""]
    return "\n".join(L)
