#!/usr/bin/env bash
set -euo pipefail
HOST="${RADAR_HOST:?Set RADAR_HOST}"
rsync -av --exclude='.git' --exclude='data/' . "$HOST:/opt/catalyst-radar/"
ssh "$HOST" "cd /opt/catalyst-radar && docker compose up -d --build"
