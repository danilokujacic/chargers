# Demo: user stories and demo paths

What can be shown with the demo fleet (`instructions/11-demo-fleet.md`), as user stories, and every
path through each one that the running system actually supports today. Each path says whether a
recorded video shows it (`demo/videos/`, made by `demo/record_demo.mjs`) or how to show it live.

**Say this at the start of every demo:** the sites are real places from PlugShare, but every
status on the map is **simulated**. This system has never talked to those chargers. The station
panel says "Demo data — simulated chargers." `instructions/12-remove-demo-fleet.md` removes it all.

## The words you will need

A **site** is a place (a hotel car park, a fuel station). It has one or more **chargers** (the
machines), and each charger has one or more **connectors** (the plugs). Each connector reports a
status of its own:

| Status | What it means for a driver |
|---|---|
| Available | Free: nothing plugged in |
| Preparing | A driver has started: plugged in, card being checked |
| Charging | A car is drawing power |
| Finishing | Charging is over, the cable is still plugged in |
| Reserved | Held for one particular driver |
| Unavailable | Switched off or out of service |
| Faulted | Broken |

Plugs you will see: **CCS2** (fast DC, most European cars), **CHAdeMO** (fast DC, older Japanese
cars), **Type 2** (the everyday AC plug in Europe). DC is fast because it bypasses the car's own
on-board charger.

**Pin colours** (one per site): red if any connector is broken, else green if any connector is
free, else violet if one is reserved, else amber if they are all in use, else grey (switched off).
So a site is amber only when *every* connector is busy. A cluster (a circle with a number) is red if
any site inside is broken, green if any has a free connector, grey otherwise.

## People in the stories

- **Driver**: anyone looking at the public map.
- **Operator**: runs the chargers, using `operate.py` against the Central System (`main.py`).
- **Presenter**: runs the demo itself (`run_demo_fleet.py`).

## Videos

| # | File | Stories | Length |
|---|---|---|---|
| 1 | `01-map-at-a-glance.mp4` | D1, D2 | 1:50 |
| 2 | `02-plugs-and-power.mp4` | D3 | 3:18 |
| 3 | `03-remote-session-live.mp4` | O1, D4 | 2:12 |
| 4 | `04-busy-and-broken-sites.mp4` | D5, D6 | 2:06 |
| 5 | `05-operator-controls.mp4` | O2, O3, O4 | 2:09 |
| 6 | `06-restarts-and-honest-stop.mp4` | P2, P1 | 2:19 |

Every video opens with a title card (the story and what you are about to see), numbers each step
in a caption, shows the mouse pointer and a box around what matters, and ends with a "what we saw"
card. Operator steps are typed into an on-screen terminal: they are the real `operate.py` commands,
run at that moment, with their real output.

---

## D1 — Where can I charge right now? *(driver)*

> As a driver, I want to see every charging site in Montenegro with its live status, so that I
> know where I can plug in before I set off.

| Path | What happens | Shown in |
|---|---|---|
| D1.1 | Open the map: the whole country, 136 sites, grouped into clusters | Video 1 |
| D1.2 | Read the colours (green, amber, violet, red, grey) | Video 1 |
| D1.3 | Click a cluster: the map zooms to it; click again until single pins show | Video 1 |
| D1.4 | Pins recolour on their own, every 10 s, with no page reload | Video 1 (said), Videos 3, 5, 6 (seen) |
| D1.5 | A hidden browser tab stops refreshing and resumes when shown | Live only; nothing visible to record |

## D2 — Find chargers near a place *(driver)*

> As a driver, I want to search for a town and see the chargers there.

| Path | What happens | Shown in |
|---|---|---|
| D2.1 | Type "Budva", press Go: the map moves to Budva | Video 1 |
| D2.2 | Type a place that does not exist: "Nothing found.", the map stays put | Video 1 |

## D3 — Will my car fit, and how fast will it charge? *(driver)*

> As a driver, I want to see each station's plugs and power before I drive there, so that I don't
> arrive at a plug my car can't use.

| Path | What happens | Shown in |
|---|---|---|
| D3.1 | Rest stop Pelev Brijeg: four chargers, each `CCS2 · 200 kW DC`; header "Up to 200 kW" | Video 2 |
| D3.2 | EKO Tivat: one charger with three plugs, each rated (`CHAdeMO · 50 kW DC`, `CCS2 · 50 kW DC`, `Type 2 · 22 kW AC`) | Video 2 |
| D3.3 | Merit Starlit Hotel, Budva: 15.36 kW is shown as `15.4 kW` | Video 2 |
| D3.4 | kolasin 1600: no power published, so `Type 2 · AC` and no "Up to" line: never guessed | Video 2 |
| D3.5 | Every simulated site ends with "Demo data — simulated chargers." | Video 2 |
| D3.6 | A real, non-demo charger: "Petrol Podgorica — Bulevar" (CP001) shows `Connector 1`, no plug data, no demo note | Live only (see "Things to know") |

## D4 — Watch a station change live *(driver)*

> As a driver, I want the station panel to update by itself, so that what I see is what is
> happening now.

| Path | What happens | Shown in |
|---|---|---|
| D4.1 | The panel says "Live"; statuses change in place as the charger reports them | Videos 3, 4, 5 |
| D4.2 | During a session: "Session #N · X Wh", energy counting up every 15 s | Video 3 |
| D4.3 | The site's pin follows within 10 s | Videos 3, 5 |

## D5 — At a big site, which charger is free? *(driver)*

> As a driver arriving at a site with several chargers, I want each charger's own status.

| Path | What happens | Shown in |
|---|---|---|
| D5.1 | GreenCar.me: five chargers ("Charger 1" to "Charger 5"), each with its own live status | Video 4 |
| D5.2 | One charger starts a session; only its row changes | Video 4 |
| D5.3 | The site stays green while any connector is free | Video 4 (said) |

## D6 — Is it broken? *(driver)*

> As a driver, I want to know a station is broken before I drive there, not after.

| Path | What happens | Shown in |
|---|---|---|
| D6.1 | Vranjina: red pin; both connectors `Faulted`, error `OtherError` (PlugShare users reported it out of order) | Video 4 |
| D6.2 | A red cluster means a broken site is somewhere inside | Video 1 (said) |

The 10 red sites are the PlugShare locations whose every outlet was reported out of order. They
stay red for the whole demo, and even after it stops.

## O1 — Start and stop a session remotely *(operator)*

> As an operator, I want to start a session for a driver whose card or app fails, and stop it
> again, from my desk.

| Path | What happens | Shown in |
|---|---|---|
| O1.1 | `operate.py remote-start PS-2946795 DEMO-REMOTE` → Accepted → panel goes Preparing → Charging | Video 3 |
| O1.2 | Starting a second session on the same connector is refused ("Rejected") | Video 3 |
| O1.3 | `operate.py remote-stop PS-2946795 <session>` → Finishing → Available within 15 s | Video 3 |
| O1.4 | With no connector given, a multi-plug charger picks its lowest free connector | Live: `remote-start` on EKO Tivat's `PS-…` |
| O1.5 | An unknown driver tag: the charger accepts the request, checks the tag, and starts nothing | Live: `remote-start PS-2946795 NOBODY` |

## O2 — Take a connector out of service *(operator)*

> As an operator, I want to switch a connector off for maintenance, and back on.

| Path | What happens | Shown in |
|---|---|---|
| O2.1 | `change-availability <id> 1 Inoperative` → Unavailable; the pin turns grey | Video 5 |
| O2.2 | `change-availability <id> 1 Operative` → Available | Video 5 |
| O2.3 | During a session the answer is "Scheduled": it switches off when the session ends | Live only |
| O2.4 | It stays off after the fleet restarts (the Central System re-sends it) | Live only |

## O3 — Hold a connector for a driver *(operator)*

> As an operator, I want to reserve a connector for one driver.

| Path | What happens | Shown in |
|---|---|---|
| O3.1 | `reserve-now <id> 1 DEMO-REMOTE <expiry>` → Reserved; the pin turns violet | Video 5 |
| O3.2 | `cancel-reservation <id> <reservation id>` → Available | Video 5 |
| O3.3 | Reserving a connector that is charging is refused ("Occupied") | Live only |

## O4 — Reboot a charger *(operator)*

> As an operator, I want to restart a misbehaving charger without anyone driving there.

| Path | What happens | Shown in |
|---|---|---|
| O4.1 | `reset <id> Soft` mid-session: the session ends, the charger restarts, reconnects on its own and reports Available | Video 5 |
| O4.2 | `reset <id> Hard`: the session is cut off; after it reconnects it is closed as "PowerLoss" | Live only |

## P1 — End the demo honestly, and bring it back *(presenter)*

> When the demo stops, the map must stop claiming live knowledge it no longer has.

| Path | What happens | Shown in |
|---|---|---|
| P1.1 | Ctrl+C in the fleet terminal: every session ends, every working charger reports Unavailable; within 10 s the map shows 124 grey and 10 red sites | Video 6 |
| P1.2 | Start the fleet again: all 195 chargers are back in about 20 s and the colours return | Video 6 |
| P1.3 | Ctrl+C twice, or `kill -9`: it exits at once; the next start closes the cut-off sessions as PowerLoss | Live only |

## P2 — Survive a Central System restart *(operator)*

> As an operator, I want chargers to come back by themselves after the server restarts.

| Path | What happens | Shown in |
|---|---|---|
| P2.1 | Restart `main.py` while the fleet runs: all 195 chargers reconnect in about 25 s, with no one intervening; sessions it cut off are closed as PowerLoss | Video 6 |

---

## What the demo cannot show (yet)

- **Removing the demo and going back to the faint grey "unknown" reference pins.** That is file 12
  (`remove_demo_fleet.py`), which is not built yet.
- **A working charger breaking down live.** OCPP gives the operator no message that causes a
  fault; the only faults in the demo are the ten PlugShare "out of order" sites.
- **Anything with no visible effect on the map:** unlocking a connector, triggering a message,
  reading or changing configuration, local authorization lists, firmware updates, diagnostics.
  They all work through `operate.py`, but there is nothing to watch.
- **Smart charging** (a power limit of 0 should pause a session as `SuspendedEVSE`, amber): the
  code path exists but has not been tried against the fleet, so do not demo it unrehearsed.
- **Drivers, payments, accounts:** not part of this system.

## Things to know before presenting

- One amber pin in Podgorica, "Petrol Podgorica — Bulevar", is CP001: a real test charger, not
  part of the demo, left in `Finishing` by earlier testing. Hotel Budva is grey because it has no
  charger at all. Both are real operator sites.
- Sessions start on their own at random (3–8 minutes long, 5–15 minutes apart per connector), so
  the exact colours differ every time. The operator actions (remote start, maintenance, reserve,
  reset) are what make a change happen on cue.
- `DEMO-REMOTE` is one driver tag: it can run one session at a time. Stop one before starting the
  next.

## Running it live

From `chargers/`, with MongoDB and Redis up (commands as in the README's "Demo fleet" section):

```
python main.py                                            # terminal 1
uvicorn api.app:app --host 0.0.0.0 --port 8000            # terminal 2
python run_demo_fleet.py                                  # terminal 3; Ctrl+C to stop
cd ../charger-fe && npm run dev                           # terminal 4, then open :3000
export ADMIN_TOKEN=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)   # terminal 5, for operate.py
```

Handy identities: `PS-2946795` (kolasin 1600, one Type 2 connector), the four `PS-…` of Rest stop
Pelev Brijeg, the five of GreenCar.me (`GET /api/v1/sites/{id}` lists them).

## Re-recording the videos

```
node demo/record_demo.mjs          # all six, into demo/videos/
node demo/record_demo.mjs 3 5      # just videos 3 and 5
DEMO_PACE=1.5 node demo/record_demo.mjs   # 50% longer pauses
```

It needs `main.py`, the API and `npm run dev` running, and `run_demo_fleet.py` **not** running:
the recorder starts its own fleet so it can show the fleet's console, and stops it at the end.
Video 6 restarts `main.py`. ffmpeg with libx264 must be on the PATH, or set `DEMO_FFMPEG`.

The videos are the real app in headless Chrome at 1920×1080, with the page's text at 133% (as
browser zoom would make it) so it reads on a projector. Captions, the pointer, highlight boxes,
the status counter and the terminal are drawn on top by the recorder; nothing in the app itself is
changed. A take that goes wrong (for example, the fleet happens to start a session on the chosen
connector a moment before the operator does) is thrown away and recorded again.
