# Deploying to a VPS

Everything runs in Docker, on one Linux server, driven by `make`. This page is the whole procedure;
`make help` lists every command.

## What gets deployed

```
                         ┌────────────── one server (Docker) ──────────────────┐
 browsers ── https ──▶ caddy ──▶ frontend (Next.js website)                    │
                       │    └─▶ api (public API, /api/*) ──┐                   │
 chargers ── wss ────▶ │ ocpp.<domain> ──▶ cs (main.py) ───┼──▶ mongo          │
                       │                                   └──▶ redis          │
                         └─────────────────────────────────────────────────────┘
```

| Service | Image | What it is | Reachable from outside? |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | HTTPS for both domains, certificates from Let's Encrypt, renewed automatically | Yes: ports 80 and 443 |
| `frontend` | built from `../charger-fe` | The public map | Through Caddy: `https://DOMAIN/` |
| `api` | built from this repo | The public, read-only API and live WebSockets | Through Caddy: `https://DOMAIN/api/…` (docs at `/docs`) |
| `cs` | built from this repo | The Central System, `main.py`: chargers connect here | Through Caddy: `wss://OCPP_DOMAIN/<identity>`. Its own port 9000 only on `127.0.0.1`, unless you choose otherwise |
| `mongo` | `mongo:7.0` | The database, password-protected | No |
| `redis` | `redis:7-alpine` | Live events from `cs` to `api`, password-protected | No |
| `fleet` | built from this repo | Demo only: 195 simulated chargers (`make demo-up`) | No |
| `tools` | built from this repo | One-off commands: migrations, seeds, operator CLIs | No |

Data lives in Docker volumes (`mongo-data`, `app-data` for charger keys and the demo's files,
`caddy-data` for certificates), so it survives rebuilds and restarts. Every container restarts on
its own after a crash or a reboot. Logs are rotated (5 × 20 MB per service).

## What you need

- **An Ubuntu server** (22.04 or 24.04). 2 vCPUs and 4 GB of RAM are comfortable. Running, the stack
  uses about 0.5 GB, but building the website needs about 2 GB: on a 2 GB server, add swap first.
  About 5 GB of disk for the images.
- **Two DNS records** pointing at the server: `DOMAIN` (the website, e.g. `charge.example.com`) and
  `OCPP_DOMAIN` (for chargers, by default `ocpp.charge.example.com`).
- **Ports 80 and 443 open** to the internet (Let's Encrypt needs port 80 to issue certificates).
- **A MapTiler API key** ([cloud.maptiler.com](https://cloud.maptiler.com/account/keys/)). It is
  visible in every visitor's browser by design, so restrict it to your domain there.
- **Both repositories**, side by side: this one and the website (`charger-fe`). `make setup` clones
  the website for you if it is missing (`FRONTEND_REPO=…` to choose the URL; a private repository
  needs a deploy key on the server).

## First install

```bash
git clone git@github.com:danilokujacic/chargers.git
cd chargers

make install-docker            # Docker Engine + Compose, make, git, openssl. Uses sudo.
                               # Then log out and back in, so your user can run docker.

make setup                     # asks for DOMAIN and MAPTILER_KEY; or, non-interactively:
                               # make setup DOMAIN=charge.example.com MAPTILER_KEY=...

make deploy                    # build, start the databases, migrate, load the PlugShare
                               # sites, start everything, and check it
```

`make setup` writes **`.env.production`** (mode 600, gitignored) with generated passwords for
MongoDB and Redis and the admin token. It never overwrites an existing file: it only checks it.
Keep a copy of that file somewhere safe; the database password in it is what opens your data.

`make deploy` ends with `make status`, which should show every container `healthy`, the website
answering HTTP 200 and the API listing 134 sites. The first HTTPS certificate can take a minute; if
the website is not reachable yet, check that DNS points at the server (`make logs SERVICE=caddy`
shows Let's Encrypt's answers).

**No domain yet?** `make setup DOMAIN=<server IP> OCPP_DOMAIN=<server IP> SCHEME=http` runs
everything over plain HTTP, for a first look. Switch to a real domain later by deleting
`.env.production`, running `make setup` again, and then `make build up`.

## Everyday commands

| Command | What it does |
|---|---|
| `make status` | Container health, and a check of the website and API through Caddy |
| `make logs SERVICE=cs` | Follow one service's logs (`cs`, `api`, `frontend`, `caddy`, `mongo`, `redis`, `fleet`); without `SERVICE`, all |
| `make up` / `make down` / `make restart SERVICE=api` | Start, stop (data is kept), restart |
| `make update` | `git pull` both repositories, rebuild, migrate, restart |
| `make migrate` / `make migrate-status` | Apply / list database migrations |
| `make seed` | Load or refresh the PlugShare reference sites (safe to re-run) |
| `make backup` / `make restore FILE=… CONFIRM=yes` | Backups (below) |
| `make chargers` | List registered chargers |
| `make register-charger ID=CP042` | Register a real charger; prints its key **once** |
| `make rotate-key ID=CP042` | Issue a new key for a charger |
| `make operate ARGS="remote-start CP042 TAG001"` | Any `operate.py` command against the running Central System |
| `make shell` / `make mongo-shell` | A shell in the backend image / a MongoDB shell |
| `make test` | The test suite, inside the backend image (uses a throwaway database) |

## Migrations and seed data

- **Migrations** (`migrate.py`, `make migrate`): each runs once, in order, and is recorded in the
  `schema_migrations` collection, so `make deploy` and `make update` run it every time. MongoDB has
  no schema to alter; a migration here creates indexes or backfills data. To add one, append it to
  `MIGRATIONS` in `migrate.py` (instructions at the top of the file).
- **Seed** (`make seed`): the 134 PlugShare sites from `montenegro_only.json`, as reference sites
  (shown as faint "unknown" pins). Re-running updates them and never duplicates.
- **Chargers are not seeded** in production: each real one is registered (`make register-charger`).

## Connecting a real charger

1. `make register-charger ID=<identity>`: the identity is the name the charger puts in its URL. The
   command prints a 40-character key once.
2. Configure the charger with the Central System URL `wss://OCPP_DOMAIN/` (it appends its identity)
   and HTTP Basic authentication: username = identity, password = the key.
3. It boots as **Pending**; the Central System then pushes it a fresh key over OCPP and accepts it
   (Route B, `SYSTEM_OVERVIEW.md` §7). A charger with the key installed in the factory can be
   registered as accepted with `make shell` → `python seed.py --identities <id> --credentials /data/charge_point_credentials.json`.
4. Put it on the map: `make operate ARGS='create-site "My site" parking 42.43 19.26'`, then
   `make operate ARGS="set-charge-point-location <identity> --site-id <site id>"`.

**Chargers that cannot do TLS** can connect to `ws://<server>:9000/<identity>` if you set
`CS_BIND=0.0.0.0` in `.env.production`, run `make up`, and open port 9000. Keys then travel
unencrypted, and the admin API (token-protected) shares that port, so prefer TLS.

Firmware files for `UpdateFirmware` go in `firmware_files/` (mounted read-only into `cs`);
chargers download them from `https://OCPP_DOMAIN/firmware/<file>`.

## The demo fleet

Demo data (`instructions/11-demo-fleet.md`): simulated chargers on the 134 PlugShare sites, so the
map comes alive. Labelled "Demo data — simulated chargers." on every site.

```bash
make seed-demo      # once: 195 simulated chargers, 256 demo cards, the key manifest in app-data
make demo-up        # start it; all 195 connected within about 30 s
make demo-down      # stop it cleanly: sessions end, chargers switch off (map: 124 grey, 10 red)
```

`make operate ARGS="remote-start PS-2946795 DEMO-REMOTE"` shows the operator side. Removing the
demo data is task 12, not built yet.

## Backups

`make backup` writes two files into `backups/`: a compressed MongoDB dump and an archive of the
`app-data` volume (charger keys, the demo fleet's manifest and state). Copy them off the server.
A nightly backup at 03:00 (`crontab -e`), keeping 14 days:

```
0 3 * * * cd /home/<you>/chargers && make backup >/dev/null 2>&1 && find backups -mtime +14 -delete
```

`make restore FILE=backups/mongo-<stamp>.archive.gz CONFIRM=yes` stops the demo fleet, replaces the
database, and restores the `app-data` archive from the same backup if it is next to it, so the
demo's memory of open sessions matches the database. Start the fleet again afterwards if needed.

## Security notes

- **Secrets** are only in `.env.production` (mode 600, never committed). MongoDB and Redis are not
  published on any port, and logs show the database URL with its password masked.
- **The admin API** (which can command chargers) is bound to `127.0.0.1` and blocked on
  `OCPP_DOMAIN`; use it through `make operate`.
- **Firewall.** Allow SSH, 80 and 443 (`sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable`).
  Docker publishes its ports past `ufw`, so the port list in `docker-compose.yml` is what is really
  open: only 80, 443, and 9000 on localhost.
- **Before going public**, read `PLATFORM_GUIDE.md` §4: the live WebSocket currently forwards driver
  card numbers, and the public API has no rate limiting.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No .env.production yet` | `make setup` |
| `make status`: website not reachable | DNS not pointing here yet, or port 80/443 closed; `make logs SERVICE=caddy` |
| The map loads but shows no pins, or the page errors | `make logs SERVICE=frontend`; the website fetches the API through Caddy on its first render |
| Changed `DOMAIN` or `MAPTILER_KEY` but the site did not change | The website bakes them in at build time: `make build up` |
| The build is killed | Out of memory: add swap, or build on a larger machine |
| A charger gets HTTP 401 | Wrong identity or key; `make rotate-key ID=…` and install the new key |
| A service keeps restarting | `make logs SERVICE=<name>`; `make ps` shows its health |

## Files

| File | Role |
|---|---|
| `docker-compose.yml` | The whole stack |
| `Makefile` | Every command above |
| `deploy/backend.Dockerfile` | The Python image (cs, api, fleet, tools) |
| `deploy/frontend.Dockerfile` (+ `.dockerignore`) | The website image, built from `../charger-fe` |
| `deploy/Caddyfile` | HTTPS and routing |
| `deploy/scripts/setup.sh`, `install-docker.sh` | `make setup`, `make install-docker` |
| `migrate.py` | Database migrations |
| `.env.production` | Your settings and secrets (created by `make setup`, never committed) |
