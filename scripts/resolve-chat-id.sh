#!/usr/bin/env bash
# Resolve the swarm bot's chat_id from getUpdates and persist it into .env.
# Send any message to the bot first (e.g. /start), then run this once.
set -uo pipefail
ENV_FILE="${SWARM_TG_ENV:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"
set -a; . "$ENV_FILE"; set +a
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set}"

id="$(curl -s --max-time 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
if not d.get("ok"): sys.exit(1)
for u in reversed(d.get("result",[])):
    m=u.get("message") or u.get("channel_post") or {}
    c=(m.get("chat") or {}).get("id")
    if c: print(c); break
')"

if [ -z "$id" ]; then
  echo "no chat found — send any message to the bot, then re-run this" >&2
  exit 1
fi

if grep -q '^TELEGRAM_CHAT_ID=' "$ENV_FILE"; then
  sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=${id}/" "$ENV_FILE"
else
  printf 'TELEGRAM_CHAT_ID=%s\n' "$id" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
echo "chat_id resolved and saved: $id"
