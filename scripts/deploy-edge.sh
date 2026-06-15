#!/usr/bin/env bash
# Deploy the Cloudflare Worker + static site.
#
# Run this from your LOCAL machine (where wrangler is logged in).
# It is safe to re-run — wrangler is idempotent.
#
# Requirements:
#   - wrangler 4.x  (npm install -g wrangler)
#   - wrangler login done
#   - STREAM_KV namespace created (id in edge/wrangler.toml)

set -euo pipefail

cd "$(dirname "$0")/../edge"

echo "==> Typechecking..."
npx tsc --noEmit

echo "==> Deploying Worker..."
wrangler deploy

echo ""
echo "✓ Worker deployed. Test it:"
echo "  curl -sI https://treefrog-streams.\$(wrangler whoami | grep -oE '@[^.]+' | head -1 | tr -d '@').workers.dev/"
