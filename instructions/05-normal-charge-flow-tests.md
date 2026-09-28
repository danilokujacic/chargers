# 05 — Tests for the normal charge flow

Write automated tests that drive the flow from `04-normal-charge-flow.md` end to end: a real
WebSocket connection, real HTTP Basic auth, real OCPP frames, real MongoDB documents.

Depends on `04-normal-charge-flow.md`.

## Framework

Use **pytest with pytest-asyncio**. Add to `requirements.txt`:

```
pytest==8.*
pytest-asyncio==1.*
```

Pin the exact versions you install. `.gitignore` already excludes `.pytest_cache/`.

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Layout:

```
tests/
  conftest.py              fixtures: database, server, registered charger, simulated charger
  test_normal_charge_flow.py
```

The existing standalone PASS/FAIL scripts (`simulate_charge_point.py` and friends) stay as they
are — they are demo tools, not the test suite. Do not convert them.

## Fixtures

### MongoDB

- Connect to `MONGODB_URL`, default `mongodb://localhost:27017`, with a **short server selection
  timeout** (5s) so an absent database fails fast.
- **`pytest.skip` the whole module when MongoDB is unreachable.** MongoDB is not installed on the
  development machine; a missing database must not look like a failing test. Use a session-scoped
  fixture that attempts a `ping` and skips on `PyMongoError`.
- Each test run gets a **uniquely named database** (e.g. `ocpp_test_<uuid4().hex[:8]}`), dropped in
  teardown. Never touch the default `ocpp_poc` database.
- `init_beanie` must run before any document is constructed — beanie 2.x raises
  `CollectionWasNotInitialized` otherwise, so this fixture has to be a dependency of every other
  one that touches documents.

### Server

- Start the Central System **in-process** in the test event loop, not as a subprocess: it is
  faster and failures surface as real tracebacks.
- Bind **port 0** and read the assigned port off the server object. Do not assume 9000 is free —
  it is frequently occupied by a stale process. Pass the resolved URL to the client fixture.
- This requires `main.py` to expose the server setup as a callable rather than only running it at
  import. Refactor `main.py` so `async def serve(host="0.0.0.0", port=9000)` returns the server
  object and the module-level `raise SystemExit(asyncio.run(main()))` calls it. Do not break the
  `python main.py` entry point.
- Tear down with `server.close()` and `await server.wait_closed()`.

### Charger

- A fixture that registers a charge point via `ChargePoint.register()` and yields
  `(identity, plaintext_key)`.
- A fixture that opens an authenticated OCPP connection using that pair and yields a started
  client, cancelling its listener task on teardown. Reuse the header construction from
  `simulate_charge_point.py` rather than duplicating base64 logic; extract it if needed.

## Tests to write

Group them so a failure names the step that broke.

### Happy path

1. **`test_full_charge_session`** — boot → authorize → Preparing → start → Charging → two
   `MeterValues` → stop → Finishing → Available. Assert on every response payload *and* on the
   resulting documents: one `Transaction` with the right `meter_start`, `meter_stop`, `id_tag`,
   `connector_id`, and timestamps equal to those the client sent.
2. **`test_transaction_id_is_allocated_not_hardcoded`** — run two sessions; assert the two
   `transaction_id` values differ and are both positive integers.
3. **`test_energy_is_recorded`** — `meter_stop - meter_start` equals the energy the client
   reported.
4. **`test_connector_status_persisted`** — after the session the stored `ConnectorStatus` for that
   connector is `Available`, and `ConnectorId 0` is untouched by connector-level changes.

### Authorization of the driver

5. **`test_unknown_id_tag_is_invalid`** — `Authorize` for an unregistered tag returns `Invalid`.
6. **`test_blocked_id_tag_is_refused`**.
7. **`test_expired_id_tag_is_expired`** — stored as `accepted` with a past `expiry_date`; the
   response must still be `Expired`.
8. **`test_parent_group_can_stop_transaction`** — tag B, sharing a `parent_id_tag` with tag A,
   stops A's transaction; an unrelated tag C cannot.

### State machine integration

9. **`test_illegal_transition_is_logged_but_persisted`** — send `Charging` directly after
   `Finishing`. Assert the status is stored and the anomaly logged (use `caplog`), and that no
   CALLError is returned.
10. **`test_connector_zero_rejects_charging_status`** — `StatusNotification(connectorId=0,
    Charging)` is logged as illegal per s4.9.
11. **`test_suspend_and_resume_keeps_one_transaction`** — Charging → SuspendedEV → Charging leaves
    exactly one open transaction.

### Robustness

12. **`test_duplicate_stop_transaction_is_idempotent`** — send the same `StopTransaction` twice;
    the stored document is unchanged after the second.
13. **`test_stop_for_unknown_transaction_is_recorded`** — a `StopTransaction` whose
    `transactionId` was never started is persisted and flagged incomplete, not dropped.
14. **`test_offline_timestamps_are_trusted`** — start and stop with timestamps two hours in the
    past; the stored transaction carries those timestamps, not server time, and is not rejected.
15. **`test_unauthenticated_connection_is_refused`** — no `Authorization` header gives 401 and no
    `Transaction` or `ConnectorStatus` document is created.
16. **`test_wrong_key_is_refused`** — 401.
17. **`test_identity_mismatch_is_forbidden`** — valid credentials for charger A used on charger
    B's URL gives 403.

## Rules

- **No `sleep` to synchronise.** Await the actual response; the OCPP library's `call()` already
  blocks until the conf arrives. A test that needs a delay is hiding a race.
- Pass `suppress=False` on every `call()` so a CALLError fails the test instead of returning
  `None`.
- Assert on **stored documents**, not only on response payloads. A handler can answer correctly
  and still write nothing.
- Each test must pass when run alone and when the file is run as a whole. No ordering
  dependencies, no shared mutable state between tests beyond the fixtures.
- Give every test a distinct charger identity and idTag so they cannot collide.

## Acceptance criteria

1. `.venv/Scripts/python.exe -m pytest` passes with MongoDB running.
2. With MongoDB stopped, the suite **skips** with a message naming MongoDB, and exits 0.
3. Every test above exists and passes.
4. The suite passes when port 9000 is already occupied by another process — prove it by starting
   a dummy listener on 9000 first.
5. No test takes longer than 5 seconds; the whole suite runs in under 60.
6. Running the suite twice in a row passes both times and leaves no `ocpp_test_*` databases
   behind.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, the use case is changing this code six months from now and knowing within a minute
whether billing still works.
