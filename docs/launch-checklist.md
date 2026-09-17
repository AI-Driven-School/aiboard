# AIBoard ローンチ要件（バズった事例の要素を MECE に分解）— 2026-09-18

根拠: docs/R5_x_viral_2026-09-17.md（12 事例）・docs/R4_market_2026-09-17.md。
**正直な前提**: 全部満たしても「バズる」保証は無い。事例で共通するのは「満たさないと伸びない条件（必要条件）」と、
「第三者の引用」という自分では起こせない要素。ここでは必要条件を全部満たし、第三者が引用したくなる材料を揃えるところまでを目標にする。

凡例: ✅ 満たした（検証済み）／🟡 着手中／⬜ 未／🙋 本人の作業（署名・投稿・課金）／🎲 自分では起こせない

## A. プロダクト（8〜30 秒で「違う」と分かるもの）
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| A1 | 1 画面で差が分かる：多数のエージェントが同時に見え、判断待ちが光る | 静止画 1 枚で説明が要らない | cmux「pane glows blue」, Nyx | ✅ 赤の脈動＋「あなたの番 N」 |
| A2 | ネイティブで軽い（数字で言える） | 常駐 MB・フレーム ms を README に実測で書く | Ghostty, cmux（libghostty） | ✅ 46MB+WebKit 82MB／パン 10.8ms。README 未 |
| A3 | 複数プロバイダ（Claude Code＋Codex）を名指しで扱う | 両方が盤に出て、両方の端末が開く | Conductor, Nyx, Nimbalyst | ✅ Codex TUI もアプリ内で描画確認（自己試験） |
| A4 | 「困っている瞬間」を解く機能が実在 | 判断待ち→1 クリックで端末→キー送信 | cmux | ✅ 0.22 秒で入力可 |
| A5 | 1 行で入れられる | `brew install --cask aiboard` が通る | cmux, Vibe Kanban（npx） | ⬜ Homebrew tap＋DMG 未。**署名は 🙋（Developer ID $99/年。ad-hoc だと Gatekeeper が止める）** |
| A6 | 英語 UI が既定 | 画面の日本語ゼロ（ja はオプション） | 全事例 | ✅ 盤・アプリとも既定英語（`?lang=ja`／システム言語で日本語）。定型の日本語 0 件を機械確認 |
| A7 | テレメトリ既定 OFF・ローカル完結を明記 | README と初回起動に明記。外部送信ゼロを `lsof`/proxy で証明 | Vibe Kanban（HN 最上位批判） | 🟡 送信は元々ゼロ。明記が未 |
| A10 | **顧客名・私物を公開物に出さない** | リポ grep 0 件／デモモード（`?demo=1`）で DOM 全体に顧客名 0 件 | 本人の指示 | ✅ リポ 0 件・デモモード 0 件を機械確認。private は `~/aiboard-private/` |
| A8 | 名前が衝突しない | GitHub/npm/brew/X で同名の活発なものが無い | Coder の Cmux（同名で埋没） | ⬜ 未調査（"aiboard" は一般語で衝突しやすい） |
| A9 | 落ちない・戻る | 1 週間 iTerm 無しで運用、復元 1 操作 | Ghostty（β 5,000 人で磨いた） | ⬜ ⇧⌘R 未試験、IME 未確認 |

## B. デモ素材（投稿の本体）
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| B1 | 26〜111 秒の MP4（GIF でない） | 尺・1 カット目 3 秒以内に盤が見える | Gemini CLI 30s, Nyx 26s, Conductor 85s, cmux 111s | ⬜ 収録経路すら無い |
| B2 | 6 カット構成（空→カード→ズーム端末→タスク紐付け→光る→俯瞰→インストール） | 絵コンテどおり | R5 §4 | ⬜ |
| B3 | 静止画 1 枚（OG 画像・README ヒーロー） | 1280×640 で文字が読める | OpenCode（画面 1 枚で 1,081 いいね） | ✅ `site/img/og.png`（2400×1260）・ヒーロー `board-overview-crop.png`。顧客名ゼロを機械確認 |

## C. 文面
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| C1 | 1 行目＝一人称＋対象 CLI 名 | "We made / Introducing" ＋ Claude Code, Codex | Conductor, Nyx, cmux | 🟡 候補 5 本あり。決定未 |
| C2 | 困りの一文 | "When an agent needs you, its card lights up." | cmux | 🟡 |
| C3 | 箇条書き 4 行＋返信 1 投目にインストールとリポ | 型どおり | cmux | ⬜ |
| C4 | 日本語版の投稿も用意（友人枠） | 1 本 | cmux（497 いいね） | ⬜ |

## D. リポジトリ／README
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| D1 | タグライン 1 文＋ヒーロー動画／GIF が先頭 | README 先頭 5 行 | cmux, Vibe Kanban | ⬜ |
| D2 | インストール 1 行が Overview 内 | 同上 | Vibe Kanban（npx を前置） | ⬜ |
| D3 | Why（何が違うか）と比較表（cmux/Conductor/Nimbalyst/Nyx） | 表がある | cmux「Why cmux?」 | ⬜（R4 に材料あり） |
| D4 | MIT ライセンス・CONTRIBUTING・Star History | ファイルがある | cmux | ⬜ |
| D5 | 実測の数字（MB・ms・秒）を出典付きで | 数字に「測り方」が付く | 自社ルール | 🟡 LP に測り方つきで掲載。README 未 |
| D6 | **LP**（投稿のリンク先・OG 画像の家） | 1 画面で機能が分かる／OG 1200×630／1 行インストール／比較表／プライバシー | cmux.com, conductor.build, vibekanban.com, ghostty.org（全事例に自前ドメイン） | 🟡 `site/index.html` 完成（英語・デモ画像・OG）。ドメインと公開先は 🙋 |

## E. 順番と時刻
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| E1 | 公開前に 2〜8 週の進捗連投で期待を溜める | 週 1 本以上の "building in public" | Ghostty（2 か月）, OpenCode（連日） | ⬜ 🙋 |
| E2 | Show HN を平日 13〜15 UTC に、困り事から書き始める | 投稿時刻・本文 | cmux, Vibe Kanban, Conductor | ⬜ 🙋 |
| E3 | 5 日以内に X「Introducing」動画＋返信にインストール | 同上 | cmux（HN 後 5 日） | ⬜ 🙋 |
| E4 | 1 週間後に使い方スレ | 同上 | cmux | ⬜ 🙋 |

## F. 第三者の引用（自分では起こせない。材料は揃えられる）
| # | 要件 | 測り方 | 根拠事例 | 状態 |
|---|---|---|---|---|
| F1 | 有名人が引用したくなる「技術的な理由」 | cmux は libghostty 採用で Mitchell が引用（991 いいね、本人投稿より先） | cmux, Boo | 🎲 **選択肢: 端末を libghostty に替える**（未タグ・API 流動。M1 で比較予定） |
| F2 | 「使ってみた」を書ける人がすぐ試せる | A5・A6・D2 が前提 | Vibe Kanban（第三者 1,643 いいね） | ⬜ |
| F3 | Claude Code / Codex の中の人に届く | 製品名をタグ付け（DM でなく） | cmux | 🙋 |

## G. やってはいけない
| # | 反例 | 根拠 |
|---|---|---|
| G1 | 会社名義で発表・動画なし | Coder Cmux（22pt） |
| G2 | TUI だけで絵が地味 | Claude Squad（5pt） |
| G3 | 既定 ON のテレメトリ | Vibe Kanban |
| G4 | Electron で「軽い」と言う | Crystal／Nimbalyst |
| G5 | 146 秒超の動画 | Warp（view の割にいいね薄） |

## いまの充足率
- 私の手で満たせるもの（A1〜A4, A6〜A9, B1〜B3, C1〜C4, D1〜D5）: 21 項目中 ✅3 🟡4 ⬜14
- 本人の作業（A5 署名, E1〜E4, F3）: 6 項目
- 運（F1, F2）: 2 項目。F1 は libghostty 採用で確率を上げられる

## 自律改善の順（このファイルを更新しながら進める）
1. A6 英語 UI（盤・アプリ）→ 2. A7 明記＋外部送信ゼロの証明 → 3. A8 名前調査 → 4. B1 収録経路（アプリが自分の窓を 10fps で撮って ffmpeg）→ 5. B2 デモ収録 → 6. D1〜D5 README → 7. A3/A9 Codex TUI・復元・IME → 8. C1〜C4 文面確定
