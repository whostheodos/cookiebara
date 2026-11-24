# Delivery state machine

Status: active design. M1 (ingestion) is in progress; delivery execution lands
in M2 and M3.
Scope: one `Delivery` row = one Event going to one Endpoint. Every dispatch
attempt, including a pre-dispatch rejection, is recorded as an immutable
`DeliveryAttempt` child row.

**Source of truth:** `README.md` owns milestone numbering and project scope.
This document owns delivery semantics: states, transitions, signing, retry
budget, SSRF policy. Milestone labels below use the README numbering (M0–M6).

## Milestone roadmap

- **M0 (Done — Setup):** Docker, health endpoint, CI.
- **M1 (In progress — Ingestion):** Tenants, API keys (hashed at rest, shown
  once), endpoint registration (URL, signing secret encrypted at rest, event
  subscriptions), `POST /events`, tenant-scoped idempotency
  (`unique(tenant, idempotency_key)`), and fan-out. Ships the part of the
  delivery schema fan-out needs: `Delivery` rows
  with `state`, `next_attempt_at`, `dead_reason`, `completed_at`,
  `unique(event, endpoint)`, the `CheckConstraint`s that apply to `pending` and
  `dead` rows, tenant and composite cross-tenant FKs (with a minimal
  database-rejection test), and `Event.payload_bytes` captured at ingestion.
- **M2 (Delivery):** `DeliveryAttempt`, the remaining `Delivery` execution
  columns and constraints (`in_flight`, `succeeded`, claim fields), single
  transition service, HMAC over timestamp + body, SSRF-safe URL validation with
  a no-redirect policy, sync/CLI happy path, manual stale-lease reclaim, and
  disabled-endpoint reconciliation (`reconcile_disabled`).
- **M3 (Reliability):** Celery + Redis workers, 24h+ backoff, 3xx/4xx/429/5xx
  classification, 410 permanent disable plus reconciliation, consecutive-
  failure **pause** with an endpoint-gated half-open probe (`paused_until`),
  sweeper,
  stale-lease recovery, crash-loop breaker, signing-secret rotation, and
  replay.
- **M4 (Hardening):** Per-endpoint concurrency caps, per-tenant fairness,
  per-destination-host rate limits, egress allowlist/proxy, retention policy,
  and the full tenant-isolation suite.
- **M5 (Scale):** 1M attempts, index/query benchmarks.
- **M6 (Ship):** OpenAPI, metrics, live deploy, case study.

## Delivery guarantee

**At-least-once, never exactly-once.** A worker can crash after the HTTP
request is sent but before the attempt is recorded, so a receiver may see the
same event twice. Receivers must dedupe on the event ID
(`X-Webhook-Id` / `event.id`). The public API docs will state this when
`POST /events` ships.

`X-Webhook-Attempt` carries the claim's monotonic `attempt_number`, allocated
at claim time, before the request is sent. It is not a retry index: it never
resets, can have gaps (a claim that crashed before recording, a replay), and
must not be used for exactly-once logic.

## Tenancy

- Every `Endpoint`, `Event`, `Delivery`, `DeliveryAttempt`, and API key belongs
  to exactly one tenant (UUID PK + `tenant` FK via `TenantOwnedModel`).
- Cross-tenant references are blocked in the database with composite foreign
  keys: `Event` and `Endpoint` get `UNIQUE (tenant_id, id)`, and `Delivery`
  references `(tenant_id, event_id)` and `(tenant_id, endpoint_id)`. Django's
  ORM cannot express composite FKs, so these live in `RunSQL` migrations. A
  minimal test that the database rejects a cross-tenant insert ships with the
  constraints in M1; the full isolation suite is M4.
- Every query is filtered to one tenant, except two cross-tenant candidate
  scans over tenant-owned rows: the worker's `SKIP LOCKED` picker and the
  sweeper. Both are bounded and become fair across tenants in M4.
- Endpoint and tenant deletion are soft (`is_active = false`, `deleted_at` set)
  or `PROTECT`; delivery and attempt history is never cascade-deleted, so the
  audit trail survives.

## States

| State | Means | Terminal | Milestone | Written by |
|---|---|---|---|---|
| `pending` | Due at `next_attempt_at` (created at fan-out in M1; scheduled retries in M3+); no worker owns it | no | M1 | API (fan-out), worker (M3+), sweeper (M3), operator (M3) |
| `in_flight` | A worker claimed it with a unique `lease_token` and is executing the HTTP request | no | M2 | worker/CLI (atomic claim) |
| `succeeded` | Receiver returned 2xx | yes | M2 | worker |
| `dead` | Delivery failed terminally; `dead_reason` records why | yes | M1 | API (disabled endpoint), worker (M2), sweeper (M3) |

`state` and `dead_reason` are `CharField`s with `choices`, not Postgres enum
types, and `dead_reason` has no value-list database constraint (only the
null/non-null rules below). Adding M2/M3 values therefore needs no
`ALTER TYPE` or constraint rewrite. All four `state` values exist from M1.

**Fan-out.** Fan-out runs inside the same database transaction as the `Event`
insert, so an Event never exists without its deliveries (when the endpoint set
below is non-empty; a tenant with no subscribed endpoints produces an Event
with no deliveries). The endpoint set is the tenant's subscribed,
**non-deleted** endpoints at that moment, including permanently disabled ones.
Deleting an endpoint unsubscribes it, so it receives no new rows. Endpoints
created later do not receive earlier events. Each Event→Endpoint pair gets
exactly one `Delivery` row. It starts as `pending` (`next_attempt_at = now()`)
when the endpoint is active, or directly as `dead` (`endpoint_disabled`,
`completed_at = now()`) when the endpoint is permanently disabled. The row
always exists, so history is auditable and replayable. An idempotent retry of
`POST /events` (same tenant and idempotency key) returns the existing Event and
does not fan out again; `unique(event, endpoint)` is the backstop.

Fan-out is one transaction, so its size grows with the endpoint count. A
per-tenant endpoint cap (value chosen in M1) bounds it; very large fan-out is
revisited in M5.

## Dead reasons

`dead_reason` is `NOT NULL` whenever `state = 'dead'` (and vice versa). It is
an **operator-facing** field: the tenant API exposes only a coarse
`failure_category` (for example `delivery_failed`), because a precise
`timeout` vs `network_error` vs `non_2xx` distinction turns the field into a
port-scan oracle.

| `dead_reason` | Meaning | Milestone |
|---|---|---|
| `non_2xx` | Non-2xx response (M2 temporary rule before classification) | M2 |
| `timeout` | Wall-clock attempt deadline exceeded | M2 |
| `network_error` | DNS/TCP/TLS error before a response | M2 |
| `url_rejected` | URL failed SSRF validation; no request was dispatched | M2 |
| `endpoint_disabled` | Endpoint permanently disabled at fan-out, or disabled or deleted afterwards; no request sent | M1 |
| `permanent_4xx` | 4xx other than 408/429 (client configuration bug) | M3 |
| `redirect_not_followed` | 3xx response; redirects are never followed (SSRF policy) | M3 |
| `budget_exhausted` | Retry budget exhausted after a retryable failure | M3 |
| `crash_loop` | `lease_expiry_count >= max_lease_expiries`; repeated worker crashes | M3 |

A circuit-breaker pause is **not** a dead reason: paused deliveries stay
`pending` and are never dead-lettered.

## Transitions

### Active in M2

| From → To | Trigger | Actor | Guard |
|---|---|---|---|
| `pending → in_flight` | Claim one due delivery | worker/CLI | Per-row CAS: `UPDATE ... SET state = 'in_flight', claimed_at = now(), lease_token = :new_token, attempt_seq = attempt_seq + 1 WHERE id = :id AND state = 'pending' AND next_attempt_at <= now() AND <endpoint usable> RETURNING attempt_seq`. *Endpoint usable* means `is_active = true AND deleted_at IS NULL AND (paused_until IS NULL OR paused_until <= now())`. The returned `attempt_seq` is this claim's `attempt_number` |
| `in_flight → succeeded` | 2xx response | worker/CLI | `state = 'in_flight' AND lease_token = :token`; attempt row and transition commit in one transaction |
| `in_flight → dead` | Non-2xx, timeout, network error, or SSRF rejection; sets `dead_reason` | worker/CLI | `state = 'in_flight' AND lease_token = :token`; attempt row and transition commit in one transaction |
| `pending → dead` | Endpoint permanently disabled or deleted after fan-out; sets `dead_reason = 'endpoint_disabled'` | the disable/delete operation (same transaction), `manage.py reconcile_disabled`, sweeper (M3) | `endpoint.is_active = false` or `endpoint.deleted_at IS NOT NULL`; no request is made |
| `in_flight → pending` | Manual stale-lease reclaim (`manage.py reclaim_leases --older-than 60s`) | operator/CLI | `claimed_at < now() - :older_than`, where `:older_than` is the value passed to the command (default 60s = 45s lease + 15s slack); clears lease fields, increments `lease_expiry_count`, writes audit entry; no attempt row |

**Claim mechanics.** Claiming is two steps: a picker, then the CAS.

- The picker selects one candidate:
  `SELECT d.id FROM deliveries d JOIN endpoints e ON ... WHERE d.state = 'pending' AND d.next_attempt_at <= now() AND <endpoint usable> ORDER BY d.next_attempt_at LIMIT 1 FOR UPDATE OF d SKIP LOCKED`.
  The picker applies the **same endpoint predicates** as the CAS. Without that,
  due rows for paused or disabled endpoints would be picked first, fail the
  CAS, and starve rows for healthy endpoints (head-of-line blocking).
- The per-row CAS above is authoritative, so two workers racing the same row
  cannot both win. It is written as `UPDATE ... FROM endpoints` (or an
  equivalent subquery). The endpoint row is not locked, so an endpoint disabled
  at the same instant as a claim may still receive that one request; this is
  accepted.
- `claimed_at` is the lease start and is set at the moment of claim,
  immediately before dispatch. A worker claims one row at a time and never
  prefetches a batch, which would let a queued row's lease expire while it
  waits.
- A partial index on `(next_attempt_at) WHERE state = 'pending'` backs the
  picker. `SKIP LOCKED` requires PostgreSQL, which is the source of truth; the
  test suite must run against PostgreSQL (a SQLite CI run would silently not
  exercise it).

**Disabled-endpoint reconcile.** The picker and claim never touch rows of an
unusable endpoint, so nothing else would ever move them. The operation that
sets `is_active = false` or `deleted_at` therefore bulk-transitions that
endpoint's `pending` rows to `dead` (`endpoint_disabled`) **in the same
transaction**. `manage.py reconcile_disabled` re-runs the same transition as a
safety net for rows missed by a crash or manual SQL, and the M3 sweeper takes
over that job. A request already `in_flight` when the endpoint is disabled is
allowed to finish.

The M2 manual reclaim exists because there is no sweeper until M3: a Ctrl-C,
OOM, or deploy mid-send would otherwise strand a row in `in_flight` forever.
Its default `--older-than 60s` deliberately exceeds the 45s lease.

### Deferred to M3

| From → To | Trigger | Actor | Guard |
|---|---|---|---|
| `in_flight → pending` | Retryable failure (408, 429, 5xx, timeout, network error) | worker | `state = 'in_flight' AND lease_token = :token AND attempts < max_attempts`; sets `next_attempt_at` from the backoff schedule |
| `in_flight → dead` | Retryable failure with the budget spent; sets `dead_reason = 'budget_exhausted'` | worker | `state = 'in_flight' AND lease_token = :token AND attempts >= max_attempts` |
| `in_flight → dead` | Permanent 4xx (all except 408/429); sets `dead_reason = 'permanent_4xx'` | worker | `state = 'in_flight' AND lease_token = :token` |
| `in_flight → dead` | 3xx response (never followed); sets `dead_reason = 'redirect_not_followed'` | worker | `state = 'in_flight' AND lease_token = :token` |
| `in_flight → dead` | 410 Gone; sets `dead_reason = 'permanent_4xx'`, permanently disables the endpoint, and dead-letters remaining pending rows | worker, then sweeper/CLI reconcile | `state = 'in_flight' AND lease_token = :token`. Side effects: `endpoint.is_active = false`, `endpoint.disabled_at = now()`, `endpoint.disabled_reason = 'endpoint_gone_410'`; the reconcile bulk-transitions that endpoint's `pending` rows to `dead` (`endpoint_disabled`) |
| `pending → in_flight` | Half-open probe claim (endpoint has `pause_reason` set and `paused_until <= now()`) | worker | The normal delivery CAS, **preceded in the same transaction** by the endpoint-row gate: `UPDATE endpoints SET paused_until = now() + interval '45s' WHERE id = :endpoint_id AND pause_reason IS NOT NULL AND paused_until <= now()`. If the gate updates 0 rows, or the delivery CAS then updates 0 rows, the transaction rolls back: no probe lease is taken and nothing is dispatched |
| `in_flight → succeeded` | Probe or ordinary success on an endpoint with failures or a pause recorded | worker | Same guard as any success. After the delivery transaction commits, a separate short transaction sets `endpoint.consecutive_failures = 0` and, for a probe, `paused_until = NULL` and `pause_reason = NULL`. If that follow-up is lost, the probe lease lapses and the next claim probes again |
| `in_flight → pending` or `dead` | Probe fails | worker | Ordinary classification (retry, `permanent_4xx`, `budget_exhausted`, ...). After commit, the endpoint is re-paused: `paused_until = now() + 15m`, `pause_reason` kept |
| `in_flight → pending` | Lease expired (worker stalled or died mid-send) | sweeper | `claimed_at < now() - lease_duration AND lease_expiry_count < max_lease_expiries`; clears lease fields, increments `lease_expiry_count` |
| `in_flight → dead` | Repeated crashes before recording; sets `dead_reason = 'crash_loop'` | sweeper | `claimed_at < now() - lease_duration AND lease_expiry_count >= max_lease_expiries` |
| `dead → pending` | Operator triggers manual replay | API/admin | Endpoint must be usable: `endpoint.is_active = true AND endpoint.deleted_at IS NULL`; otherwise the replay is rejected. Resets `attempts = 0` and `lease_expiry_count = 0` (never `attempt_seq` or attempt rows), clears `dead_reason` and `completed_at`; `next_attempt_at = max(now(), endpoint.paused_until)`. Logs audit entry (actor, reason, timestamp) |

**Pause trip (endpoint-level, not a delivery transition).** After the
transaction of any dispatched failed attempt commits, a separate short
transaction runs `UPDATE endpoints SET consecutive_failures =
consecutive_failures + 1 ... RETURNING consecutive_failures`. Reaching 20 sets
`paused_until = now() + 15m` and `pause_reason = 'consecutive_failures'`. The
failed delivery itself follows its ordinary classification (retry with
backoff, or dead); the pause only gates future claims.

`dead → succeeded` and `succeeded → pending` remain illegal in every milestone.
See Decisions & non-goals.

## Invariants

- **[M1]** Event insert and fan-out commit in one transaction; when the
  endpoint set is non-empty, an Event never exists without its `Delivery`
  rows.
- **[M1]** `unique(event, endpoint)` holds on `Delivery` for all time; fan-out
  is idempotent and safe to re-run.
- **[M1]** Idempotency keys are scoped per tenant:
  `unique(tenant, idempotency_key)`. Keys never collide across tenants.
- **[M1]** API keys are stored only as a hash of a high-entropy random token
  (plus a non-secret lookup prefix), shown once at creation, and compared in
  constant time. Endpoint signing secrets are encrypted at rest from the first
  migration that stores them; no plaintext secret or API key is ever persisted,
  logged, or committed in a fixture.
- **[M1]** `Event.payload_bytes` (`bytea`) is captured once at ingestion and is
  the only source for signing and sending; `jsonb` is query-only and never
  re-serialized.
- **[M1]** Every `pending` delivery has `next_attempt_at` set and
  `completed_at` null.
- **[M1]** Every `dead` delivery has `completed_at` and `dead_reason`
  non-null; conversely `dead_reason IS NOT NULL` implies `state = 'dead'`.
- **[M2]** Every `pending` delivery also has `claimed_at` and `lease_token`
  null.
- **[M2]** Every `in_flight` delivery has non-null `claimed_at` and
  `lease_token`, and `completed_at` null.
- **[M2]** Every `succeeded` delivery has `completed_at` non-null.
- **[M2]** Every transition out of `in_flight` must match
  `lease_token = :token`; mismatched tokens mutate no state.
- **[M2]** `attempts` is a **resettable retry-budget counter** on `Delivery`. It
  increments only when a token-guarded transition records an attempt that was
  actually dispatched (payload sent), resets to 0 on replay, and is never
  derived from attempt rows. A `url_rejected` row has `dispatched_at` null and
  does not increment it.
- **[M2]** `Delivery.attempt_seq` is monotonic and never resets. It is
  incremented **inside the claim CAS**, and the value returned is that claim's
  `attempt_number`: sent as `X-Webhook-Attempt` and used on the attempt row. The
  number the receiver saw therefore always matches the audit row, and the
  database `UniqueConstraint(delivery, attempt_number)` holds because every
  claim gets a distinct value. Gaps occur when a claim never records an attempt (crash, reclaim).
- **[M2]** A stale worker that loses the token guard still commits its
  `DeliveryAttempt` row, with the `attempt_number` it was allocated at claim,
  so the request that really happened is auditable. It must not change delivery
  state or the `attempts` budget.
- **[M2]** The `DeliveryAttempt` insert and the `Delivery` transition commit in
  one DB transaction; if the guard matches zero rows, the attempt row is still
  committed alone. Attempt rows are immutable: no `UPDATE`, no `DELETE`.
- **[M2]** A `succeeded` delivery is never sent again.
- **[M2]** `lease_expiry_count` increments only on sweeper/manual reclaim of an
  expired lease; normal claims and retries never consume it.
- **[M2]** A worker claims exactly one row immediately before dispatch; the
  lease starts at `claimed_at`, not at batch pickup.
- **[M2]** The picker and the claim CAS share the same endpoint-usable
  predicate.
- **[M2]** `endpoint.paused_until` only pauses claims. Pending rows for a
  paused endpoint stay `pending` and are never dead-lettered; only permanent
  disable (`is_active = false`) or deletion dead-letters them.
- **[M2]** `endpoint.consecutive_failures` increments atomically
  (`SET consecutive_failures = consecutive_failures + 1`) per dispatched failed
  attempt, and resets on success. It is updated in its own short transaction
  **after** the delivery transaction commits, and a success writes it only when
  the value is non-zero, so a healthy endpoint is never a hot row. It is a
  heuristic, not an audit counter: a lost update only delays or skips a pause.
  `url_rejected` attempts, which dispatch nothing, do not count. Reaching 20
  triggers the pause; it is never a dead-letter trigger.
- **[M2]** All state transitions go through one module, `core/transitions.py`,
  never raw `.save()` calls across views or tasks.
- **[M2]** Endpoint and tenant deletion is soft (`is_active = false`,
  `deleted_at`) or `PROTECT`; `Delivery`/`DeliveryAttempt` rows are never
  cascade-deleted.

## Failure matrix

| Scenario | Observed state | Milestone | Recovery |
|---|---|---|---|
| Receiver returns 2xx | `succeeded` | M2 | Terminal success; attempt logged in the same transaction |
| Receiver returns non-2xx or times out (M2 rule) | `dead` (`non_2xx` / `timeout` / `network_error`) | M2 | Immediately dead until classification lands in M3 |
| URL fails SSRF validation before dispatch | `dead` (`url_rejected`), attempt row with `dispatched_at` null | M2 | No request sent, no retry; terminal |
| Duplicate fan-out for same event | row unchanged | M1 | `UniqueConstraint(event, endpoint)` skips duplicate creation |
| Endpoint permanently disabled at fan-out | `dead` (`endpoint_disabled`) | M1 | Row still created (audit + replay); no request. A deleted endpoint is unsubscribed and gets no row |
| Endpoint disabled or deleted after fan-out | `pending → dead` (`endpoint_disabled`) | M2 | The disable/delete operation reconciles in the same transaction; `reconcile_disabled` and (M3) the sweeper catch stragglers |
| CLI/worker killed mid-send | `in_flight` with stale lease | M2 | Manual `reclaim_leases --older-than 60s` → `pending`; sweeper takes over in M3 |
| Receiver returns 5xx, 408, 429, or times out | `pending` (`attempts > 0`) | M3 | Exponential backoff with jitter over a 24h+ window (respecting `Retry-After` on 429) |
| Receiver returns 400/401/403/404/422 | `dead` (`permanent_4xx`) | M3 | Immediate terminal failure; no wasted retries |
| Receiver returns 3xx | `dead` (`redirect_not_followed`) | M3 | Redirects never followed; terminal by policy |
| Receiver returns 410 Gone | `dead` (`permanent_4xx`) + endpoint permanently disabled | M3 | Endpoint disabled (`endpoint_gone_410`); remaining pending rows reconciled to `dead` (`endpoint_disabled`) |
| 20 consecutive dispatched failures | endpoint paused | M3 | `paused_until` + 15m gates claims; the failed delivery follows its ordinary classification and pending rows wait; nothing is dead-lettered by the pause |
| Half-open probe fails | endpoint re-paused | M3 | New 15m pause; the probe delivery follows its ordinary classification; claims stay gated until `paused_until` |
| Probe worker crashes | endpoint half-open, probe lease lapses | M3 | Probe lease (45s) expires; the next claim becomes the new probe |
| Two workers race different rows on a half-open endpoint | exactly one probe | M3 | The endpoint-row gate admits one claim; the other transaction rolls back |
| Attempt budget exhausted | `dead` (`budget_exhausted`) | M3 | No further attempts; eligible for replay when the endpoint is usable |
| Worker dies before/after sending | `in_flight`, stale lease | M3 | Sweeper → `pending`, or `dead` (`crash_loop`) at the expiry cap |
| Redis flushed or enqueue lost | `pending`, `next_attempt_at` in past | M3 | Sweeper polls due rows and re-enqueues |
| Endpoint hard-deleted | impossible | M1 | Soft delete / `PROTECT` keeps history |

## Signing and headers

| Header | Value |
|---|---|
| `X-Webhook-Id` | Event ID (the receiver's dedupe key) |
| `X-Webhook-Attempt` | Claim's monotonic `attempt_number` (hint; gaps possible) |
| `X-Webhook-Timestamp` | Unix seconds at send time |
| `X-Webhook-Signature` | One or more `v1=<hex>` values, comma-separated during rotation |

The signed string is `<timestamp>.<raw_body>`: the timestamp is **inside** the
MAC, not decorative. Receivers reject requests whose timestamp is more than
5 minutes from their clock, which closes the replay window together with
event-ID dedupe. The body is the captured `Event.payload_bytes` sent
byte-for-byte; neither side may re-serialize it. The timestamp is generated
fresh on every attempt.

The header format and verification support multiple `v1=` values from M2, so
rotation (M3) is additive.

**Secret rotation (M3):** each endpoint holds an `active_secret` plus an
optional `previous_secret`. During overlap the worker signs with **both** and
sends both `v1=` values, newest first; receivers verify against the secrets they
hold and accept if any value matches. A rotated-out secret stays valid for a 24h
overlap, then is dropped. Signing only with the active secret would break every
receiver that has not deployed the new secret yet, so it is explicitly not the
design.

**Secret storage (M1):** secrets must be recoverable to sign, so they are never
hashed. They are encrypted at rest with an application key from the
environment/KMS, decrypted only inside the worker, excluded from logs and
error reports, and never returned by API responses after creation.

**API key storage (M1):** API keys only need to be verified, never recovered,
so they are stored as a hash of a high-entropy random token with a non-secret
lookup prefix, shown once at creation, and compared in constant time.

## SSRF and outbound HTTP policy

Webhook URLs are attacker-controlled; this is part of delivery (M2), not a
later hardening pass.

- **Scheme and port:** `https` on port 443 only. There is no global switch that
  relaxes this or the address checks. Local development and tests use a
  separate `WEBHOOK_DEV_ALLOWLIST` of exact `host:port` entries (for example
  `127.0.0.1:8000`) that bypasses the scheme, port, and address checks for
  those entries only. The app refuses to start if the allowlist is non-empty
  while `DEBUG` is off, so it cannot become a production kill switch.
- **URL shape:** reject URLs with embedded credentials (`user:pass@host`).
- **No ambient network config:** the HTTP client is built with `trust_env=False`
  so `HTTP(S)_PROXY`, `NO_PROXY`, and `.netrc` can never reroute a request
  around the IP pinning.
- **Address ranges:** reject unless every resolved address (all A and AAAA
  records) is public. Block loopback, RFC1918, link-local (including
  `169.254.169.254`), CGNAT `100.64.0.0/10`, multicast, reserved, IPv6
  ULA/link-local, and `localhost`/`.local`/`.internal` names.
- **Literal forms:** accept only canonical parsers. Reject IPv4-mapped IPv6
  (`::ffff:127.0.0.1`), IPv4-compatible IPv6, and non-decimal IPv4 literals
  (octal/hex/`127.1`) explicitly, with tests for each. These are the classic
  bypasses.
- **DNS rebinding:** resolve immediately before connecting, validate the
  resolved IPs, and connect to the validated IP while keeping TLS SNI and
  certificate verification bound to the hostname (custom `httpx` transport or
  equivalent). Never reuse a cached resolution across attempts.
- **Redirects:** never followed. A 3xx is terminal
  (`redirect_not_followed` in M3; non-2xx dead in M2).
- **Deadline starts before DNS:** the 10s monotonic deadline is armed before
  name resolution. `getaddrinfo` has no timeout and can stall for tens of
  seconds, so resolution runs in a bounded resolver (thread/future with the
  remaining budget) and is abandoned when the deadline fires. The clock never
  starts after DNS.
- **Wall-clock enforcement:** 10s total per attempt, enforced by the monotonic
  deadline, which aborts and closes the connection. Connect timeout 2s and
  read-idle timeout 5s are subordinate to it. Per-socket read timeouts alone
  let a receiver drip bytes for minutes, outlive the 45s lease, and cause the
  sweeper to create a duplicate send.
- **Response body cap:** read at most 64 KiB of **decoded** bytes, then stop and
  close. Compression is not requested and decompression is size-capped, so a
  small compressed body cannot expand into memory. Never buffer an unbounded
  response.
- **Stored response data:** at most 1 KiB of status/error text per attempt,
  sanitized; tenant-visible fields must not leak internal network details,
  DNS answers, or error strings that could aid exfiltration.
- **Per-destination-host rate limit (M4):** a token bucket per hostname/port so
  the service cannot be used as an attack relay against one public target.
- **Egress allowlist / proxy (M4).**

## Per-endpoint and tenant protection

Two different mechanisms, deliberately separated:

- **Pause (temporary, M3):** 20 consecutive dispatched failures set
  `paused_until = now() + 15m` and `pause_reason = 'consecutive_failures'`.
  Claim guards skip paused endpoints, so every `pending` row stays pending and
  no row's `next_attempt_at` is modified. The pause is purely a claim gate and
  never dead-letters; the failed delivery follows its ordinary classification.
- **Half-open probe (M3):** once `paused_until` passes while `pause_reason` is
  still set, the endpoint is half-open. Exactly one claim may become the probe,
  and the gate is an **endpoint-row CAS**, not a per-delivery check: the claim
  transaction first runs
  `UPDATE endpoints SET paused_until = now() + interval '45s' WHERE id = :id AND pause_reason IS NOT NULL AND paused_until <= now()`,
  then the delivery CAS. If either updates zero rows the whole transaction rolls
  back, so two workers holding two different due rows for the same endpoint
  cannot both become probes: the second sees the 45s probe lease and loses. A
  successful probe clears `paused_until`, `pause_reason`, and
  `consecutive_failures`; a failed probe re-pauses for 15m; a crashed probe
  lets the 45s lease lapse so the next claim becomes the probe. M3 includes a
  two-worker race test on a half-open endpoint that yields exactly one probe.
- **Backlog release:** rows whose `next_attempt_at` passed during the pause
  become claimable once the probe succeeds, because nothing was rescheduled.
  Until the per-endpoint concurrency cap lands in M4 they are drained at
  whatever concurrency the workers have. This is a known M3 limitation, not a
  design goal.
- **Hot-row avoidance:** endpoint counters are updated in their own short
  transactions after delivery commits, and the success path writes only when
  the counter is non-zero, so one busy endpoint does not serialize its
  completions on a single row.
- **Permanent disable:** only 410 Gone or an operator sets
  `is_active = false` (plus `disabled_at`, `disabled_reason`). Pending rows for
  that endpoint are reconciled to `dead` (`endpoint_disabled`); they are never
  silently left pending or silently retried. Re-enabling an endpoint does not
  resurrect those rows; replay is the explicit, audited path.
- **Per-endpoint concurrency cap (M4):** at most 4 in-flight requests per
  endpoint, so one slow receiver cannot pin workers.
- **Per-tenant fairness (M4):** endpoints are served round-robin across
  tenants; one tenant's backlog cannot starve another's.
- **Fan-out gating:** a permanently disabled endpoint still gets its delivery
  row (initial state `dead`, `endpoint_disabled`) for audit and replay; a
  paused endpoint gets `pending` rows that wait for `paused_until`.
- **Worst-case cost:** with `max_attempts = 10`, a dead endpoint costs at most
  10 requests per event, and at most 20 consecutive failures before the pause
  trips; after that it costs one probe per pause window and nothing while
  paused.

## Timing & budget parameters

### M2 active defaults

- Wall-clock attempt deadline: **10s**, armed before DNS resolution
- Connect timeout: **2s**; read-idle timeout: **5s** (both under the deadline)
- Response body cap: **64 KiB**; stored body snippet cap: **1 KiB**
- Max HTTP attempts (`max_attempts`): **1** in M2
- Lease duration: **45s** (deadline plus persistence slack); enforced by the
  sweeper in M3
- Manual reclaim: `--older-than` default **60s** (45s lease + 15s slack); the
  guard uses exactly the value passed to the command

### M3 / M4 target defaults (columns created in M2)

**Budget vs numbering:** `attempts` is the resettable retry budget; `0` means
no attempt consumed yet. `attempt_number` is the monotonic claim number. The
backoff schedule is driven by the budget, not the claim number. After recording
an attempt, if `attempts < max_attempts`, the delay before the next attempt is:

```
delay = min(28800, 300 * 2 ** (attempts - 1)) * uniform(0.8, 1.2)   # seconds
```

With `max_attempts = 10` this gives (nominal, before jitter):

| Attempt | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|
| Delay | 5m | 10m | 20m | 40m | 1h20 | 2h40 | 5h20 | 8h (cap) | 8h (cap) |

Total retry window is **~26.6h nominal** (roughly 21–32h with ±20% jitter), so
a receiver down for a 20-minute deploy loses nothing. The 8h cap is reached at
attempt 9; it is not dead code. A replay resets `attempts`, so the schedule
restarts from 5m, while `attempt_number` keeps increasing.

- Max HTTP attempts (M3): **10**
- Max lease expiries (M3): **3** (crash-loop breaker; separate from the retry
  budget)
- Sweeper interval (M3): **30s** (periodic Celery Beat task)
- Consecutive-failure pause (M3): **20** failures → 15 minute `paused_until`
  → half-open probe with a **45s** probe lease
- Per-endpoint in-flight cap (M4): **4**
- Per-host request rate (M4): token bucket, one bucket per destination
  hostname/port
- `Retry-After` clamp (M3): `min(28800, max(300, retry_after_seconds))`
- Retention (M4): terminal rows retained until an explicit audit-retention
  decision; no purge job before then. Endpoint/tenant deletion is soft.

## M2 exit checklist

**M1 handoff (built in M1, verified here, not re-migrated):** `Tenant`,
hashed API keys, `Event` (with `payload_bytes` and a tenant-scoped idempotency
key), `Endpoint` (`is_active`, `deleted_at`, `disabled_at`, `disabled_reason`,
encrypted signing secret), `Delivery` (`state`, `next_attempt_at`,
`dead_reason`, `completed_at`); `unique(event, endpoint)` and
`unique(tenant, idempotency_key)`; the `pending` and `dead` check constraints;
tenant FKs and composite cross-tenant FKs via `RunSQL`, with a minimal
database-rejection test; no plaintext secret or API key in any migration,
fixture, or log.

- [ ] `DeliveryAttempt` model plus the remaining `Delivery` execution columns
  (`claimed_at`, `lease_token`, `attempts`, `attempt_seq`,
  `lease_expiry_count`) and `endpoint.paused_until`, `endpoint.pause_reason`,
  `endpoint.consecutive_failures`.
- [ ] `UniqueConstraint(delivery, attempt_number)` exists in the migration
  itself, not only in a test that asserts it.
- [ ] `CheckConstraint`s completed and bidirectional: `pending` also requires
  claim fields null; `in_flight` ⇒ claim fields set and `completed_at` null;
  `succeeded` ⇒ `completed_at` set; terminal states ⇒ `completed_at` set.
  (`pending` and `dead` core constraints already exist from M1.)
- [ ] Partial index `(next_attempt_at) WHERE state = 'pending'` backs the
  picker.
- [ ] Endpoint/tenant deletion is soft (`is_active`, `deleted_at`,
  `disabled_reason`); delivery/attempt FKs use `PROTECT`, never `CASCADE`.
- [ ] `core/transitions.py` enforces every allowed transition and rejects
  illegal ones (`succeeded → pending`, `dead → succeeded`, etc.).
- [ ] Claim = picker (`SELECT ... FOR UPDATE OF d SKIP LOCKED LIMIT 1` with the
  endpoint-usable predicate) plus per-row CAS that sets `lease_token` and
  increments `attempt_seq` in the same statement; one row claimed immediately
  before dispatch.
- [ ] CI runs the test suite against PostgreSQL (service container), never
  SQLite.
- [ ] Concurrency test: two workers race one `pending` row; exactly one wins,
  the loser makes no request and mutates nothing.
- [ ] No-prefetch test: a second candidate row is not claimed while the first
  request is in flight, so its lease cannot expire waiting.
- [ ] Head-of-line test: with many due rows for a paused or disabled endpoint
  ahead of it in `next_attempt_at` order, a due row for a healthy endpoint is
  still claimed.
- [ ] Attempt-number test: the `X-Webhook-Attempt` value sent equals the
  recorded `attempt_number`, including for a stale worker recording after its
  replacement; `unique(delivery, attempt_number)` is never violated.
- [ ] Audit test: a stale worker whose token guard fails still commits its
  `DeliveryAttempt` and changes neither state nor the `attempts` budget.
- [ ] Commit-atomicity test: forced failure between HTTP call and commit leaves
  attempt and state consistent (attempt row may exist alone; no state change
  without its attempt).
- [ ] Manual reclaim: `manage.py reclaim_leases --older-than 60s` moves stale
  `in_flight` rows to `pending`, increments `lease_expiry_count`, writes an
  audit entry, and refuses rows younger than the passed threshold.
- [ ] Outbound requests sign `<timestamp>.<payload_bytes>` with HMAC-SHA256 and
  send `X-Webhook-Id`, `X-Webhook-Attempt`, `X-Webhook-Timestamp`,
  `X-Webhook-Signature`. Tests cover valid, tampered body, stale timestamp,
  gaps in attempt numbers, and verification of a header carrying multiple
  `v1=` values. (The rotation lifecycle itself is M3.)
- [ ] The M1 storage rules still hold in the worker: secrets are decrypted only
  inside the worker and are absent from logs, errors, and post-creation API
  responses.
- [ ] Disable/delete reconcile: disabling or deleting an endpoint moves its
  `pending` rows to `dead` (`endpoint_disabled`) in the same transaction (a
  forced failure rolls both back); `manage.py reconcile_disabled` moves rows
  that were missed; an `in_flight` row is left to finish.
- [ ] URL validation rejects non-443 ports, loopback, private, link-local/
  metadata, CGNAT, reserved, IPv4-mapped/compatible IPv6, and non-decimal IPv4
  literals; re-checks every attempt; pins TLS SNI/cert verification to the
  hostname; never follows redirects; rejects embedded credentials; ignores
  proxy environment variables (`trust_env=False`); caps decoded body size. The
  dev allowlist bypasses only its exact `host:port` entries, and the app
  refuses to start with it non-empty while `DEBUG` is off. Tests cover each
  case and a 3xx response.
- [ ] Deadline is armed before DNS; a hanging-resolver test proves the attempt
  ends by 10s. A slow-drip receiver test also proves the 10s wall clock cannot
  be extended by per-read timeouts; the 64 KiB body cap is enforced.
- [ ] pytest suite covers fan-out idempotency (including duplicate rows),
  transition guards, illegal transitions, HMAC verification, SSRF rejection,
  concurrency, no-prefetch, head-of-line, reclaim, disable-reconcile, and
  end-to-end 200 OK vs 500 paths. The full tenant-isolation suite is M4.

## Decisions & non-goals

- **Pause is not disable.** `paused_until` throttles claims and leaves rows
  `pending`; only 410/manual disable (`is_active = false`) dead-letters, and it
  reconciles outstanding pending rows rather than orphaning them.
- **A pause never dead-letters and never reschedules.** It only gates claims;
  the failed delivery that tripped it follows its ordinary classification and
  retry budget.
- **Deleting an endpoint unsubscribes it.** Deleted endpoints get no new
  `Delivery` rows; pending rows are dead-lettered at deletion time.
- **No operator re-send for `succeeded`.** `succeeded` is terminal and
  `succeeded → pending` is deliberately rejected. To redeliver, the tenant
  creates a new Event with a new event ID; receivers dedupe on event ID, so
  reusing the old ID would be silently swallowed.
- **Replay is for `dead` only**, lands in M3, and requires a usable endpoint
  (`is_active`, not deleted). It resets the budget and `lease_expiry_count`,
  never `attempt_seq` or attempt rows; a paused endpoint defers replay until
  `paused_until`.
- **`attempts` is a budget, not a count of rows.** Replay resets it, stale
  audit rows do not touch it, and `X-Webhook-Attempt` comes from the separate
  monotonic `attempt_number`.
- **The recipient set is fixed at fan-out time.** Endpoints created later do
  not receive earlier events; replay covers failures, not late subscribers.
- **A blocked URL is terminal** (`url_rejected`): no dispatch, no retry, one
  immutable attempt row with `dispatched_at` null.
- **No follow-through on 3xx.** Redirects are a security hazard, not a
  delivery feature.
- Strict FIFO ordering, exactly-once delivery, priority queues, and
  multi-region active-active workers are out of scope, mirrored in the README
  non-goals.
