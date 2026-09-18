#!/bin/zsh
# GitHub の Releases に出し、Homebrew の tap を更新する。
#   scripts/publish-release.sh 0.1.0 [--draft]
# 先に scripts/release.sh で dist/<版>/ を作っておくこと。
set -eu
cd "$(dirname "$0")/.."
VER=${1:?版を指定}
DRAFT=${2:-}
REPO=${AIBOARD_REPO:-AI-Driven-School/aiboard}
TAP=${AIBOARD_TAP:-AI-Driven-School/homebrew-tap}
OUT="dist/$VER"
[ -f "$OUT/AIBoard-$VER.dmg" ] || { echo "$OUT に配布物が無い。先に scripts/release.sh $VER" >&2; exit 2; }

if codesign -dv build/AIBoard.app 2>&1 | grep -q "Signature=adhoc"; then SIGNED=no; else SIGNED=yes; fi
NOTES="$OUT/notes.md"
{
  echo "## 入れ方"
  echo
  if [ "$SIGNED" = yes ]; then
    echo '```sh'
    echo "brew install --cask ${TAP%/*}/tap/aiboard"
    echo '```'
    echo
    echo "または AIBoard-$VER.dmg を開いて Applications に入れる。"
  else
    echo "**この版は署名していません。** 初回は右クリック →「開く」で起動してください。"
    echo
    echo '```sh'
    echo "xattr -d com.apple.quarantine /Applications/AIBoard.app   # 警告が消えないとき"
    echo '```'
  fi
  echo
  echo "## 中身"
  echo
  if [ -f "docs/release-notes/$VER.md" ]; then sed -n '1,40p' "docs/release-notes/$VER.md"; else echo "- 変更点は README と履歴を参照"; fi
  echo
  echo "## 確認"
  echo
  echo "受け入れ試験 $(grep -c '@case(' scripts/uat/run_uat.py) 件が実機に対して通っています(python3 scripts/uat/run_uat.py)。"
  echo
  echo '```'
  cat "$OUT/SHA256SUMS.txt"
  echo '```'
} > "$NOTES"

gh release create "v$VER" "$OUT"/*.dmg "$OUT"/*.zip "$OUT/SHA256SUMS.txt" \
  --repo "$REPO" --title "AIBoard $VER" --notes-file "$NOTES" ${DRAFT:+--draft}

if [ "$SIGNED" = yes ]; then
  SHA=$(awk '/\.dmg$/{print $1}' "$OUT/SHA256SUMS.txt")
  TMP=$(mktemp -d)
  if ! gh repo clone "$TAP" "$TMP/tap" -- -q; then
    gh repo create "$TAP" --public -d "Homebrew tap for AI-Driven-School"
    gh repo clone "$TAP" "$TMP/tap" -- -q
  fi
  mkdir -p "$TMP/tap/Casks"
  sed -e "s/__VERSION__/$VER/" -e "s/__SHA256__/$SHA/" packaging/aiboard.rb.tmpl > "$TMP/tap/Casks/aiboard.rb"
  git -C "$TMP/tap" add Casks/aiboard.rb
  git -C "$TMP/tap" commit -q -m "aiboard $VER"
  git -C "$TMP/tap" push -q
  echo "tap を更新: brew install --cask ${TAP%/*}/tap/aiboard"
else
  echo "未署名なので tap は更新しない(brew は署名済みだけ配る)"
fi
echo "Releases: https://github.com/$REPO/releases/tag/v$VER"
