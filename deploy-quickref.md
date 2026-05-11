# Deploy quickref

Single-VM deploy via rsync + `docker compose`. Run from your laptop; the engine
runs on the server.

## One-time server setup

Any Linux VM with SSH access. Install Docker + create the install dir:

```bash
ssh <user>@<server-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit                              # log out & back in for group to apply

ssh <user>@<server-ip>
sudo mkdir -p /opt/catalyst-radar/data
sudo chown -R $USER:$USER /opt/catalyst-radar
exit
```

## SSH alias (optional)

In `~/.ssh/config` on your laptop:

```
Host radar
  HostName <server-ip-or-domain>
  User <user>
  IdentityFile ~/.ssh/id_ed25519
```

Then `radar` works in place of `<user>@<ip>` everywhere below.

## Pre-deploy checklist

```bash
# .env must contain at minimum these keys (rsync ships it to the server)
grep -E "^(ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=" .env
```

## Deploy

```bash
RADAR_HOST=radar ./deploy.sh
```

What this does:
1. `rsync -av --exclude='.git' --exclude='data/'` to `/opt/catalyst-radar/`
   (your DB stays put on the host).
2. `ssh` in and run `docker compose up -d --build`.

## Watch the boot

```bash
ssh radar 'cd /opt/catalyst-radar && docker compose logs -f --tail=100 radar' \
  | grep -E "Backfill|Tier|Prune|Hourly|ERROR|WARNING"
```

Expected sequence on a **fresh DB**:
1. `catalyst-radar starting; tier1=300s, tier2=60s`
2. `Backfill: starting`
3. Per-ticker `Backfill BTC: +240 bars in 1.2s` (~5–10 min total)
4. `Backfill complete: N bars across M tickers in T.Ts`
5. `Tier 1: ...` cycle every 5 min
6. First Telegram hourly heartbeat at boot + 1h

On a **restart of a populated DB**, backfill prints mostly `fresh — skipping`
and returns in ~5 seconds.

## Common operations

```bash
# Tail live logs
ssh radar 'cd /opt/catalyst-radar && docker compose logs -f --tail=200 radar'

# Restart without rebuilding
ssh radar 'cd /opt/catalyst-radar && docker compose restart radar'

# Stop the engine
ssh radar 'cd /opt/catalyst-radar && docker compose down'

# DB size on host
ssh radar 'ls -lh /opt/catalyst-radar/data/radar.db'

# Quick DB stats
ssh radar 'sqlite3 /opt/catalyst-radar/data/radar.db \
  "SELECT COUNT(DISTINCT ticker) AS tickers, COUNT(*) AS bars FROM bars_1h;"'

# Active watchlist
ssh radar 'sqlite3 /opt/catalyst-radar/data/radar.db \
  "SELECT ticker, direction_bias, swing_high_reference, swing_low_reference, added_at FROM watchlist;"'

# Recent alerts (last 24h)
ssh radar 'sqlite3 /opt/catalyst-radar/data/radar.db \
  "SELECT ticker, decision, reason, datetime(created_at,'\''unixepoch'\'') FROM alerts ORDER BY created_at DESC LIMIT 20;"'

# Force a full re-backfill (rarely needed)
ssh radar 'cd /opt/catalyst-radar && docker compose down && \
           rm data/radar.db && docker compose up -d'
```

## Subsequent deploys

Just rerun `RADAR_HOST=radar ./deploy.sh`. Each deploy:
- Rebuilds the container with your code changes.
- `data/radar.db` survives (host volume; `data/` excluded from rsync).
- Backfill tops off the small gap created during the rebuild window.
- Engine resumes scanning.

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| Container won't start | `docker compose logs --tail=200 radar` — look for Python tracebacks |
| No backfill messages in log | `.env` made it over? `config.BACKFILL_ENABLED` still `True`? |
| No hourly Telegram message after 1h+ | Engine alive? Bot token rotated? See logs for `Hourly report send failed` |
| `permission denied` writing `data/radar.db` | `ssh radar 'sudo chown -R 1000:1000 /opt/catalyst-radar/data'` |
| DB growing past 1 GB | Check `Prune: removed N bars` is firing daily; confirm `PRUNE_INTERVAL_SEC` not overridden |
| Backfill always re-fetches everything | `data/` got rsync'd over by mistake — verify `deploy.sh` has `--exclude='data/'` |

## What ships in this deploy

The engine runs **two cooperating asyncio loops + one heartbeat loop**:

- **Tier 1** (every 5 min) — universe scan → ranker → catalysts → classifier → suppression → EMIT/WATCHLIST/DROP
- **Tier 2** (every 60 s) — polls watchlist tickers for live BOS crosses
- **Tier 3** (every 1 h) — pushes heartbeat + active watchlist + recent top movers to Telegram

On boot: gap-aware backfill of `bars_1h` so BOS can fire immediately rather
than waiting 5.5 days. On each Tier 1 cycle: auto-prune of bars > 30 days
and alerts > 30 days (at most once per day).
