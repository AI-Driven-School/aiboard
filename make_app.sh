#!/bin/zsh
# AIBoard.app を作って ~/Applications に置く。SwiftPM の実行ファイルを .app に包み、ad-hoc 署名する。
set -eu
cd "$(dirname "$0")"
# ビルドが失敗したら、そこで止める(古い実行ファイルを包んで配らない。grep に通すと rc が消えるので PIPESTATUS で見る)
mkdir -p build
swift build -c release 2>&1 | tee build/build.log | grep -E "error|Build complete" || true
rc=${pipestatus[1]:-${PIPESTATUS[0]:-0}}
if [ "$rc" != "0" ]; then
  echo "ビルド失敗(rc=$rc)。.app は作り直していない: build/build.log" >&2
  exit "$rc"
fi
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
