#!/usr/bin/env bash
# Run a Cloudflare quick tunnel to the local approval endpoint and publish the
# ephemeral URL to docs/data/approve_endpoint.json (committed+pushed to Pages).
set -uo pipefail
DASH=/home/ubuntu/quanty-dashboard
ENDPOINT_FILE="$DASH/docs/data/approve_endpoint.json"
last=""
cloudflared tunnel --url http://localhost:8078 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  if [ -n "$url" ] && [ "$url" != "$last" ]; then
    last="$url"
    printf '{"url": "%s", "updated_at": "%s"}\n' "$url" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ENDPOINT_FILE"
    git -C "$DASH" add docs/data/approve_endpoint.json >/dev/null 2>&1 \
      && git -C "$DASH" commit -m "chore: publish approval tunnel url" -q >/dev/null 2>&1 \
      && git -C "$DASH" push -q >/dev/null 2>&1 || true
    echo "[tunnel] published $url"
  fi
done
