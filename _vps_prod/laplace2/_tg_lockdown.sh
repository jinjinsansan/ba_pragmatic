#!/bin/bash
# テレグラム ドライラン配信の切替: メイン2ch停止・山口さんのミラーchのみ継続
# 使い方: bash _tg_lockdown.sh lock   -> パターンch/結果chを止め、配信先をミラーchだけに
#         bash _tg_lockdown.sh unlock -> バックアップから元に戻す
set -e
ENV=/opt/laplace2/.env
MIRROR=-1004326175826
STAMP=$(date +%Y%m%d_%H%M%S)

if [ "$1" = "lock" ]; then
  cp "$ENV" "$ENV.bak_tglock_$STAMP"
  echo "backup -> $ENV.bak_tglock_$STAMP"
  # DUAL_LINE_CHAT_ID / TELEGRAM_CHAT_ID をミラーchへ・結果chとミラー重複は空に
  sed -i "s/^DUAL_LINE_CHAT_ID=.*/DUAL_LINE_CHAT_ID=$MIRROR/" "$ENV"
  sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=$MIRROR/" "$ENV"
  sed -i "s/^DUAL_LINE_RESULT_CHAT_ID=.*/DUAL_LINE_RESULT_CHAT_ID=/" "$ENV"
  sed -i "s/^DUAL_LINE_EXTRA_CHAT_IDS=.*/DUAL_LINE_EXTRA_CHAT_IDS=/" "$ENV"
  # 勝率ch(7/16新設)もミラーへ付け替え(2026-07-20追加)
  sed -i "s/^WINRATE_CHAT_ID=.*/WINRATE_CHAT_ID=$MIRROR/" "$ENV"
elif [ "$1" = "unlock" ]; then
  LAST=$(ls -t $ENV.bak_tglock_* 2>/dev/null | head -1)
  [ -n "$LAST" ] || { echo "no backup found"; exit 1; }
  cp "$LAST" "$ENV"
  echo "restored from $LAST"
else
  echo "usage: $0 lock|unlock"; exit 1
fi

grep -n 'CHAT_ID' "$ENV"
# bot再起動(ラッパーが20秒後に自動再起動)
sudo pkill -9 -f '[c]amoufox_profile_dual_line' || true
echo "bot kill sent — wrapper restarts in ~20s. Verify: tail wrapper log for TELEGRAM sends."
