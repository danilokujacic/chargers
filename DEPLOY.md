# Deploying to a VPS

A step-by-step guide, from a freshly created Ubuntu server to the charging map running at your own
domain with HTTPS. Everything runs in Docker and is driven by `make`. Part 1 is the walkthrough — do
it once, top to bottom. Part 2 is the reference for running it afterwards.

Expect about 30 minutes, most of it waiting for DNS and the first build.

**Contents**

- Part 1 — First deployment
  1. [What you need](#1-what-you-need)
  2. [Create the server](#2-create-the-server)
  3. [Log in and create a user](#3-log-in-and-create-a-user)
  4. [Prepare the server](#4-prepare-the-server)
  5. [Point your domain at the server](#5-point-your-domain-at-the-server)
  6. [Get the code](#6-get-the-code)
  7. [Install Docker](#7-install-docker)
  8. [Configure](#8-configure)
  9. [Deploy](#9-deploy)
  10. [Check that it works](#10-check-that-it-works)
  11. [Set up backups](#11-set-up-backups)
- Part 2 — Running it
  - [What is running](#what-is-running) · [Everyday commands](#everyday-commands) ·
    [Updating](#updating) · [Migrations and seed data](#migrations-and-seed-data) ·
    [Connecting a real charger](#connecting-a-real-charger) · [The demo fleet](#the-demo-fleet) ·
    [Backups and restore](#backups-and-restore) · [Security](#security) ·
    [Troubleshooting](#troubleshooting) · [Files](#files)

---

# Part 1 — First deployment

## 1. What you need

| Thing | Details |
|---|---|
| **A VPS** | Ubuntu **24.04** (or 22.04), **2 vCPUs, 4 GB RAM, 25 GB disk** or more. The stack uses about 0.5 GB of memory while running, but building the website needs about 2 GB. A 2 GB server works if you add swap (step 4). Any provider: Hetzner, DigitalOcean, OVH, Contabo, AWS Lightsail… |
| **A domain name** | One you control, e.g. `example.com`. You will create two names in it: one for the website (e.g. `charge.example.com`) and one for chargers (`ocpp.charge.example.com`). |
| **A MapTiler API key** | Free account at [cloud.maptiler.com](https://cloud.maptiler.com/account/keys/) → *API keys*. The map's tiles come from there. |
| **An SSH key on your own computer** | `ls ~/.ssh/id_ed25519.pub`. If it does not exist: `ssh-keygen -t ed25519`. |

Both repositories are public on GitHub, so the server can download them without any GitHub keys:
`https://github.com/danilokujacic/chargers` (the backend) and
`https://github.com/danilokujacic/chargers-fe` (the website).

## 2. Create the server

In your provider's dashboard, create a server with:

- **Image:** Ubuntu 24.04 LTS.
- **Size:** as in step 1.
- **SSH key:** paste the contents of your `~/.ssh/id_ed25519.pub`, so you can log in without a
  password.
- **Firewall** (if the provider offers one): allow inbound TCP **22** (SSH), **80** and **443**.
  Port 80 is required even though the site uses HTTPS: Let's Encrypt checks it when issuing the
  certificate.

Write down the server's **public IPv4 address** (and IPv6, if it has one). Below it is `203.0.113.10`;
use yours.

## 3. Log in and create a user

From your own computer:

```bash
ssh root@203.0.113.10
```

Do not run the platform as `root`. Create a normal user with sudo rights (here `deploy`) and give it
your SSH key:

```bash
adduser deploy                                   # choose a password; the other questions can be left empty
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
exit
```

Log in again as that user. **Every step from here on runs as `deploy`**, not root:

```bash
ssh deploy@203.0.113.10
```

## 4. Prepare the server

**Updates:**

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo reboot            # only if the upgrade asks for it; then ssh back in
```

**Time zone and clock.** Chargers timestamp every session, and billing depends on the Central
System's clock. Ubuntu keeps it synchronised by default; check it:

```bash
timedatectl            # "System clock synchronized: yes" and "NTP service: active"
```

**Swap** (needed on servers with less than 4 GB of RAM, harmless on bigger ones): it lets the
website build finish instead of being killed for lack of memory.

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h                # the "Swap" line now shows 4.0Gi
```

**Firewall.** Allow SSH first, or you will lock yourself out:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable        # answer y
sudo ufw status        # OpenSSH, 80, 443 allowed
```

## 5. Point your domain at the server

In your domain's DNS settings (at your registrar or DNS provider), create two **A** records:

| Type | Name | Value |
|---|---|---|
| A | `charge` | `203.0.113.10` |
| A | `ocpp.charge` | `203.0.113.10` |

That gives `charge.example.com` (the website) and `ocpp.charge.example.com` (for chargers). If the
server has an IPv6 address, add matching **AAAA** records too. Any names work: the website one is
`DOMAIN` below, the charger one `OCPP_DOMAIN`.

DNS takes from a minute to an hour to spread. Check from the server:

```bash
dig +short charge.example.com          # must print 203.0.113.10
dig +short ocpp.charge.example.com     # must print 203.0.113.10
```

Carry on with the next steps meanwhile; only step 9 needs DNS to be working.

## 6. Get the code

```bash
sudo apt-get install -y git make
cd ~
git clone https://github.com/danilokujacic/chargers.git
git clone https://github.com/danilokujacic/chargers-fe.git charger-fe
cd chargers
```

The two repositories must sit side by side, as `~/chargers` and `~/charger-fe` (`make setup` would
clone the second one for you if you forgot). **Every `make` command runs from `~/chargers`.**

## 7. Install Docker

```bash
make install-docker
```

This installs Docker Engine and Docker Compose from Docker's own repository, plus `openssl`,
enables Docker at boot, and adds you to the `docker` group. **Log out and back in** so the group
takes effect:

```bash
exit
ssh deploy@203.0.113.10
cd ~/chargers
docker run --rm hello-world        # prints "Hello from Docker!" — no sudo needed
```

## 8. Configure

```bash
make setup
```

It checks Docker and git, then asks three questions:

| Question | Answer |
|---|---|
| Domain for the website and API | `charge.example.com` (no `https://`) |
| Domain chargers connect to | press Enter for the default, `ocpp.charge.example.com` |
| MapTiler API key | the key from step 1 |

It writes **`~/chargers/.env.production`**: your domains, your MapTiler key, and freshly generated
random passwords for MongoDB, Redis and the admin API. The file is readable only by you and is never
committed to git.

**Make a copy of `.env.production` somewhere safe** (a password manager). Without it the database
password is lost, and with it anyone can read your data. `make setup` never overwrites an existing
file; to start over, delete it and run `make setup` again.

The same thing without questions, for scripts:

```bash
make setup DOMAIN=charge.example.com MAPTILER_KEY=your-key
```

## 9. Deploy

Make sure `dig` from step 5 prints your IP, then:

```bash
make deploy
```

This takes 3–10 minutes the first time. It:

1. builds the images for the backend (Python) and the website (Next.js);
2. starts MongoDB and Redis, and waits until they are healthy;
3. runs the database migrations (`make migrate`);
4. loads the 134 PlugShare charging sites (`make seed`);
5. starts everything else — the Central System, the public API, the website and Caddy, which obtains
   the HTTPS certificates — and waits until every container is healthy;
6. runs `make status`.

The end of the output should look like:

```
chargers-api-1        ...   Up (healthy)
chargers-caddy-1      ...   Up
chargers-cs-1         ...   Up (healthy)
chargers-frontend-1   ...   Up (healthy)
chargers-mongo-1      ...   Up (healthy)
chargers-redis-1      ...   Up (healthy)

website  https://charge.example.com/  HTTP 200
api      https://charge.example.com/api/v1/sites  134 sites
```

## 10. Check that it works

1. **In a browser**, open `https://charge.example.com`: a full-screen map of Montenegro with faint
   grey pins (the PlugShare sites, status "unknown" until chargers are connected). The padlock shows
   a Let's Encrypt certificate. Click a pin to open its panel.
2. **The API:** `https://charge.example.com/api/v1/sites` returns JSON; `https://charge.example.com/docs`
   shows its documentation.
3. **The charger endpoint:** `curl -sI https://ocpp.charge.example.com/admin/` answers `404` (the
   admin API is deliberately not reachable from the internet).
4. **Survives a reboot:** `sudo reboot`, wait a minute, `ssh` back in, `make status` — everything is
   running again by itself.

To see it come alive without real chargers, run the demo fleet (Part 2, [The demo fleet](#the-demo-fleet)).

## 11. Set up backups

```bash
make backup            # writes backups/mongo-<time>.archive.gz and backups/app-data-<time>.tar.gz
```

Run it every night at 03:00 and keep two weeks — `crontab -e`, then add the line:

```
0 3 * * * cd /home/deploy/chargers && make backup >/dev/null 2>&1 && find backups -mtime +14 -delete
```

Backups on the same server do not survive losing the server: copy `backups/` elsewhere regularly
(for example `rsync -a deploy@203.0.113.10:chargers/backups/ ./chargers-backups/` from your own
computer, or your provider's snapshot feature).

**Done.** The rest of this page is reference.

---

# Part 2 — Running it

## What is running

```
                         ┌────────────── one server (Docker) ──────────────────┐
 browsers ── https ──▶ caddy ──▶ frontend (Next.js website)                    │
                       │    └─▶ api (public API, /api/*) ──┐                   │
 chargers ── wss ────▶ │ ocpp.<domain> ──▶ cs (main.py) ───┼──▶ mongo          │
                       │                                   └──▶ redis          │
                         └─────────────────────────────────────────────────────┘
```

| Service | What it is | Reachable from outside? |
|---|---|---|
| `caddy` | HTTPS for both domains; certificates from Let's Encrypt, renewed automatically | Yes: ports 80 and 443 |
| `frontend` | The public map | Through Caddy: `https://DOMAIN/` |
| `api` | The public, read-only API and live WebSockets | Through Caddy: `https://DOMAIN/api/…`, docs at `/docs` |
| `cs` | The Central System (`main.py`); chargers connect here | Through Caddy: `wss://OCPP_DOMAIN/<identity>`. Its own port 9000 only on `127.0.0.1` unless you change `CS_BIND` |
| `mongo` | The database, password-protected | No |
| `redis` | Live events from `cs` to `api`, password-protected | No |
| `fleet` | Demo only: 195 simulated chargers (`make demo-up`) | No |
| `tools` | One-off commands: migrations, seeds, operator CLIs | No |

Data lives in Docker volumes — `mongo-data` (the database), `app-data` (charger keys, the demo's
files), `caddy-data` (certificates) — so it survives rebuilds and restarts. Every container restarts
by itself after a crash or reboot. Logs are rotated (5 × 20 MB per service).

## Everyday commands

All from `~/chargers`. `make help` lists them all.

| Command | What it does |
|---|---|
| `make status` | Container health, plus a check of the website and API |
| `make logs SERVICE=cs` | Follow one service's logs (`cs`, `api`, `frontend`, `caddy`, `mongo`, `redis`, `fleet`); without `SERVICE`, all of them. Ctrl+C to stop following |
| `make up` | Start everything, or apply a changed setting |
| `make down` | Stop everything (data is kept) |
| `make restart SERVICE=api` | Restart one service (or all, without `SERVICE`) |
| `make update` | Pull new code, rebuild, migrate, restart (below) |
| `make migrate` / `make migrate-status` | Apply / list database migrations |
| `make seed` | Load or refresh the PlugShare reference sites (safe to re-run) |
| `make backup` / `make restore FILE=… CONFIRM=yes` | Backups (below) |
| `make chargers` | List registered chargers |
| `make register-charger ID=CP042` | Register a real charger; prints its key **once** |
| `make rotate-key ID=CP042` | Issue a new key for a charger |
| `make operate ARGS="remote-start CP042 TAG001"` | Any `operate.py` command (`make operate ARGS=--help` lists them) |
| `make shell` | A shell in the backend image, with the database and admin API reachable |
| `make mongo-shell` | A MongoDB shell on the application database |
| `make test` | Run the test suite inside the backend image (uses a throwaway database) |
| `make destroy CONFIRM=yes` | Delete the containers **and all data**. There is no undo |

## Updating

```bash
cd ~/chargers
make backup            # always, before an update
make update
```

`make update` runs `git pull` in both repositories, rebuilds the images, runs any new migrations and
restarts what changed. The site is briefly unavailable (seconds) while containers are replaced;
chargers reconnect by themselves.

**Changing settings.** Edit `.env.production`, then `make up`. If you changed `DOMAIN`, `PUBLIC_URL`,
`PUBLIC_WS_URL` or `MAPTILER_KEY`, the website must be rebuilt, because it bakes them in: `make build up`.

## Migrations and seed data

- **Migrations** (`migrate.py`, `make migrate`) run in order, each exactly once, and are recorded in
  the `schema_migrations` collection. `make deploy` and `make update` run them every time, so there is
  nothing to remember. MongoDB has no schema to alter; a migration here creates indexes or fixes up
  existing data. To add one, append it to `MIGRATIONS` in `migrate.py` (instructions at the top of
  that file).
- **Seed** (`make seed`) loads the 134 PlugShare sites from `montenegro_only.json` as reference sites
  (faint "unknown" pins). Re-running updates them and never duplicates.
- **Chargers are not seeded** in production: each real one is registered (next section).

## Connecting a real charger

1. **Register it:** `make register-charger ID=<identity>`. The identity is the name the charger uses
   in its connection URL (often its serial number). The command prints a 40-character **key, once** —
   copy it.
2. **Configure the charger** (in its own web or installer interface):
   - Central System URL: `wss://ocpp.charge.example.com/` (the charger appends its identity)
   - Protocol: OCPP 1.6J
   - Authentication: HTTP Basic, username = its identity, password = the key
3. **It connects and boots as Pending.** The Central System then sends it a fresh key over OCPP and
   accepts it. `make logs SERVICE=cs` shows the exchange; `make chargers` then lists it as Accepted.
4. **Put it on the map:**
   ```bash
   make operate ARGS='create-site "Hotel Example" hotel 42.43 19.26'        # prints the site id
   make operate ARGS="set-charge-point-location <identity> --site-id <site id>"
   ```
   Site types: `gas_station`, `hotel`, `parking`, `other`.

**Chargers without TLS.** If a charger can only use `ws://`, set `CS_BIND=0.0.0.0` in
`.env.production`, run `make up`, and open the port: `sudo ufw allow 9000/tcp`. It then connects to
`ws://203.0.113.10:9000/`. Keys travel unencrypted that way, and the admin API (token-protected)
shares that port, so prefer TLS whenever the charger supports it.

**Firmware updates:** put the files in `~/chargers/firmware_files/`. Chargers download them from
`https://ocpp.charge.example.com/firmware/<file>` when you send `make operate ARGS="update-firmware …"`.

## The demo fleet

Simulated chargers on the 134 PlugShare sites, so the map comes alive for a demonstration
(`instructions/11-demo-fleet.md`). Every demo site is labelled "Demo data — simulated chargers."

```bash
make seed-demo      # once: 195 simulated chargers and 256 demo cards
make demo-up        # start them; all connected within about 30 s; pins turn green, amber and red
make demo-down      # stop cleanly: sessions end, chargers switch off (map: 124 grey, 10 red)
```

Try the operator side with `make operate ARGS="remote-start PS-2946795 DEMO-REMOTE"`. Removing the
demo data is specified (task 12) but not built yet — so on a server meant for real use, do not run
`make seed-demo`.

## Backups and restore

`make backup` writes two files into `~/chargers/backups/`: a compressed MongoDB dump, and an archive of
the `app-data` volume (charger keys, the demo's manifest and state). See step 11 for the nightly job.

To restore:

```bash
make restore FILE=backups/mongo-20260930-030000.archive.gz CONFIRM=yes
```

It stops the demo fleet, replaces the database, and restores the `app-data` archive from the same
backup if it is next to it (so the demo's memory of open sessions matches the database). Start the
fleet again afterwards if you use it (`make demo-up`).

**Moving to a new server:** do Part 1 on the new server up to step 8, but copy `.env.production`
over instead of running `make setup`, run `make deploy`, then copy the backup files over and
`make restore`. Then point DNS at the new server.

## Security

- **Secrets** exist only in `.env.production` (mode 600, never committed). MongoDB and Redis are not
  published on any port, and logs show the database address with its password masked.
- **The admin API**, which can command chargers, listens only on `127.0.0.1` and is blocked on the
  public charger domain; use it through `make operate`.
- **Open ports:** 22, 80 and 443 (plus 9000 only if you enabled it). Docker publishes its ports past
  `ufw`, so the ports in `docker-compose.yml` are what is really open: 80, 443, and 9000 on localhost.
- **SSH:** once your key login works, you can turn off password logins: set
  `PasswordAuthentication no` in `/etc/ssh/sshd_config`, then `sudo systemctl restart ssh`.
- **MapTiler key:** it is visible in every visitor's browser by design. In the MapTiler dashboard,
  restrict it to your domain.
- **Before going public**, read `PLATFORM_GUIDE.md` §4: the live WebSocket currently forwards driver
  card numbers, and the public API has no rate limiting.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No .env.production yet` | Run `make setup` |
| `permission denied ... docker.sock` | You have not logged out and back in since `make install-docker` |
| `make status`: website not reachable, or the browser shows a certificate error | DNS does not point here yet (step 5), or port 80/443 is closed (steps 2 and 4). `make logs SERVICE=caddy` shows Let's Encrypt's answer. Caddy retries by itself once it is fixed |
| The map page shows an error, or no pins | `make logs SERVICE=frontend` and `make logs SERVICE=api`. The website fetches the API through Caddy when it renders |
| Changed `DOMAIN` or `MAPTILER_KEY`, the site did not change | The website bakes them in at build time: `make build up` |
| The build stops with `Killed` | Out of memory: add swap (step 4) |
| A charger gets HTTP 401 | Wrong identity or key. `make rotate-key ID=…` and install the new key on the charger |
| A container keeps restarting | `make logs SERVICE=<name>`; `make status` shows its health |
| Disk filling up | `docker system df`; old images: `docker image prune`; old backups: `find backups -mtime +14 -delete` |

## Files

| File | Role |
|---|---|
| `docker-compose.yml` | The whole stack |
| `Makefile` | Every command on this page |
| `deploy/backend.Dockerfile` | The Python image (cs, api, fleet, tools) |
| `deploy/frontend.Dockerfile` (+ `.dockerignore`) | The website image, built from `../charger-fe` |
| `deploy/Caddyfile` | HTTPS and routing |
| `deploy/scripts/setup.sh`, `install-docker.sh` | `make setup`, `make install-docker` |
| `migrate.py` | Database migrations |
| `.env.production` | Your settings and secrets (created by `make setup`, never committed) |
