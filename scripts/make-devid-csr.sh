#!/bin/zsh
# Developer ID 証明書を申請するための CSR(署名要求)を作る。Xcode は要らない。
#
#   scripts/make-devid-csr.sh          → ~/aiboard-private/signing/devid.csr を作って場所を出す
#
# この後は developer.apple.com → Certificates → ＋ → Developer ID Application → この CSR を上げる。
# 作れるのは Account Holder 本人だけ(API では 403 "This operation can only be performed by the Account Holder."。2026-09-19 実測)。
# 受け取った .cer は scripts/install-devid-cert.sh で鍵束に入れる。
set -eu
DIR="$HOME/aiboard-private/signing"
mkdir -p "$DIR"; chmod 700 "$DIR"
KEY="$DIR/devid.key"; CSR="$DIR/devid.csr"
if [ -f "$KEY" ] && [ -f "$CSR" ]; then
  echo "すでにあります: $CSR"
else
  openssl req -new -newkey rsa:2048 -nodes -keyout "$KEY" -out "$CSR" \
    -subj "/CN=Developer ID Application: RADINEER/O=RADINEER, LIMITED LIABILITY COMPANY/C=JP"
  chmod 600 "$KEY"
  echo "作りました: $CSR"
fi
echo "--- 次の手順 ---"
echo "1. https://developer.apple.com/account/resources/certificates/add を開く"
echo "2. Software の中の「Developer ID Application」を選ぶ（G2 Sub-CA のままでよい）"
echo "3. 上の CSR を上げて、できた .cer をダウンロードする"
echo "4. scripts/install-devid-cert.sh ~/Downloads/developerID_application.cer"
