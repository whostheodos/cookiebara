# cookiebara

Multi-tenant webhook delivery service. One event in, signed and retried
delivery to every subscribed endpoint out.

**Status:** early development, not production-ready. M0 is complete; M1
(ingestion) is in progress — tenant models landed; API keys, endpoint
registration, `POST /events`, fan-out, and the `Delivery` schema subset are
pending. Delivery itself (signing, retries) is designed but not yet
implemented.

This README is the **source of truth for milestone numbering and project
scope**. The delivery design doc is the source of truth for delivery
semantics; where they disagree about delivery behavior, the doc wins and this
README is updated.

## Stack

Django 6.1 · PostgreSQL 16 · Celery + Redis (M3) · Docker Compose · pytest · Ruff

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
`db` service publishes port 5432 for exactly this. Tests must run against
PostgreSQL — the delivery claim uses `FOR UPDATE SKIP LOCKED`, which SQLite
does not implement.

## Layout

```
cookiebara/    Django project (settings, urls, wsgi/asgi)
core/          single app: models, views, tests, migrations
docs/          design notes — read delivery-state-machine.md first
```

State changes will live in `core/transitions.py` (added in M2): one module, no
`.save()` calls scattered across views and tasks.

## Milestones

| Milestone | Scope | State |
|---|---|---|
| **M0 Setup** | Docker, health endpoint, CI | done |
| **M1 Ingestion** | Tenants, API keys (hashed at rest, shown once), endpoint registration (URL, encrypted signing secret, event subscriptions), `POST /events`, tenant-scoped idempotency, fan-out in the same transaction as the event insert; `Delivery` rows with `unique(event, endpoint)`, `state`/`next_attempt_at`/`dead_reason`/`completed_at` and their constraints, composite cross-tenant FKs with a minimal database-rejection test, and `Event.payload_bytes` | in progress |
| **M2 Delivery** | `DeliveryAttempt` + remaining delivery execution columns and constraints, transition service, HMAC over timestamp + body (multi-value signature header), SSRF-safe URL validation with no redirects, wall-clock attempt deadline, sync/CLI happy path, manual lease reclaim, disabled-endpoint reconciliation | planned |
| **M3 Reliability** | Celery + Redis workers, 24h+ backoff, 3xx/4xx/429/5xx classification, 410 permanent disable + reconciliation, consecutive-failure pause (`paused_until`) with an endpoint-gated half-open probe, sweeper, stale-lease recovery, signing-secret rotation, replay | planned |
| **M4 Hardening** | Per-endpoint concurrency caps, per-tenant fairness, per-destination-host rate limits, egress allowlist/proxy, retention policy, full tenant-isolation suite | planned |
| **M5 Scale** | 1M attempts, index/query benchmarks | planned |
| **M6 Ship** | OpenAPI, metrics, live deploy, case study | planned |

## Multi-tenancy

Every endpoint, event, delivery, attempt, and API key belongs to exactly one
tenant. Tenant-owned tables use `TenantOwnedModel` (UUID PK, `tenant` FK), and
cross-tenant references are blocked by composite foreign keys, not just
application checks. Django's ORM cannot express composite FKs, so they live in
`RunSQL` migrations; a minimal database-rejection test ships with them in M1,
and the full isolation suite is M4. Delivery and attempt history is never
cascade-deleted with an endpoint or tenant; deletion is soft (`is_active`,
`deleted_at`). Temporary breaker pauses use `paused_until` and never
dead-letter deliveries.

## Docs

- [Delivery state machine](docs/delivery-state-machine.md) — authoritative for
  states, transitions, signing, retry budget, and SSRF policy

## Security

Webhook URLs are attacker-controlled, so the outbound request path is treated
as an SSRF boundary; the policy is in the delivery doc. API keys are stored
only as hashes and shown once; endpoint signing secrets are encrypted at rest
and never returned after creation. Local testing against `localhost` uses an
explicit dev allowlist that the app refuses to start with outside `DEBUG`.
This project is not production-ready. Please report suspected vulnerabilities
privately (GitHub Security Advisories) rather than in a public issue.

## Non-goals

- Strict FIFO ordering between deliveries
- Exactly-once delivery (at-least-once only; receivers dedupe on event ID)
- Priority queues
- Multi-region active-active workers
- Following HTTP redirects from webhook endpoints

These boundaries are mirrored in the delivery design doc.

## License

MIT
