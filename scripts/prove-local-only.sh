#!/bin/zsh
# Termplane(AIBoard) が外部と通信していないことを確かめる。
# アプリ本体・盤サーバ(と子プロセス)の TCP/UDP 接続を 1 秒ごとに記録し、127.0.0.1 / ::1 以外の相手を数える。
# 使い方: scripts/prove-local-only.sh [秒数=120]   結果: stdout と ~/.aiboard/local-only-proof.txt
# 注: config.json に remote_hosts(ssh)を自分で設定した場合、その ssh は数に入る(利用者が明示的に有効にしたもの)。
set -u
SECS=${1:-120}
OUT="${AIBOARD_DATA:-$HOME/.aiboard}/local-only-proof.txt"
: > "$OUT"
# 遠隔(同じ LAN から判断待ちに答える)を入れていれば、その事実を先に書く。
# 入れていると盤サーバは 0.0.0.0 で待ち受ける(外へ「送る」わけではないが、隠さずに出す)
CFG="${AIBOARD_DATA:-$HOME/.aiboard}/config.json"
REMOTE=$(python3 -c "
import json, sys
try:
    print('on' if (json.load(open('$CFG')).get('remote') or {}).get('enabled') else 'off')
except Exception:
    print('off')
")
JUDGE=$(python3 -c "
import json
try:
    j = (json.load(open('$CFG')).get('judge') or {})
    print(j.get('backend') or 'rules', j.get('external_url') or '')
except Exception:
    print('rules')
")
case "$JUDGE" in
  external*) echo "判定器: 外部の決定モデル(${JUDGE#external }) — 伏せ字にした題名・依頼の先頭・フォルダ名が外へ出る。「外部送信ゼロ」ではない" | tee -a "$OUT" ;;
  local*)    echo "判定器: 手元のモデル(127.0.0.1 のみ)" | tee -a "$OUT" ;;
  *)         echo "判定器: 規則(外へ出さない)" | tee -a "$OUT" ;;
esac
GITPR=$(python3 -c "
import json
try:
    print('on' if (json.load(open('$CFG')).get('git') or {}).get('pr') else 'off')
except Exception:
    print('off')
")
if [ "$GITPR" = "on" ]; then
  echo "PR バッジ: 入(gh が GitHub にブランチ名で問い合わせる。認証は gh のもの)" | tee -a "$OUT"
else
  echo "PR バッジ: 切" | tee -a "$OUT"
fi
if [ "$REMOTE" = "on" ]; then
  echo "遠隔: 入(同じ LAN から /m と一覧・返事のみ・合言葉つき。待ち受けは 0.0.0.0)" | tee -a "$OUT"
else
  echo "遠隔: 切(待ち受けは 127.0.0.1 のみ)" | tee -a "$OUT"
fi
pids_of() {
  local app srv
  app=$(pgrep -x AIBoard | tr '\n' ' ')
  srv=$(lsof -nP -iTCP:8791 -sTCP:LISTEN -t 2>&1 | grep -E '^[0-9]+$' | tr '\n' ' ')
  local all="$app $srv" kids=""
  for p in ${=all}; do kids="$kids $(pgrep -P $p 2>&1 | grep -E "^[0-9]+$" | tr '\n' ' ')"; done
  echo "$all $kids" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ',' | sed 's/,$//'
}
echo "start $(date '+%F %T')  seconds=$SECS" | tee -a "$OUT"
for i in $(seq 1 $SECS); do
  P=$(pids_of)
  [ -n "$P" ] && lsof -nP -a -p "$P" -i 2>&1 | awk 'NR>1 {print $1, $2, $8, $9}' >> "$OUT.raw"
  sleep 1
done
echo "processes: $(pids_of)" | tee -a "$OUT"
TOTAL=$( [ -f "$OUT.raw" ] && wc -l < "$OUT.raw" | tr -d ' ' || echo 0 )
EXT=$( [ -f "$OUT.raw" ] && grep -v -E '127\.0\.0\.1|\[::1\]|localhost|\*:[0-9]+' "$OUT.raw" | grep -E -- '->' | sort -u || true )
echo "sampled socket rows: $TOTAL" | tee -a "$OUT"
# 1 行も測れていなければ「0 件」とは言わない(測れていないことと、外部接続が無いことは別。2026-09-18)
if [ "$TOTAL" -eq 0 ]; then
  echo "RESULT: INCONCLUSIVE — sampled 0 socket rows (app or board server not running, or lsof was denied). Start AIBoard and run again." | tee -a "$OUT"
  RC=2
elif [ -z "$EXT" ]; then echo "RESULT: 0 connections to anything other than this Mac (127.0.0.1 / ::1) in $TOTAL sampled rows (sampled once a second: a connection shorter than that can be missed — this is an observation, not a proof)" | tee -a "$OUT"; RC=0
else echo "RESULT: external connections found:" | tee -a "$OUT"; echo "$EXT" | tee -a "$OUT"; RC=1; fi
rm -f "$OUT.raw"
echo "end $(date '+%F %T')" | tee -a "$OUT"
exit $RC
