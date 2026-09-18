#!/bin/zsh
# Termplane(AIBoard) が外部と通信していないことを確かめる。
# アプリ本体・盤サーバ(と子プロセス)の TCP/UDP 接続を 1 秒ごとに記録し、127.0.0.1 / ::1 以外の相手を数える。
# 使い方: scripts/prove-local-only.sh [秒数=120]   結果: stdout と ~/.aiboard/local-only-proof.txt
# 注: config.json に remote_hosts(ssh)を自分で設定した場合、その ssh は数に入る(利用者が明示的に有効にしたもの)。
set -u
SECS=${1:-120}
OUT="${AIBOARD_DATA:-$HOME/.aiboard}/local-only-proof.txt"
: > "$OUT"
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
elif [ -z "$EXT" ]; then echo "RESULT: 0 connections to anything other than this Mac (127.0.0.1 / ::1) in $TOTAL sampled rows" | tee -a "$OUT"; RC=0
else echo "RESULT: external connections found:" | tee -a "$OUT"; echo "$EXT" | tee -a "$OUT"; RC=1; fi
rm -f "$OUT.raw"
echo "end $(date '+%F %T')" | tee -a "$OUT"
exit $RC
