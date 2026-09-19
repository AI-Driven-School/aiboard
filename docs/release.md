# 配る手順（署名 → 公証 → Releases → Homebrew）

自動でできない所は 1 つだけです。**Developer ID 証明書の作成は Account Holder 本人しかできません**
（API で試したところ `403 This operation can only be performed by the Account Holder.` でした。2026-09-18 実測）。

## 1. 一度だけ: Developer ID 証明書を作る（あなたの操作・2 分）

API では作れません（`403 This operation can only be performed by the Account Holder.` 2026-09-19 に再確認）。
**Xcode を使う道と、ブラウザだけの道**のどちらでもよく、後者の方が速いです。

### ブラウザだけ（推奨・CSR は作成済み）

1. `zsh scripts/make-devid-csr.sh`（もう作ってあります: `~/aiboard-private/signing/devid.csr`）
2. https://developer.apple.com/account/resources/certificates/add を開く
3. **Developer ID Application** を選ぶ → 上の CSR を上げる → `.cer` をダウンロード
4. `zsh scripts/install-devid-cert.sh ~/Downloads/developerID_application.cer`
   （秘密鍵と組にして鍵束に入れ、`security find-identity` で見えることまで確かめます）

### Xcode を使う道

1. Xcode → `Settings…` → `Accounts` → Apple ID → **Manage Certificates…**
2. 左下 **＋** → **Developer ID Application**（チームは RADINEER, LIMITED LIABILITY COMPANY (K7CD7UAWWC)）

どちらでも、できたか確認:

```sh
security find-identity -v -p codesigning | grep "Developer ID Application"
```

## 2. あとは自動

```sh
scripts/release.sh 0.1.0            # ビルド → 署名 → 公証 → staple → zip と dmg → SHA256
scripts/publish-release.sh 0.1.0    # GitHub Releases に出す → Homebrew tap を更新
```

- 公証は App Store Connect の API 鍵を使うので、パスワードの入力は要りません
  （鍵: `~/.appstoreconnect/private_keys/AuthKey_77XTUTQL3A.p8` / key-id `77XTUTQL3A` / issuer は `scripts/release.sh` 内）
- 署名が無い状態で試すなら `scripts/release.sh 0.1.0 --unsigned`。配布物はできますが、
  受け取った人の初回起動で「開けません」が出ます（Releases の説明文にも自動でその注意が入ります）
- `scripts/publish-release.sh` は**署名済みのときだけ** Homebrew の tap を更新します（未署名を brew で配らないため）

## 3. できあがるもの

| 置き場 | 中身 |
|---|---|
| `dist/<版>/AIBoard-<版>.dmg` | ドラッグして入れる形。署名時は公証と staple 済み |
| `dist/<版>/AIBoard-<版>.zip` | そのままの .app |
| `dist/<版>/SHA256SUMS.txt` | 2 つのチェックサム |
| GitHub Releases | 上の 3 つ＋入れ方・変更点・試験の件数 |
| `AI-Driven-School/homebrew-tap` の `Casks/aiboard.rb` | `brew install --cask ai-driven-school/tap/aiboard` |

## 4. 版を上げるとき

1. `docs/release-notes/<版>.md` に変更点を書く（Releases の本文に入ります）
2. `scripts/release.sh <版>` が `Resources/Info.plist` の版も書き換えます
3. `scripts/publish-release.sh <版>`

## 署名に付ける権限（entitlements）

`Resources/AIBoard.entitlements`。端末が `login` と `zsh` を起こし、盤サーバ（python）を動かし、
iTerm に AppleScript で話しかけるために要るものだけを入れてあります。サンドボックスは使いません
（端末そのものが仕事なので、App Store には出しません）。
