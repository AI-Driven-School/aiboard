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
