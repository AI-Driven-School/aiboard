"""状態の真理値表 — 「いまどう見せ、何を鳴らし、次の一手は何か」を 1 か所で決める。

散らばった if を 1 つの表にまとめたもの。表は上から順に見て、**最初に当たった 1 行**が答え（優先順位つき決定表）。
**カードの色・状態名・印・音・小窓・Dock・通知・「あなたの番」の集計は、全部この答えを読むだけ**（docs/state-spec.md 0 節）。
2026-09-23 までは印と自動処理しかここを読んでおらず、ログイン切れなのに音も小窓も出ない等、同じセッションで答えが割れていた。
仕様: docs/state-spec.md（期待値の正本）。文書 docs/state-truth-table.md はこの表から生成する。
試験: SM-01 が scripts/uat/state_oracle.py（仕様から別に書いた期待値）と全組み合わせを突き合わせる。DT-01 は文書との一致。

入力（セッション 1 本について、すべて独立に観測できるもの）:
  proc        : AI のプロセスか背景エージェントが居るか   True/False
  stop        : 記録の末尾の止まり方   ""|five_hour|seven_day|overage|limit_reset|credits|login|oauth|expired|apikey|transient|error
  hook        : 上書き後の最終の状態   ""|working|waiting|replied
  loop        : /loop が次に自分で起きるか   True/False
  trust       : フォルダ信頼の確認     None|suspect(設定から推した)|seen(画面で確かめた)
  idle        : 記録を持たない CLI の、端末の出力が止まってからの秒数   数値/None
  stale       : いまの状態が続いている秒数   数値/None
  answerable  : 盤から 1/2/Esc を送れるか   True/False
  others      : 同じ場所で動いている他の本数   0,1,2...

出力: kind(色と状態名の種類) state badge sound popup dock notify attn action why auto parallel
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

LIMITS = ("five_hour", "seven_day", "overage")
AUTHS = ("login", "oauth", "expired")

# 各行の知らせ方。A=すぐ人が要る(音・小窓・Dock・通知・集計の先頭) / Q=静か
_A = {"sound": True, "popup": True, "dock": True, "notify": True, "attn": 0}
_Q = {"sound": False, "popup": False, "dock": False, "notify": False, "attn": None}


def _ch(base, **kw):
    return dict(base, **kw)


# 上から順に、最初に当たった行が答え
#   (名前, 条件, kind, state, badge, 知らせ方, action, why)
ROWS = [
    ("認証: 鍵が無効", lambda s: s["stop"] == "apikey",
     "auth", "止まっている", "auth", _A, "key", "ログインし直しても直らない（鍵の設定の問題）"),
    ("認証: ログイン切れ", lambda s: s["stop"] in AUTHS,
     "auth", "止まっている", "auth", _A, "login", "放っておくと永久に進まない（989 件中 63 件しか再開されなかった）"),
    ("クレジット切れ", lambda s: s["stop"] == "credits",
     "billing", "止まっている", "billing", _A, "billing", "時間では戻らないので待たせない"),
    ("エラーで停止", lambda s: s["stop"] == "error",
     "error", "停止", "err", _ch(_A, popup=False), "resume", "エラーで終わった。答える問いは無いので小窓は出さない"),
    ("判断待ち", lambda s: s["proc"] and s["hook"] == "waiting" and s["answerable"],
     "need", "確認待ち", "need", _A, "answer", "人が答えないと 1 文字も進まない。古い上限より優先する"),
    ("判断待ち（盤から答えられない）", lambda s: s["proc"] and s["hook"] == "waiting",
     "need", "確認待ち", "need", _ch(_A, popup=False), "look", "答えられない口は出さない"),
    ("信頼の確認", lambda s: s["proc"] and s["trust"] == "seen",
     "need", "確認待ち", "need", _A, "trust", "画面で確かめた信頼の確認は判断待ちと同じ"),
    ("上限", lambda s: s["stop"] in LIMITS,
     "limited", "上限", "lim", _Q, "move_or_wait", "時間が解く。移すか待つかの二択だけ出す"),
    ("上限が明けた", lambda s: s["stop"] == "limit_reset",
     "resumable", "返答待ち", "turn", _ch(_Q, notify=True, attn=1), "reply", "明けた時に 1 回だけ知らせる（明けてから戻るまで中央値 79 分）"),
    ("終わっている", lambda s: not s["proc"],
     "ended", "終了", "", _Q, "resume", "端末は残っている。同じ会話を再開できる"),
    ("信頼の確認かも", lambda s: s["trust"] == "suspect" and not s["hook"],
     "need", "起動中?", "need", _ch(_Q, attn=2), "trust", "推測だけなので断定しない。答える口と印だけ出す"),
    ("一時的な失敗が長引いている", lambda s: s["stop"] == "transient" and (s["stale"] or 0) >= 600,
     "yourturn", "返答待ち", "turn", _ch(_Q, attn=0), "reply", "10 分動かない。「続けて」と送らないと進まない"),
    ("一時的な失敗", lambda s: s["stop"] == "transient",
     "work", "作業中", "", _Q, "none", "自分で再試行することが多い。赤くすると狼少年になる"),
    ("作業中", lambda s: s["hook"] == "working",
     "work", "作業中", "", _Q, "none", "AI の番。触らない"),
    ("ループ待機", lambda s: s["hook"] == "replied" and s["loop"],
     "loop", "返答待ち", "", _Q, "none", "次に自分で起きるので、あなたの番ではない"),
    ("あなたの番", lambda s: s["hook"] == "replied",
     "yourturn", "返答待ち", "turn", _ch(_Q, attn=1), "reply", "返事を書くまで止まっている。印だけで、音や通知は出さない"),
    ("記録なしの CLI: 出力が続く", lambda s: not s["hook"] and s["idle"] is not None and s["idle"] < 5,
     "work", "作業中", "", _Q, "none", "状態の記録が無い CLI は、端末の出力で見分ける"),
    ("記録なしの CLI: 出力が止まった", lambda s: not s["hook"] and s["idle"] is not None and s["idle"] >= 30,
     "idle", "出力が止まっている", "look", _ch(_Q, attn=2), "look", "30 秒出ていない。こちらの番かもしれないが断定しない"),
    ("起動中", lambda s: True,
     "starting", "起動中?", "", _ch(_Q, attn=2), "look", "記録がまだ無いだけのことがある。10 秒は待つ"),
]

# 画面に出す状態名(カード・TUI・見張り・会話ビューが同じ名前を使う)と、行の印の絵文字。どちらも表の答えの一部
LABEL = {"認証: 鍵が無効": "API キーが無効", "認証: ログイン切れ": "ログイン切れ", "クレジット切れ": "クレジット切れ", "エラーで停止": "停止",
         "判断待ち": "判断待ち", "判断待ち（盤から答えられない）": "判断待ち", "信頼の確認": "信頼の確認", "上限": "上限",
         "上限が明けた": "上限が明けた", "終わっている": "終了", "信頼の確認かも": "起動中", "一時的な失敗が長引いている": "あなたの番",
         "一時的な失敗": "作業中", "作業中": "作業中", "ループ待機": "ループ待機", "あなたの番": "あなたの番",
         "記録なしの CLI: 出力が続く": "作業中", "記録なしの CLI: 出力が止まった": "出力が止まっている", "起動中": "起動中"}
MARK = {"need": "🔴", "auth": "🔴", "billing": "🔴", "error": "🔴", "limited": "🟣", "resumable": "🟡", "yourturn": "🟡",
        "loop": "🔵", "work": "🟢", "idle": "🔵", "starting": "🔵", "ended": "⚪"}
assert set(LABEL) == {r[0] for r in ROWS}

FIELDS = ("proc", "stop", "hook", "loop", "trust", "idle", "stale", "answerable", "others")
DEFAULTS = {"proc": True, "stop": "", "hook": "", "loop": False, "trust": None, "idle": None, "stale": None,
            "answerable": True, "others": 0}


def auto_for(action):
    """その一手をシステムが代わりにやれるか。やれるなら自動処理の名前、やれないなら ""。"""
    return AUTOABLE.get(action, "")


def decide(sess):
    """セッション 1 本について、表を上から見て最初に当たった行を返す。"""
    s = {k: sess.get(k, DEFAULTS[k]) for k in FIELDS}
    for name, cond, kind, state, badge, ch, action, why in ROWS:
        try:
            hit = cond(s)
        except (TypeError, KeyError):
            hit = False
        if hit:
            out = {"row": name, "kind": kind, "state": state, "label": LABEL[name], "mark": MARK[kind], "badge": badge, **ch,
                   "action": action, "action_label": ACTIONS[action], "why": why, "auto": auto_for(action)}
            break
    # 並行は状態でなく見せ方: どの行でも、同じ場所に他が居れば印を足す
    out["parallel"] = int(s["others"] or 0)
    return out


CONDS = [   # ROWS と同じ順。文書の「当たる条件」列
    "記録の末尾が「API キーが無効」", "記録の末尾が「未ログイン / OAuth 失効 / 期限切れ」", "記録の末尾が「クレジット切れ」",
    "Codex の ⛔/⏹・背景エージェントの failed", "プロセスが居て、hook が「判断待ち」、盤から答えられる",
    "プロセスが居て、hook が「判断待ち」、盤からは答えられない（背景エージェント）", "プロセスが居て、画面に信頼の確認が出ている",
    "記録の末尾が「上限（5 時間 / 7 日 / 超過）」", "解除時刻を過ぎたのに、記録の末尾が上限のまま", "プロセスが居ない",
    "記録が無く、設定から信頼の答えが無いと推した", "記録の末尾が「一時的な失敗」で 10 分以上そのまま", "記録の末尾が「一時的な失敗」",
    "hook が「作業中」", "hook が「返答済み」かつ /loop 待機", "hook が「返答済み」",
    "記録が無い CLI で、出力が 5 秒以内", "記録が無い CLI で、出力が 30 秒以上止まった", "それ以外（記録がまだ無い）",
]
assert len(CONDS) == len(ROWS)


def table_markdown():
    """この表から文書を作る(docs/state-truth-table.md)。文書とコードがずれないように。"""
    L = ["# 状態の真理値表（board/decide.py から生成・手で書き換えない）", "",
         "上から順に見て、**最初に当たった 1 行**が答え。入力はすべて独立に観測できるものだけ。",
         "カードの色・状態名・印・音・小窓・Dock・通知・「あなたの番」の集計は、全部この答えを読む。仕様と根拠は docs/state-spec.md。", "",
         "| # | 当たる条件 | 状態 | 印 | 音 | 小窓 | Dock | 通知 | 次の一手 | なぜ |", "|---|---|---|---|---|---|---|---|---|---|"]
    mark = {"auth": "🔑", "need": "!", "lim": "⏸", "turn": "●", "billing": "¥", "err": "⛔", "look": "·", "": "—"}
    yn = lambda v, w: w if v else "—"
    for i, ((name, _, kind, state, badge, ch, action, why), c) in enumerate(zip(ROWS, CONDS), 1):
        L.append(f"| {i} | {c} | {state} | {mark.get(badge, badge)} | {yn(ch['sound'], '鳴らす')} | {yn(ch['popup'], '出す')} | "
                 f"{yn(ch['dock'], '数える')} | {yn(ch['notify'], '出す')} | {ACTIONS[action] or '—'} | {why} |")
    L += ["", "音をオフにすると、盤の音も通知の音も止まる。", "",
          "## システムが自分でやること（既定は全部オフ）", "",
          "| 一手 | 自動でやれるか | 中身 |", "|---|---|---|",
          "| 別アカウントに移す / 解除時刻に自動で続ける | **やれる**（設定でオン） | 解除時刻＋60 秒に同じ会話を `--resume` で開く予約を 1 つ作る。予約ができた時だけ「自動で続きます」と出す |",
          "| ログインし直す / 鍵の棚 / 請求 | やれない | 人しかできない（鍵も課金も AIBoard は触らない） |",
          "| 1 / 2 / Esc・信頼の確認・返事 | やれない | 判断そのものなので、勝手に答えない |",
          "| 同じ会話を再開・端末を見る | やれない | 人が見たい時に見る |",
          "", "やったことは `~/.aiboard/actions.jsonl` に全部残り、盤の「やったこと」で読める。", "",
          "## 重なった時（この順で勝つ）", "",
          "認証 → クレジット → エラー → 判断待ち → 信頼の確認 → 上限 → 上限明け → 終了 → 信頼かも → 一時的な失敗 → 作業中 → ループ → あなたの番 → 出力の有無 → 起動中", "",
          "理由: **人が直さないと永久に進まないもの**を上に置く。判断待ちは古い上限より上（上限の記録は本物の返答が来ると解けるので、重なって見えるのは上限が古い時）。", "",
          "## 並行（状態ではなく見せ方）", "",
          "同じ場所で 2 本以上動いていれば、どの行でもカードに `⇉N` を足し、会話ビューに相棒を並べる。畳まない。", ""]
    return "\n".join(L)
