# Delivery state machine

Status: draft · Last updated: 2026-11-17
Scope: one `Delivery` row = one Event going to one Endpoint.

## Delivery guarantee

**At-least-once, never exactly-once.** A worker can crash after the HTTP
request is sent but before the attempt is recorded, so a receiver may see the
same event twice. Receivers must dedupe on the event ID. We never claim
exactly-once, and the API docs will say so.

## States

| State | Means | Terminal | Written by |
|---|---|---|---|
| pending | Due now or at `next_attempt_at`, no worker owns it | no | API (fan-out), sweeper |
| in_flight | A worker claimed it and is sending, within its lease | no | worker (claim) |
| succeeded | Receiver returned 2xx | yes | worker |
| retrying | Attempt failed, budget remains, waiting for backoff | no | worker |
| dead | Budget exhausted or unrecoverable | yes | worker, manual replay |

## Transitions

| From → To | Trigger | Actor | Guard |
|---|---|---|---|
| pending → in_flight | Worker picks up a due delivery | worker | atomic claim: `state='pending' AND next_attempt_at <= now()` |
| in_flight → succeeded | 2xx response | worker | — |
| in_flight → retrying | non-2xx / timeout / connection error | worker | `attempts < max_attempts` |
| in_flight → dead | non-2xx / timeout / connection error | worker | `attempts >= max_attempts` |
| in_flight → retrying | Lease expired (worker died mid-send) | sweeper | `claimed_at < now() - lease` |
| retrying → pending / in_flight | `next_attempt_at` reached | sweeper or worker | — |
| dead → pending | Operator replays | API/admin | records who and why |
| any → dead | Endpoint disabled or deleted | sweeper | — |

Open: does a replay reset the attempt budget, and does it reuse the delivery
row or create a new one? Pick one and write the reason here before M3.

## Invariants

1. Every non-terminal delivery has `next_attempt_at` set.
2. Every `in_flight` delivery has `claimed_at`, and a stale `claimed_at` is a
   bug signal, not a normal state.
3. `attempts` increments only when an HTTP request was actually made — never on
   enqueue or claim.
4. `unique(event, endpoint)` holds for all time; fan-out is therefore safe to
   re-run.
5. A `succeeded` delivery is never sent again.
6. State changes go through one function, not `.save()` calls sprinkled across
   views and tasks.

## Failure matrix

| Scenario | Observed state | Recovery |
|---|---|---|
| Worker dies before sending | in_flight, stale lease | sweeper → retrying |
| Worker dies after sending, before recording | in_flight, stale lease | sweeper → retrying ⇒ duplicate send |
| Redis flushed entirely | rows untouched, no triggers left | sweeper finds all due rows |
| DB committed, enqueue lost | pending, `next_attempt_at` in the past | sweeper |
| Receiver 500 | retrying | backoff |
| Receiver timeout | retrying | backoff |
| Receiver 429 | retrying | respect `Retry-After`? (decide) |
| Receiver 410 Gone | dead | endpoint should be disabled |

## Timing (settled in M3, placeholders now)

- HTTP timeout per attempt: __ s
- Lease duration: __ (must exceed the timeout)
- Sweeper interval: __
- Backoff: `min(cap, base * 2**attempt) * jitter`
- Max attempts: __ before dead

## Out of scope

Ordering guarantees between deliveries, exactly-once, priorities, cross-region.
Stated explicitly so the README can point here instead of apologising.

## Open questions

- [ ] Should a disabled endpoint kill queued deliveries or pause them?
- [ ] Does replay reset `attempts`?
- [ ] Are 4xx responses (other than 429 and 408) retried, or dead immediately?
