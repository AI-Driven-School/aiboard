# セッションの状態の仕様（期待値の正本）

2026-09-23。Claude（調査サブエージェント）と Codex（`codex exec -s read-only`）が**別々に**コードを読んで変数と出力を洗い出し、
食い違い・判断が割れた所を Jev に A/B で 2 回ずつ聞いて決めた（対照問題の正答 0.97 / 0.98）。
試験 `scripts/uat/state_oracle.py` はこの文書から**decide.py とは別に**書いた期待値で、`board/decide.py` の全組み合わせを照合する（SM-01）。

## 0. 大原則（両者が独立に同じ結論）

表（`board/decide.py`）の答えを**上書きがすべて済んだ後で 1 回だけ**出し、
カードの色・状態名・印・一言・音・小窓・Dock の数字・通知・「あなたの番」の集計・会話ビューの主な一手は、**全部その答えを読むだけ**にする。
以前は印と自動処理だけが表を読み、ほかは各所が `state` 文字列から自分で決めていたため、同じセッションで答えが割れていた
（例: ログイン切れなのに音も小窓も出ない／一時的なエラーが「ログインが切れている」で最優先に数えられる）。

## 1. 入力変数（セッション 1 本。すべて独立に観測できる）

| 変数 | 値域 | 観測元 |
|---|---|---|
| proc | True / False | AI のプロセス（pid）か背景エージェントが居るか |
| stop | "" / five_hour / seven_day / overage / limit_reset / credits / login / oauth / expired / apikey / transient / error | 記録の末尾の止まり方。limit_reset = 解除時刻を過ぎたのに上限の記録が末尾のまま。error = Codex の ⛔/⏹・背景エージェントの failed |
| hook | "" / working / waiting / replied | hook・公式の状態（上書き後の最終値） |
| loop | True / False | /loop が次に自分で起きるか（cron の予約は含めない — Jev 0.55 で五分のため現状維持） |
| trust | None / suspect / seen | フォルダ信頼の確認。seen = 画面の文字で確かめた、suspect = 設定ファイルから推した |
| idle | None / 秒 | 記録を持たない CLI だけ。端末の出力が止まってからの秒数 |
| stale | None / 秒 | いまの状態が続いている秒数（一時的な失敗が長引いたかの判断）。**None（分からない）は長引いていないとみなす**（分からないのに騒がない: P3/P4） |
| answerable | True / False | 盤から 1/2/Esc を送れるか（背景エージェントは送れない） |
| others | 0,1,2… | 同じ持ち場で動いている他の本数（見せ方だけ。状態は変えない） |

## 2. 出力チャネル（全部 decide の答え 1 つから）

| 出力 | 意味 | 読む所 |
|---|---|---|
| kind | カードの色と状態名の種類: need / auth / billing / error / limited / resumable / yourturn / loop / work / idle / starting / ended | overview.html（カード・レール） |
| state | 状態名（日本語） | 遠隔の行・文書 |
| badge | カードの印: need(!) / auth(🔑) / billing(¥) / err(⛔) / lim(⏸) / turn(●) / look(·) / "" | overview.html |
| sound | 鳴らすか（入った瞬間 1 回） | main.swift |
| popup | 最前面の小窓を出すか | main.swift |
| dock | Dock の数字に数えるか（人が動かないと進まないもの） | main.swift |
| notify | OS 通知を出すか（入った瞬間 1 回） | main.swift |
| attn | 「あなたの番」の集計の順位: 0 すぐ / 1 放置されたら / 2 起動待ち・推測（最後に並べる） / None 数えない | overview.py attention() |
| action | 次の一手（1 つ） | 会話ビュー・小窓 |
| auto | システムが代わりにやれる一手（resume_when_reset だけ） | autopilot.py |

音をオフにしたら、盤の音も通知の音も止める（Jev 0.77/0.78）。

## 3. 状態の同値クラスと理想の動き（上から順に最初に当たった 1 行）

| # | 条件 | kind | badge | 音 | 小窓 | Dock | 通知 | attn | 一手 | 根拠 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | stop=apikey | auth | auth | ○ | ○ | ○ | ○ | 0 | key | 人が直さないと永久に進まない（Jev 0.59/0.67・Claude・Codex） |
| 2 | stop∈login/oauth/expired | auth | auth | ○ | ○ | ○ | ○ | 0 | login | 同上。989 件中 63 件しか再開されていない |
| 3 | stop=credits | billing | billing | ○ | ○ | ○ | ○ | 0 | billing | 時間では戻らない |
| 4 | stop=error | error | err | ○ | — | ○ | ○ | 0 | resume | エラーで終わった。答える問いが無いので小窓は出さない |
| 5 | proc かつ hook=waiting かつ answerable | need | need | ○ | ○ | ○ | ○ | 0 | answer | 答えないと 1 文字も進まない。**古い上限より優先**（Jev 0.81/0.81） |
| 6 | proc かつ hook=waiting（盤から答えられない） | need | need | ○ | — | ○ | ○ | 0 | look | 答えられない口は出さない |
| 7 | proc かつ trust=seen | need | need | ○ | ○ | ○ | ○ | 0 | trust | 画面で確かめた信頼の確認は判断待ちと同じ（Jev 0.95/0.96） |
| 8 | stop∈five_hour/seven_day/overage | limited | lim | — | — | — | — | None | move_or_wait | 時間が解く。当たった時は静か（Jev 0.95） |
| 9 | stop=limit_reset | resumable | turn | — | — | — | ○ | 1 | reply | 明けた時に 1 回だけ知らせる（Jev 0.95/0.94）。明けてから戻るまで中央値 79 分 |
| 10 | not proc | ended | — | — | — | — | — | None | resume | プロセスが居なければ終了（Jev 0.52/0.60・docs/continuity-ux.md）。認証・請求・エラー・上限は上で残す |
| 11 | trust=suspect かつ hook="" | need | need | — | — | — | — | 2 | trust | 推測だけなら印のみ（Jev 0.95） |
| 12 | stop=transient かつ（stale<600 または stale=None） | work | — | — | — | — | — | None | none | 自分で再試行することが多い。狼少年にしない |
| 13 | stop=transient かつ stale≥600 | yourturn | turn | — | — | — | — | 0 | reply | 10 分動かなければ人の番（Jev 0.62/0.66） |
| 14 | hook=working | work | — | — | — | — | — | None | none | AI の番 |
| 15 | hook=replied かつ loop | loop | — | — | — | — | — | None | none | 次に自分で起きる |
| 16 | hook=replied | yourturn | turn | — | — | — | — | 1 | reply | 返答済みは印だけ。端末の種類で扱いを変えない（Jev 0.81/0.83） |
| 17 | hook="" かつ idle<5 | work | — | — | — | — | — | None | none | 記録の無い CLI は出力で見分ける |
| 18 | hook="" かつ idle≥30 | idle | look | — | — | — | — | 2 | look | 断定しない。弱い印と「端末を見る」だけ（Jev 0.96/0.97） |
| 19 | それ以外（proc あり・記録なし） | starting | — | — | — | — | — | 2 | look | 記録がまだ無いだけのことがある |

attn=1 の行（返答済み・上限明け）は、15 分以上そのままなら「あなたの番」の集計に入れる（以前からの基準）。上限明けは**解除時刻から**測る（元の状態が始まった時刻ではない）。

表の答えを持たない古い盤サーバが相手の時だけ、画面・アプリは以前の決め方（state 文字列）に落ちる。この互換の道は 1 か所ずつ（overview.html の cardKey、main.swift の legacyUI、mobile.html、overview_tui.py の needs_you）に閉じ込める。

## 4. 自動で続ける（上限）の約束

- 「⟳ HH:MM に自動で続きます」と言うのは**予約が実際にできた時だけ**（Jev 0.01/0.01 で「オンなら言う」を否定）。
  オンなのに予約できない時は理由を出す（会話の id が取れない／解除時刻が分からない／予約の保存に失敗）
- 解除時刻を過ぎて上限の表示が消えても、予約がまだ走っていなければ「⟳ HH:MM に自動で続きます」を出し続ける

状態名（label）と印の絵文字（mark）も表の答えの一部（decide.py の LABEL / MARK）。盤・TUI・見張り・会話ビュー・判定器は同じ名前と印を使う。

`cs`（board/cs.py の一覧）は、端末から見えたもの（観測）をそのまま出す道具で、表の判断の**前段**。ログイン切れ・上限の情報を持たないので、表の答えは出さない（ここで旧い state が見えるのは仕様）。

## 5. この回で扱わないもの（別途）

予約の実行側の信頼性（元のアカウントで開く・Codex に依頼文を渡す・実行前に状態を確かめ直す・起動失敗の記録）、
遠隔（macmini）のスキーマ、埋め込み端末への送信の宛先確認。どれも Codex/Claude の指摘として `tasks/todo.md` に残す。
