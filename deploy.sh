#!/usr/bin/env bash
set -euo pipefail
HOST="${RADAR_HOST:?Set RADAR_HOST}"
rsync -av --exclude='.git' --exclude='data/' . "$HOST:/home/radar/catalyst-radar/"
ssh "$HOST" "cd /home/radar/catalyst-radar && docker compose up -d --build"
