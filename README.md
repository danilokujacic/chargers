# ocpp-poc

A proof-of-concept OCPP 1.6 Central System (CSMS): a WebSocket server that charge points connect
to, backed by MongoDB. Implements charge point authentication, commissioning, the normal charge
flow (authorize → start → meter values → stop), and remote start/stop.

For the OCPP background, the build history, and what's still unimplemented, see
[instructions/](instructions/) — start with [instructions/README.md](instructions/README.md).
How the Central System works is explained in [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md); the public
map platform built on it (the API, the PlugShare import, the website and the demo fleet — tasks
07–11) in [PLATFORM_GUIDE.md](PLATFORM_GUIDE.md).

## Deploying to a server

Everything — MongoDB, Redis, the Central System, the public API, the website and HTTPS — runs in
Docker on one Ubuntu VPS, driven by `make` (`make install-docker`, `make setup`, `make deploy`).
See [DEPLOY.md](DEPLOY.md). The rest of this README is about running it for development.

## Requirements

- Python 3.13
- MongoDB reachable at `mongodb://localhost:27017` (override with `MONGODB_URL`)

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Running the Central System

```powershell
.\.venv\Scripts\python.exe main.py
```

Listens on `ws://0.0.0.0:9000`, using MongoDB database `ocpp_poc` (override with `MONGODB_DB`).

## Registering charge points

Every charger authenticates with HTTP Basic auth: its identity as the username, a 20-byte key
as the password (OCPP-J 1.6 s6.2.2). Two ways to issue one:

```powershell
# Seed chargers with a key already installed (Route A) -- writes charge_point_credentials.json
.\.venv\Scripts\python.exe seed.py --identities CP001 CP002

# Or register one that onboards over OCPP instead (starts Pending, Route B)
.\.venv\Scripts\python.exe register_charge_point.py register CP001
.\.venv\Scripts\python.exe register_charge_point.py list
.\.venv\Scripts\python.exe register_charge_point.py rotate CP001
```

## Simulating a charger

```powershell
# Run the full scripted session: boot, authorize, start, meter values, stop
.\.venv\Scripts\python.exe simulate_charge_point.py --cp-id CP001 --stop

# Connect and idle, waiting to be remotely started/stopped (see below)
.\.venv\Scripts\python.exe simulate_charge_point.py --cp-id CP001 --idle-seconds 60
```

Run `--help` on any script for the full set of flags (key rotation, rejecting a remote command,
`AuthorizeRemoteTxRequests` mode, etc.).

## Remote start/stop

The Central System exposes a small admin API (multiplexed on the same port as the OCPP
endpoint) for starting or stopping a *connected* charger remotely. Set `ADMIN_TOKEN` before
starting `main.py` — the admin API answers 404 for everyone if it isn't set — then:

```powershell
$env:ADMIN_TOKEN = "some-shared-secret"
.\.venv\Scripts\python.exe operate.py remote-start CP001 TAG001
.\.venv\Scripts\python.exe operate.py remote-stop CP001 42
```

## Demo fleet

**Demo data.** Turns every imported PlugShare site into a simulated one: a mock OCPP charger per
PlugShare station, one connector per outlet, driven live by one fleet process
([instructions/11-demo-fleet.md](instructions/11-demo-fleet.md)). The map then shows live
statuses for real chargers this system has never talked to, so remove it when the demo is over
([instructions/12-remove-demo-fleet.md](instructions/12-remove-demo-fleet.md)).

MongoDB and Redis must be running, and `.env` filled in (`REDIS_URL`, `CORS_ORIGINS`,
`ADMIN_TOKEN`). Run these in order, from this directory unless noted:

```
python main.py                                            # terminal 1
uvicorn api.app:app --host 0.0.0.0 --port 8000            # terminal 2
python import_plugshare_sites.py                          # once; skip if already imported
python seed_demo_fleet.py                                 # once; safe to re-run
python run_demo_fleet.py                                  # terminal 3; Ctrl+C to stop
cd ../charger-fe && npm run dev                           # terminal 4, then open :3000
```

`seed_demo_fleet.py` writes the mock chargers' keys to `demo_fleet_manifest.json` (the only
copy); `run_demo_fleet.py` keeps its energy registers and open sessions in
`demo_fleet_state.json`. Both are gitignored. Try `python operate.py remote-start PS-2946795
DEMO-REMOTE` against a running fleet.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Spins up MongoDB-backed fixtures and an in-process Central System on an OS-assigned port; skips
cleanly (exit 0) if MongoDB isn't reachable.

`test_connector_state_machine.py` is a standalone script (not part of the pytest suite) that
checks the connector status transition table on its own:

```powershell
.\.venv\Scripts\python.exe test_connector_state_machine.py
```

## Layout

| File | Role |
|---|---|
| `main.py` | The Central System: WebSocket server, HTTP Basic auth, OCPP message handlers, admin API |
| `models.py` | MongoDB documents (Beanie): charge points, idTags, transactions, connector status |
| `commissioning.py` | Route B onboarding — pushing a unique key to a charger over OCPP |
| `connector_state_machine.py` | The OCPP 1.6 s4.9 connector status transition rules, dependency-free |
| `seed.py` / `register_charge_point.py` | Operator CLIs for provisioning charge points |
| `simulate_charge_point.py` | A scriptable simulated charger, for testing against `main.py` |
| `operate.py` | Operator CLI for remote start/stop against a connected charger |
| `seed_demo_fleet.py` / `run_demo_fleet.py` | The demo fleet: mock chargers for the PlugShare sites, and the process that runs them |
| `ocpp_client_auth.py` | Shared HTTP Basic auth header helper |
| `tests/` | pytest suite |
| `ocpp-docs/` | The OCPP 1.6 specification PDFs this project is built from |
