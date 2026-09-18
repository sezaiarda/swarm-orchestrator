#!/usr/bin/env bash
# The SWARM's telegram sender — @your_swarm_bot.
#
# Swarm traffic is run events (a worker's question, a park, a merge conflict,
# the run finishing), sent from the swarm's own bot and kept apart from any
# other channel the owner uses.
#
# Credentials live in this repo's gitignored .env. The token is never printed,
# never passed on a command line that gets logged, and never echoed on error.
#
# Usage: notify.sh "message"        (plain text -- no parse_mode, see below)
#
# Sent with NO parse_mode on purpose. Under parse_mode=HTML the API rejects any
# `<` that does not open a valid tag ("400 can't parse entities") and the message
# is dropped outright -- and the text we send is worker prose (`Vec<T>`,
# `Option<String>`, `<200ms`) and raw git stderr. No template uses markup, so
# markup buys nothing and costs silently lost pings.

set -uo pipefail

ENV_FILE="${SWARM_TG_ENV:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"

if [ ! -r "$ENV_FILE" ]; then
  echo "swarm notify: missing $ENV_FILE" >&2
  echo "       create it with TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=..." >&2
  exit 1
fi

set -a; . "$ENV_FILE"; set +a

: "${TELEGRAM_BOT_TOKEN:?swarm notify: TELEGRAM_BOT_TOKEN not set in $ENV_FILE}"
: "${TELEGRAM_CHAT_ID:?swarm notify: TELEGRAM_CHAT_ID not set in $ENV_FILE -- send any message to the bot, then run: scripts/resolve-chat-id.sh}"

MSG="${1:?swarm notify: no message}"

resp="$(curl -s --max-time 20 \
  -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  -d "disable_web_page_preview=true" 2>&1)"

if printf '%s' "$resp" | grep -q '"ok":true'; then
  echo "sent"; exit 0
fi

# Surface the API's own error, but never the token.
printf 'swarm notify FAILED: %s\n' "$(printf '%s' "$resp" | sed "s#${TELEGRAM_BOT_TOKEN}#<token>#g" | head -c 400)" >&2
exit 1
