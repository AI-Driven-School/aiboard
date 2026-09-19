#!/bin/zsh
# 受け取った Developer ID 証明書(.cer)を、手元の秘密鍵と組にして鍵束に入れる。
#
#   scripts/install-devid-cert.sh ~/Downloads/developerID_application.cer
#
# 前提: scripts/make-devid-csr.sh で作った ~/aiboard-private/signing/devid.key があること。
# 鍵は公開リポには入れない(~/aiboard-private の下・600)。
set -eu
CER=${1:?証明書(.cer)のパスを指定}
DIR="$HOME/aiboard-private/signing"
KEY="$DIR/devid.key"
[ -f "$KEY" ] || { echo "秘密鍵が無い: $KEY（先に scripts/make-devid-csr.sh を実行）" >&2; exit 2; }
[ -f "$CER" ] || { echo "証明書が無い: $CER" >&2; exit 2; }

PEM="$DIR/devid.cer.pem"
openssl x509 -inform DER -in "$CER" -out "$PEM" 2>/dev/null || cp "$CER" "$PEM"   # DER でも PEM でも受ける
P12="$DIR/devid.p12"
PASS=$(openssl rand -base64 18)
openssl pkcs12 -export -inkey "$KEY" -in "$PEM" -out "$P12" -passout "pass:$PASS" -name "Developer ID Application (AIBoard)"
chmod 600 "$P12"
security import "$P12" -k "$HOME/Library/Keychains/login.keychain-db" -P "$PASS" -T /usr/bin/codesign -T /usr/bin/security
echo "---"
security find-identity -v -p codesigning | grep "Developer ID Application" || {
  echo "鍵束に入ったが codesigning の身元として見えない。Keychain Access で信頼設定を確認してください" >&2; exit 3; }
echo "入りました。次: scripts/release.sh 0.1.0 && scripts/publish-release.sh 0.1.0"
