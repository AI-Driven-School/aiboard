#!/bin/zsh
# AIBoard.app を作って ~/Applications に置く。SwiftPM の実行ファイルを .app に包み、ad-hoc 署名する。
set -eu
cd "$(dirname "$0")"
swift build -c release 2>&1 | grep -E "error|Build complete" || true
APP=build/AIBoard.app
# /Applications に書けるならそこへ(Finder の「アプリケーション」・Spotlight・Launchpad から見える)。書けなければ ~/Applications
if [ -w /Applications ]; then DEST="/Applications/AIBoard.app"; else DEST="$HOME/Applications/AIBoard.app"; fi
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/AIBoard "$APP/Contents/MacOS/AIBoard"
# libghostty の資源(terminfo・シェル統合)は同梱バンドルから読まれる
for b in .build/release/*.bundle; do [ -d "$b" ] && cp -R "$b" "$APP/Contents/Resources/"; done
cp Resources/Info.plist "$APP/Contents/Info.plist"
[ -f Resources/AppIcon.icns ] && cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
# 盤サーバ一式を同梱(データは ~/.aiboard に書くので同梱側は読み取りだけ)
rsync -a --exclude '__pycache__' --exclude '*.pyc' board/ "$APP/Contents/Resources/board/"
codesign --force --sign - "$APP" 2>&1 | grep -v "replacing existing signature" || true
mkdir -p "$HOME/Applications"
if [ -d "$DEST" ]; then rm -rf "$DEST"; fi
cp -R "$APP" "$DEST"
echo "installed: $DEST ($(du -sh "$DEST" | cut -f1))"
