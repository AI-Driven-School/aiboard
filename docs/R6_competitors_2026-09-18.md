# 競合比較（MECE・取得日 2026-09-18）— 調査エージェントの報告を保存

軸: A 対象（A1 対応エージェント／A2 過去の閲覧・再開／A3 人のタスク）・B 見る（B1 表示形式／B2 顧客・案件の束ね／B3 関係表示）・C 気づく（C1 判断待ち検知／C2 OS 通知・Dock／C3 リソース表示）・D 触る（D1 端末内蔵／D2 端末エンジン／D3 worktree／D4 既存 iTerm/tmux の取り込み）・E 作り（E1 基盤／E2 OS／E3 ライセンス／E4 価格／E5 ★・最終 push／E6 データ・テレメトリ／E7 ビジネスモデル／E8 規模）・F 市場（F1 ローンチ反応／F2 継続性）

## 表2（作り・ビジネスモデル・規模）
| 製品 | 基盤 | OS | ライセンス | 価格 | ★ / push | モデル | 規模（公開値） | ローンチ | 状態 |
|---|---|---|---|---|---|---|---|---|---|
| cmux | Swift/AppKit | Mac | GPL-3.0＋商用 | 無料 | 27,190 / 09-17 | OSS＋商用ライセンス | brew 3,200/30日・DL 2,362万・Discord 1,993・調達$500K(二次) | HN 198pt | Manaflow (YC S24) |
| Warp | Rust | 3OS | AGPL(UI MIT) | 無料/$20/$200/$50 | 65,068 | OSS＋有料 | brew 4,497・ARR $16M・調達$75.1M(Latka 二次) | HN 946pt | 継続 |
| Ghostty(基準) | Zig＋Swift | Mac/Linux | MIT | 無料 | 61,224 | OSS | brew 20,541・Discord 29,920 | HN 2,319pt | 非営利化 |
| Zed | Rust | 3OS | 不明 | 不明 | 90,418 | 不明 | brew 5,579 | 1.0 04-29 | 継続 |
| Claude Desktop / Agent View | 不明 | Mac/Win/Linux β | 独自 | Pro以上 | ― | 契約に内包 | brew(claude) 10,458 | ― | Anthropic |
| Codex app→ChatGPT.app | 不明 | 3OS | 独自 | 全プラン | ― | 契約に内包 | 初週100万DL(VentureBeat) | HN 805pt | 07-09 統合 |
| Conductor | 不明 | Mac | 非公開 | 無料/$50/$60 | ― | 無料＋有料 | brew 243(推測) | HN 228pt | 継続 |
| Nimbalyst(旧Crystal) | Electron | 3OS＋モバイル | MIT | 無料 | 1,731 | OSS無料 | DL 635万・Discord 899 | HN 8pt | 継続 |
| Vibe Kanban | Rust＋Web | 任意 | Apache-2.0 | 無料 | 28,112 | OSS(クラウド終了) | 30,000 MAU・Discord 2,226 | HN 195pt | 04-10 事業終了 |
| Switchboard | Electron | 3OS | MIT | 無料 | 338 | OSS | DL 12.5万 | 不明 | 継続 |
| agentboard | Web(Bun) | Mac/Linux | MIT | 無料 | 415 | OSS | brew 9 | 不明 | 個人 |
| Xum(coder) | Electron | Mac/Linux | AGPL | 無料 | 2,024 | OSS | DL 7.8万 | HN 100pt | Coder 社 |
| claude-squad | Go TUI | Mac/Linux | AGPL | 無料 | 8,488 | OSS | brew 317・DL 2.0万 | HN 5pt | 鈍化 |
| termcanvas | Electron | 3OS | MIT | 無料 | 400 / 05-31 | OSS | DL 7.5万 | HN 3pt | 3.5か月停止 |
| termscape | Node＋WebView | 3OS | MIT | 無料 | 0 | OSS | 不明 | 不明 | 個人 |
| Nyx | 不明 | Mac/Win | 非公開 | $29 買い切り | ― | 有料のみ | 非公開 | X | 個人 |
| **Maestri** | **Swift/SwiftUI** | Mac | 非公開 | 無料/$18 買い切り | ― | 無料＋有料 | 非公開 | PH 180票・日間8位 | 個人 |

## 追記（2026-09-18 夕・本人共有 https://agi-labo.com/tools/cockpit）

**AGI Cockpit**（日本語・商用・3OS: Windows / macOS Apple Silicon / Linux）。公開ページに書いてある範囲のみ:

| 軸 | Cockpit | AIBoard |
|---|---|---|
| 見せ方 | **タスク一覧**（実行中・待機中・完了）。チャット応答・ファイル差分・プレビュー | セッションのカード（canvas）。会話ビュー＋本物の端末 |
| 対応 AI | Claude Code / Codex / Antigravity / Cursor / Grok Build＋OpenRouter・LM Studio | Claude・Codex は状態つき。Gemini/Grok/Cursor は在席のみ |
| 判断待ち | **`cockpit ask`＝最前面に浮かぶ小窓**でボタン or 自由入力。外出先はスマホでも | Dock の数字＋OS 通知（**通知を切られると無音**＝今日の実測） |
| 自動実行 | Autorun（時刻・間隔を指定して起動） | 既存の /loop・cron を**読んで表示**するだけ（作れない） |
| まとめ役 | Master Agent が仕事を分解し「向いている AI に任せて 5 つ並列」、進捗を集約 | 「任せる」＝上限でない AI を 1 本起こす＋結果の突き合わせ（今日追加） |
| 遠隔 | PWA でタスク追加・追加指示・画像添付・ask への応答 | 無し（ローカル完結が売り） |
| 価格 | ローカルは無料・無制限。**Autorun とリモートだけ $20/月 or $200/年** | MIT・全部無料 |
| 既存セッション | 「普段の CLI もそのまま」とだけ。取り込みの記述は無し | iTerm のタブを tty で拾い、右の端末へ移せる |
| 状態の取り方 | **記載なし** | Claude の hook・Codex の rollout・`claude agents --json` |

読み取れること:
1. **同じ痛み（どれが自分を待っているか）に、別の入口で答えている**。向こうは「タスクを作らせる」、AIBoard は「既にあるセッションを拾う」。ここは正面衝突しない。
2. **有料になっているのは Autorun とリモートの 2 つだけ**＝そこに金を払う人がいる、という他人の実証。AIBoard は両方持っていない（リモートはローカル完結の方針と衝突する）。
3. **`cockpit ask` の最前面小窓**は、今日の実測（この Mac は通知が denied で 1 通も出ていなかった）への直接の答えになっている。通知は利用者が切れるが、**自前の小窓は切られない**。
4. 状態の検出方法を公開していない＝「どうやって知るのか」は AIBoard の説明資産（hook・rollout・公式の agents）として使える。

## 追記2（2026-09-18 夜・本人共有: TypeSafe「Jev」）

**未検証**（早期アクセスの待ち行列。手元で 1 回も叩いていない）。公開情報だけを写す:
- 「決めるだけ」のモデル（System One）。自由文でなく**型の付いた選択**を返す。用途は routing・分類・アプリ内の判断点
- OpenRouter 掲載値: 入力 $0.042/M・**出力 $0.00/M**、遅延 70〜500ms（[OpenRouter](https://openrouter.ai/typesafe/jev-1.13)）
- 事例として LLM-as-a-judge、オセロの着手選択、DOOM を毎秒 ~10 推論・約 $7/時（[The Register](https://www.theregister.com/ai-and-ml/2026/09/16/typesafe_ai_debuts_model_for_machines/)）

AIBoard で当てはまる所（すべて「選ぶ」仕事。生成は要らない）:
1. **束ね方**（このセッションはどの持ち場・どの顧客か）。いま顧客が付くのは実測 21% だけ
2. **判断待ちの優先順**（小窓に最初に出す 1 件を選ぶ。いまは先頭固定）
3. **任せる先**（上限だけで選んでいる所に、内容の向き不向きを足す）
4. 無人実行の束から「見るべき 1 件」を選ぶ

当てはまらない所: **分解**（/api/plan）は生成が要るので Jev の型に合わない。いまどおり haiku で足りる。

**制約（ここが判断の要）**: どれも**セッションの文章を機械の外へ出す**。AIBoard の売りは
「外部のサーバを通らない・それを機械で証明する」なので、既定で使うことはできない。
入れるなら遠隔と同じ形（**既定は切・入れたら prove-local-only.sh が必ずその事実を書く**）にするか、
LM Studio / Ollama の**手元のモデル**で同じ判断をさせる。判定器の差し替え口を 1 つ作れば、
規則 → 手元モデル → 外部（Jev）を設定で切り替えられる。

## 表1 要点（機能）
- 無限キャンバス: termcanvas / termscape / Nyx / Maestri。顧客・案件で束ねる B2 が ✅ は Maestri だけ。
- 判断待ち検知 C1 ✅: cmux・Agent View・Nimbalyst(モバイル)・Switchboard・agentboard・termcanvas。OS 通知＋Dock C2 はどれも ⚠ か不明。
- リソース表示 C3: 全製品 ❌/⚠。既存 iTerm/tmux の取り込み D4 ✅ は agentboard(tmux のみ)。
- libghostty のネイティブ: cmux のみ。

## 空いている組み合わせ
1. キャンバス × 顧客/案件 × 判断待ちの OS 通知・Dock（B1×B2×C2）— Maestri が最も近い
2. 過去 30 日索引 × ネイティブ libghostty × キャンバス（A2×E1×D2×B1）
3. セッションごとのリソース表示と停止（C3）× 既存 iTerm の取り込み（D4）— Termplane は D4 を満たす（tty 束ね）、C3 は MB 表示のみ

## 負けている軸
実績と配布（★0）／Mac のみ／クラウド・チーム機能なし。Vibe Kanban は 30,000 MAU でも個人課金が成立せず終了。有料化しているのは Warp・Conductor・Nyx・Maestri。

出典: エージェント報告の URL 一覧（GitHub API・formulae.brew.sh analytics・hn.algolia・各公式サイト）。crosscheck 未実施。brew は 30 日、DL は累計で定義が違うので直接比較しない。
