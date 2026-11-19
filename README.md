# cookiebara

Multi-tenant webhook delivery service. One event in, signed and retried
delivery to every subscribed endpoint out.

**Status:** M0 — skeleton. Health endpoint only; no public API yet.
PostgreSQL is the source of truth; Celery is only the trigger.

## Stack

Django 6.1 · PostgreSQL 16 · Celery + Redis (M2) · Docker Compose · pytest · Ruff

## Quickstart (Docker)

```bash
git clone https://github.com/whostheodos/cookiebara.git
cd cookiebara
cp .env.example .env
# replace the placeholder key:
python -c "import secrets; print(secrets.token_urlsafe(50))"   # paste into DJANGO_SECRET_KEY
docker compose up --build
```

```bash
curl http://localhost:8000/health/
# {"status": "ok"}
```

`docker compose up` runs migrations automatically.

## Local development (without Docker for the app)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check .
```

Requires a reachable PostgreSQL matching `DATABASE_*` in `.env`; the compose
`db` service publishes port 5432 for exactly this.

## Layout

```
cookiebara/    Django project (settings, urls, wsgi/asgi)
core/          single app: models, views, tests, migrations
docs/          design notes — read delivery-state-machine.md first
```

## Docs

- [Delivery state machine](docs/delivery-state-machine.md) — states, transitions, failure recovery

## Roadmap

- [x] M0 Setup — Docker, health endpoint, CI
- [ ] M1 Ingestion — tenants, API keys, `POST /events`, idempotency, fan-out
- [ ] M2 Delivery — Celery worker, HMAC signing, attempt log
- [ ] M3 Reliability — backoff, dead-letter, sweeper, failure tests
- [ ] M4 Hardening — SSRF protection, rate limiting, isolation tests
- [ ] M5 Scale — 1M attempts, index/query benchmarks
- [ ] M6 Ship — OpenAPI, metrics, live deploy, case study

## License

MIT
