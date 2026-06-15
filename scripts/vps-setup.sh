#!/usr/bin/env bash
# Run this ONCE on the VPS to install Docker, clone the repo, and
# boot the engine. After it completes, follow the remaining steps in
# docs/vps-deploy.md to wire up the tunnel and DNS.
#
# Run as a user with sudo rights (NOT as root).

set -euo pipefail

REPO_URL="https://github.com/michaelrobgrove/treefrogfree.git"
INSTALL_DIR="/opt/treefrogfree"

echo "==> Installing Docker (if missing)..."
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed. You may need to log out and back in for group changes to take effect."
fi

echo "==> Cloning repo to ${INSTALL_DIR}..."
if [ ! -d "$INSTALL_DIR" ]; then
    sudo git clone "$REPO_URL" "$INSTALL_DIR"
    sudo chown -R "$USER:$USER" "$INSTALL_DIR"
else
    echo "Already cloned; pulling latest..."
    (cd "$INSTALL_DIR" && git pull)
fi

cd "$INSTALL_DIR/engine"

if [ ! -f .env ]; then
    echo "==> Creating .env from template..."
    cp .env.example .env
    echo ""
    echo "  ⚠️  IMPORTANT: Edit /opt/treefrogfree/engine/.env and fill in:"
    echo "      CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, ADMIN_TOKEN"
    echo "      Then re-run this script (or just: cd /opt/treefrogfree/engine && docker compose up -d)"
    echo ""
    exit 1
fi

echo "==> Building image and starting engine..."
docker compose up -d --build

echo "==> Tailing logs (Ctrl-C to stop watching; engine keeps running)..."
docker compose logs -f tf-engine
