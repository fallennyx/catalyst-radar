# Catalyst Radar

A single-process Python engine that scans a leveraged crypto/equity/commodity
universe every five minutes, ranks unusual movers by a composite vol-normalized
score, fetches catalysts from asset-routed news sources, classifies them with
Claude Haiku, applies a four-rule suppression chain, and pushes the survivors
to Telegram.

## Architecture

```
universe → ranker → catalysts → classifier → suppression → telegram → sleep(300)
```

Synchronous, single-process, no async, no microservices, no message queues.
SQLite for state. ~40-line main loop. See [the spec](./CatalystRadar_Cowork_Spec.md)
for design rationale.

## Quick start

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env   # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LIGHTER_API_KEY

# 2. Run with Docker (recommended)
docker compose up -d --build
docker compose logs -f radar

# 3. Or run locally
pip install -e ".[dev]"
python -m radar.main
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/
```

## Tuning

All knobs live in [`radar/config.py`](./radar/config.py): asset universe,
cadence, ranker weights, suppression thresholds, LLM model, RSS feeds.

## Deploy

Set `RADAR_HOST=user@host` then run `./deploy.sh`. The script rsyncs the repo
(excluding `.git/` and `data/`) to `/opt/catalyst-radar/` and rebuilds the
container.
