# Instructions for AI agents

Each file in this folder specifies one module or flow to build in this OCPP 1.6 proof of
concept. Read this file first, then the numbered file for your task.

## How to use these

- Implement exactly what the file specifies. Where it says MUST, that is a hard requirement,
  usually because the OCPP specification demands it.
- Every file ends with **Acceptance criteria**. You are not done until all of them hold, and
  you have run something that demonstrates it.
- Spec citations look like `OCPP 1.6 s4.9` (from `ocpp-docs/ocpp-1.6 edition 2.pdf`) or
  `OCPP-J 1.6 s6.2.2` (from `ocpp-docs/ocpp-j-1.6-specification.pdf`). Where a file quotes the
  spec, the quote is authoritative: do not "improve" on it.
- If an instruction contradicts the spec, stop and say so rather than guessing.
- Finish by writing the brief described in **Report when you finish** below. Every task ends with
  one.

## Report when you finish

Once the acceptance criteria pass, write a short brief in your reply. This is required, it is the
last step of every task, and it is not a changelog — no commit lists, no file diffs.

**Who it is for:** the owner of this project, who is a software developer but is **new to EV
charging**. Assume fluency in Python, MongoDB and HTTP. Assume nothing about chargers, OCPP,
EVSEs, idTags or connector states. Define any charging term the first time you use it, in a short
clause rather than a footnote.

Four sections, in this order:

1. **What is done** — the files you added or changed, one line each saying what each now does.
   Name the public functions or documents a reader would go looking for.
2. **The use case** — the real-world situation this part serves. Describe it as something
   that happens to a driver, a charger or an operator, not as a message exchange. "A driver
   taps their card and the charger has to ask us whether to let them charge" tells the
   reader more than
   "implements Authorize.req handling". One short paragraph.
3. **Why it is built this way** — only the decisions that were not obvious, and the reason.
   Include anything the spec forced, quoting or citing the section. Include anything you
   chose between,
   and what the alternative was. If a decision has a consequence the owner will later run into,
   say so plainly. Three to six bullets.
4. **Verification** — what you actually ran, and what it showed. Real numbers and real output, not
   "tests pass". If something is untested, or you could not test it, say which part and why.

Rules:

- Aim for 300–500 words. If it needs more, the task was too big and should have been split.
- Report faithfully. If an acceptance criterion does not hold, say which one and why, rather than
  reporting completion. A known gap stated plainly is useful; a gap discovered later is expensive.
- No praise for the code, no restating the instruction file back. The owner has read it.
- If you had to deviate from the instruction file, that goes in **Why**, with the reason. If the
  instruction file itself was wrong, say so — it should be corrected for the next agent.

## Order

| # | File | Depends on |
|---|---|---|
| 01 | [connector-state-machine.md](01-connector-state-machine.md) | nothing |
| 02 | [charge-point-authentication.md](02-charge-point-authentication.md) | nothing (already built; verify) |
| 03 | [commissioning-flow.md](03-commissioning-flow.md) | 02 |
| 04 | [normal-charge-flow.md](04-normal-charge-flow.md) | 01, 03 |
| 05 | [normal-charge-flow-tests.md](05-normal-charge-flow-tests.md) | 04 |
| 06 | [remaining-flows.md](06-remaining-flows.md) | 04, 05 |
| 06a | [remaining-flows-progress.md](06a-remaining-flows-progress.md) | 06 (read this before continuing 06 -- sections A-H are done, I and J are not) |
| 07 | [public-map-platform.md](07-public-map-platform.md) | 04, 05 for §A/§B; the frontend (now detailed in 10) additionally depends on 08 being built first. A new, separate public-facing platform -- does not depend on 06's remaining sections I/J. |
| 08 | [public-api-service.md](08-public-api-service.md) | 07's §A (the `Site` data model) must exist first |
| 09 | [plugshare-import.md](09-plugshare-import.md) | 07's §A (needs the `Site` document's `source`/`external_*` fields) |
| 10 | [frontend-nextjs.md](10-frontend-nextjs.md) | 08 (nothing to fetch or subscribe to before the API exists) |
| 11 | [demo-fleet.md](11-demo-fleet.md) | 04, 08, 09, 10 -- a simulated charger per PlugShare station, driven live by a fleet runner. Demo data. |
| 12 | [remove-demo-fleet.md](12-remove-demo-fleet.md) | 11 -- removes every piece of 11's data and restores 09's reference-only state |

## What already exists

Flat layout, no packages. Every module sits in the project root.

| File | Role |
|---|---|
| `main.py` | The Central System: WebSocket server on port 9000, HTTP Basic auth, OCPP handlers |
| `models.py` | Beanie `ChargePoint` document, authorization key hashing, `init_db()` |
| `seed.py` | Seeds charge points with random keys, writes `charge_point_credentials.json` |
| `register_charge_point.py` | Operator CLI: register / list / rotate / verify |
| `simulate_charge_point.py` | Charge point simulator that runs a scripted session |

`main.py` currently handles seven charger-initiated messages: `BootNotification`, `Heartbeat`,
`StatusNotification`, `Authorize`, `StartTransaction`, `MeterValues`, `StopTransaction`. The
handlers for `Authorize` and `StartTransaction` are stubs that accept everything and return a
hardcoded `transaction_id=1`.

## Conventions

- Python 3.13. The virtualenv is `.venv`; run things as `.venv/Scripts/python.exe <script>`.
- `ruff` config lives in `pyproject.toml`: line length 100, double quotes, rules `E,F,I,UP`,
  target `py312`. Ruff is not installed; keep lines under 100 columns by hand.
- Files use CRLF line endings, except `.gitignore` which uses LF. Match what you find.
- Docstrings on every public function and class. Comments explain *why*, not *what*, and cite
  the spec section when behaviour is driven by it.
- Scripts that are entry points end with `raise SystemExit(asyncio.run(main(parse_args())))`
  rather than an `if __name__ == "__main__":` block. Follow that.
- Argument parsing is `argparse` with long options only.
- Reuse the enums from the `ocpp` library (`ocpp.v16.enums.ChargePointStatus`,
  `RegistrationStatus`, `AuthorizationStatus`, …) instead of defining parallel ones. `models.py`
  already does this.
- Any new dependency must be added to `requirements.txt`, which is UTF-8 with pinned versions.

## Library behaviour you will otherwise get wrong

These cost time to discover. Trust them.

**beanie 2.2.0**
- It uses `pymongo.AsyncMongoClient`, *not* motor. `motor` is not installed and must not be.
- `Document.__init__` raises `CollectionWasNotInitialized` unless `init_beanie()` has run. You
  cannot construct a document, not even for a unit test, without a live MongoDB. Design pure
  logic (such as the state machine) so it needs no documents, and it stays unit-testable.
- `init_beanie(database=..., document_models=[...])`. Indexes go in the inner `Settings` class
  as `pymongo.IndexModel` objects.

**ocpp 2.0.0**
- Handlers registered with `@on(Action.x)` may be `def` or `async def`; both are awaited
  correctly.
- Handlers are always invoked with keyword arguments (`handler(**payload)`), so parameter order
  in your signature is irrelevant.
- Optional fields are *absent* from the payload, not `None`. Test with `if "x" in kwargs:`, not
  `kwargs.get("x")`, or you will overwrite stored values with `None`.
- `ChargePoint.start()` loops on `recv()` forever. Run it as a task; it raises
  `websockets.exceptions.ConnectionClosed` when the peer goes away.
- `call(payload, suppress=False)` raises on a CALLError. The default `suppress=True` silently
  returns `None`, which hides failures.
- Outgoing payloads are validated against the JSON schemas in `ocpp/v16/schemas/`, so
  over-long strings fail before they reach the wire.

**websockets 16.1.1**
- `websockets.serve(..., process_request=callable)` where the callable is
  `async def (connection, request) -> Response | None`. Returning `None` accepts.
- Do **not** use `websockets.asyncio.server.basic_auth()`. It decodes the credentials with
  `.decode()` guarded only by `except binascii.Error`, so a spec-conformant raw-byte password
  raises an uncaught `UnicodeDecodeError`. See `02-charge-point-authentication.md`.
- Client side: `connect(uri, additional_headers={...})`. A rejected handshake raises
  `websockets.exceptions.InvalidStatus` with `exc.response.status_code`.

## Environment

- MongoDB is reached through `MONGODB_URL` (default `mongodb://localhost:27017`) and
  `MONGODB_DB` (default `ocpp_poc`). `models.mongodb_url()` returns the former.
- MongoDB is **not** installed on the development machine by default. Anything that needs it
  must fail with a clear message, and tests must skip rather than fail when it is absent.
- Port 9000 may be occupied by a stale server. Tests MUST bind an ephemeral port instead of
  assuming 9000 is free.
