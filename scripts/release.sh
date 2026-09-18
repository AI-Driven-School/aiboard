#!/bin/zsh
# 配布物を作る: ビルド → 署名 → 公証 → staple → zip と DMG → チェックサム。
#
#   scripts/release.sh 0.1.0            署名して公証する(Developer ID 証明書が要る)
#   scripts/release.sh 0.1.0 --unsigned 署名しない(手元で試す用。配ると「開けません」が出る)
#
# 公証は App Store Connect の API 鍵を使う(パスワード不要)。鍵の場所と ID は下の 3 つ。
set -eu
cd "$(dirname "$0")/.."
VER=${1:?版を指定: scripts/release.sh 0.1.0}
MODE=${2:-}
OUT="dist/$VER"
KEY=${ASC_KEY:-$HOME/.appstoreconnect/private_keys/AuthKey_77XTUTQL3A.p8}
KEY_ID=${ASC_KEY_ID:-77XTUTQL3A}
ISSUER=${ASC_ISSUER:-8937c198-525f-4bc4-a57a-55a0e0e8c9d4}

# 版を Info.plist に反映してから作る(出荷物と README の版がずれないように)
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" -c "Set :CFBundleVersion $VER" Resources/Info.plist
./make_app.sh >/dev/null
APP=build/AIBoard.app
mkdir -p "$OUT"

ID=$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -1 | sed -E 's/.*"(.*)"/\1/' || true)
if [ "$MODE" = "--unsigned" ]; then
  echo "⚠ 未署名で作る。配ると初回に「開けません」が出る"
elif [ -z "${ID:-}" ]; then
  echo "Developer ID Application の証明書が無い。Xcode > Settings > Accounts > Manage Certificates > + で作るか、--unsigned を付ける" >&2
  exit 2
else
  echo "署名: $ID"
  # 同梱物から順に署名し、最後に .app 全体。hardened runtime は公証に必須
  find "$APP/Contents/Resources" -name "*.bundle" -maxdepth 1 -exec \
    codesign --force --options runtime --timestamp --sign "$ID" {} \;
  codesign --force --options runtime --timestamp --entitlements Resources/AIBoard.entitlements \
    --sign "$ID" "$APP/Contents/MacOS/AIBoard"
  codesign --force --options runtime --timestamp --entitlements Resources/AIBoard.entitlements \
    --sign "$ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
fi

ZIP="$OUT/AIBoard-$VER.zip"
ditto -c -k --keepParent "$APP" "$ZIP"

if [ -n "${ID:-}" ] && [ "$MODE" != "--unsigned" ]; then
  echo "公証に出す(数分かかる)"
  xcrun notarytool submit "$ZIP" --key "$KEY" --key-id "$KEY_ID" --issuer "$ISSUER" --wait
  xcrun stapler staple "$APP"
  rm -f "$ZIP"
  ditto -c -k --keepParent "$APP" "$ZIP"   # staple 後に作り直す
  spctl -a -vvv -t install "$APP" || echo "⚠ Gatekeeper の判定が通らない"
fi

# DMG(ドラッグして入れる形)
DMGDIR=$(mktemp -d)
cp -R "$APP" "$DMGDIR/"
ln -s /Applications "$DMGDIR/Applications"
hdiutil create -volname "AIBoard $VER" -srcfolder "$DMGDIR" -ov -format UDZO "$OUT/AIBoard-$VER.dmg" >/dev/null
rm -rf "$DMGDIR"
[ -n "${ID:-}" ] && [ "$MODE" != "--unsigned" ] && {
  codesign --force --sign "$ID" --timestamp "$OUT/AIBoard-$VER.dmg"
  xcrun notarytool submit "$OUT/AIBoard-$VER.dmg" --key "$KEY" --key-id "$KEY_ID" --issuer "$ISSUER" --wait
  xcrun stapler staple "$OUT/AIBoard-$VER.dmg"
}

( cd "$OUT" && shasum -a 256 *.zip *.dmg > SHA256SUMS.txt )
echo "---"
ls -lh "$OUT" | awk '{print $5, $9}'
cat "$OUT/SHA256SUMS.txt"
