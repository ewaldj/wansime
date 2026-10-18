# wansime — WAN Simulation made easy

Single-file Python tool (standard library only) that simulates WAN links on Linux / Raspberry Pi with `tc netem`: delay, jitter, loss, reorder, duplicate, corrupt, rate limit and MTU per direction. It runs as a transparent L2 bridge between two Ethernet ports (or on a single interface) and is controlled from a full-screen terminal UI (TUI) and a web GUI at the same time, both with live traffic graphs.

By Ewald Jeitler - https://www.jeitler.cc

Current version: v1.60

## Features

- Bridge mode: transparent L2 bridge between two ports, impairments per direction (A->B and B->A)
- Single mode: one interface, OUT = egress, IN = ingress redirected to an IFB device
- Delay, jitter, loss, consecutive (burst) loss, reorder, duplicate, corrupt, rate limit, MTU, all per direction
- Presets (Metro, Transatlantic, DSL, LTE, Satellite, Congested, ...)
- START / STOP without tearing down the setup; restore the original network state at any time
- Changes are applied live (debounced, 0.6 s)
- Bridge passes VLAN, STP, CDP, VTP, LLDP and 802.1X EAPOL frames
- Live traffic graphs (IN / OUT per direction), CPU load, interface table in TUI and web GUI
- USB network adapter watchdog and recovery (RTL8153 / `r8152`)
- Runs headless as a systemd service
- Python 3.8+, Linux (tested on Raspberry Pi 5), root required

## Installation / Update

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/ewaldj/wansime/main/e-install.sh)"
```

Installs `wansime.py` to `/usr/local/bin` as command `wansime`; running it again updates. Manual: download `wansime.py` from https://github.com/ewaldj/wansime, `chmod +x wansime.py`.

### Requirements

```bash
sudo wansime.py --install            # apt-get/dnf/yum: iproute2, ethtool, kernel modules sch_netem, bridge, ifb
```

At every start the script checks the requirements; if something is missing it lists it and offers to install it (`Install now? [Y/n]`). Without a terminal (service) it only reports the missing parts and exits.

Root is required. Without root the script restarts itself through `sudo` and asks for the sudo password (interactive terminal only, otherwise it prints a hint and exits).

## Screenshots

Web GUI (port 8082):

![wansime web GUI](screenshots/wansime-webgui.png)

TUI (180 x 46 terminal, the layout adapts to the terminal size):

![wansime TUI](screenshots/wansime-tui.png)

The screenshots show example values and simulated traffic (A->B about 400 Mbit/s, B->A about 25 Mbit/s, 40 ms delay, 5 ms jitter, 0.1 % loss).

## Quick Start

```bash
sudo wansime                         # TUI + web GUI on http://<ip>:8082/
sudo wansime --install-service       # systemd service wansime.service (headless, web GUI on port 8082)
sudo wansime --restore               # restore the original network state
```

Workflow: choose mode and ports (TUI `b` / web setup row) -> START -> change WAN values (applied live) -> STOP / START as needed -> restore original state when finished.

## Modes

| Mode | What it does |
|------|--------------|
| **bridge** | Transparent L2 bridge `br0` between two Ethernet ports (A and B). Impairments are set per direction: A->B (egress on port B) and B->A (egress on port A). |
| **single** | One interface, no bridge. **OUT** impairs locally generated traffic (egress). **IN** impairs locally received traffic (ingress redirected to an IFB device). |

Directions are labelled with the interface names (e.g. `eth0 -> eth1`) in the GUI and the TUI.

## Parameters (per direction)

| Parameter | Range | Note |
|-----------|-------|------|
| Delay | 0-1000 ms | |
| Jitter | 0-500 ms | normal distribution; needs a base delay |
| Loss | 0-20 % | random loss |
| Consecutive packet loss | 1-50 | average burst length in packets, 1 = independent loss (Gilbert-Elliott model, overall loss rate stays the configured Loss) |
| Reorder | % | needs a delay |
| Duplicate | % | |
| Corrupt | % | |
| Rate limit | kbit/s, 0 = off | TUI accepts `512k`, `20m`, `1g` |
| MTU | bytes, default = port MTU | |

- Values beyond the slider range can be typed (web GUI number field, TUI edit).
- Jitter: tc refuses `delay 0ms 10ms distribution normal`; with jitter > 0 and delay 0 the delay is set to the jitter value and an info message says so. Packets sent closer together than the jitter overtake each other, so out-of-order packets appear even with Reorder = 0; use jitter 0 for strictly ordered delivery.
- Reorder: if Reorder > 0 and the delay is below 10 ms, the delay is raised to 10 ms.
- The queue limit is sized automatically from delay and rate.
- MTU, bridge mode: both ports get the largest configured MTU (never below the original MTU). A direction with a smaller MTU is limited by a tc filter on its egress port that drops frames larger than MTU + 14 byte Ethernet header, without ICMP (path MTU discovery sees a black hole). Use together with "disable NIC offloads". USB adapters are taken down for an MTU change and checked afterwards; if the adapter stops responding, the USB recovery re-initializes it and applies the MTU again.
- MTU, single mode: the MTU of the interface is set (OUT value only).
- The MTU field shows the port's original MTU as default; Reset returns to it.
- Presets (keep the MTU setting): Clean link, Metro WAN (5 ms), Cross-country (50 ms RTT), Transatlantic (90 ms RTT), Intercontinental (300 ms RTT), DSL 16/1, LTE, Satellite GEO (600 ms RTT), Congested WAN, Lossy 5 %.
- Mirror: edits of one direction are copied to the other one (TUI `l`, web checkbox MIRROR).

## Start / Stop

- START sets up the bridge (or IFB) and applies the WAN values.
- STOP keeps the setup in place but drops all traffic (`netem loss 100 %`); the state shows `STOPPED - NO TRAFFIC`. Value changes while stopped are stored and take effect on START (the web GUI does not apply them while stopped). The red "traffic in, nothing out" warning in the LIVE table is suppressed while stopped.
- Restore original state removes netem, the bridge or IFB and brings the ports back (see Persistence and restore). To change ports or mode: restore, change, START.

## Applying values

- Changes of WAN values (slider, number field, `-` / `+` buttons, cursor keys, `+`/`-`) are shown at once and applied to the system 0.6 s after the last change, so a fast sequence of changes results in a single tc update. The TUI status line shows "applying in a moment".
- While a setup is active, every edit is applied; FORWARD LLDP / 802.1X and DISABLE NIC OFFLOADS take effect immediately as well. Unchecking the offloads sets the NIC features back to the values saved before the setup.

## TUI

| Key | Action |
|-----|--------|
| Arrows | select parameter / direction |
| Enter or `e` | edit value |
| `+` `-` / `]` `[` | step / step x10 |
| `Space` | select the current value: Up/Down jump between values, Left/Right change it (Left = -, Right = +, PgUp/PgDn = x10), Tab switches the column; Space, Enter or Esc leaves the selection |
| `l` | mirror edits to the other direction |
| `p` | load preset |
| `b` | setup: choose mode and interfaces, set up (questions are answered with Enter = yes) |
| `a` | START / STOP |
| `r` | restore original state |
| `c` | reset all impairments and MTU |
| `s` | save config |
| `f` | fix: re-initialize the USB adapter(s) of the active setup |
| `q` / Ctrl-C | quit; with an active setup it asks "Reset system (restore original state)?": `y` or Enter = restore and quit, `n` = quit and leave everything as it is |

Upper-case letters work like lower-case. At exit the script prints `THX for using wansime.py <version> - www.jeitler.cc`.

Layout: the tables, slider bars and the key bar adapt to the terminal size (minimum 80 columns): the key bar picks the longest label set that fits and spreads it over the whole line. Rows from top to bottom: parameter table, LIVE, INTERFACES (if rows are left), two traffic graphs (needs at least 9 free rows), key bar, status line. The frame is drawn down to the key bar. The status line shows mode, state (ACTIVE / STOPPED / NOT ACTIVE), config (SAVED / MODIFIED), mirror, and messages ("working ...", "applied", errors; errors stay longer).

Colors: green = active/up/changed, red = down/error, white = normal. A red OUT value means traffic comes in but nothing goes out.

## Web GUI

Port 8082 (`--port`). Header with clock and Nelly (animated SVG, links to https://jeitler.cc/nelly/). Buttons: RESCAN INTERFACES, START / STOP, RESTORE ORIGINAL STATE, SAVE CONFIG, RECOVER USB ADAPTER, preset select (LOAD PRESET), MIRROR, RESET. Checkboxes: FORWARD LLDP / 802.1X, DISABLE NIC OFFLOADS. Mode and port selects with bridge name field.

Sections: WAN PARAMETERS (slider + number field + `-` / `+` per parameter and direction, sliders scale with the window width), LIVE (IN / OUT rate, netem packets / drops / backlog), TRAFFIC, INTERFACES (state, speed, MTU, member of, addresses, MAC).

- No login by default. Use `--auth USER:PASS` (or env `WANSIM_AUTH`) and/or `--bind 127.0.0.1`.

HTTP API (JSON): `GET /api/state`, `GET /api/hist?since=<ts>`; `POST` (`Content-Type: application/json`): `/api/cfg`, `/api/apply`, `/api/stop`, `/api/clear`, `/api/preset`, `/api/recover`, `/api/restore`, `/api/save`, `/api/histreset`.

## Traffic graphs

The script samples the port counters once a second (30 minutes of history, kept in the process, so a reloaded web page keeps its history). Per direction IN (what arrives) and OUT (what passes the simulation) are shown; the difference is what loss, rate limit and drops removed.

- Web GUI: two time graphs (A->B, B->A), range 1 / 5 / 15 / 30 min, current values, peak and totals. The RESET button next to the range selector clears the history and the totals on the server (other open browser windows keep their graph until reloaded).
- CPU: behind the RESET button the CPU load of the host is shown once a second: whole system, busiest core and the wansime process (percent of one core). Values above 80 % turn red. Forwarding often saturates a single core (soft IRQ) while the total stays low, hence the busiest-core value.
- TUI: two btop-like braille graphs (white = IN, green = OUT) with an info box (IN, OUT, TOP, totals). They grow with the terminal height and are left out on small terminals. A UTF-8 locale is needed for the braille characters (otherwise `#` / `.` is used).

## Transparency of the bridge

The bridge runs with STP off, IGMP/MLD snooping off, has no IP address and IPv6 is disabled on it, so it sends nothing of its own. What passes between the two ports (and gets the same delay / loss as normal traffic):

| Protocol | Passes? |
|---|---|
| VLAN tags (802.1Q, QinQ) | yes |
| STP / RSTP / MSTP BPDUs (01-80-C2-00-00-00) | yes, flooded because STP is off |
| CDP, VTP, DTP, UDLD, PAgP, PVST+/Rapid-PVST+ (01-00-0C-CC-CC-xx) | yes, ordinary multicast, flooded |
| LLDP (01-80-C2-00-00-0E), 802.1X EAPOL (01-80-C2-00-00-03), GARP/other link-local frames | yes with FORWARD LLDP / 802.1X checked (default; `group_fwd_mask 0xfff8`) |
| 802.3x pause (01-80-C2-00-00-01) | no - handled by each NIC itself |
| LACP / slow protocols (01-80-C2-00-00-02) | no - a Linux bridge cannot forward these (kernel restriction) |

Offloads (gro/gso/tso/lro) are switched off on the bridge ports by default so that packet sizes and timing stay realistic (setup checkbox / question, config key `offloads_off`).

## Persistence and restore

- Config: `/etc/wansim/config.json` (TUI `s` / web SAVE CONFIG).
- Original state: before changing anything the script stores a snapshot in `/var/lib/wansim/original.json`. Restore removes netem, the bridge or IFB, and brings the ports back (IP addresses, routes, MTU, offloads, NetworkManager managed state). Command line: `sudo wansime --restore`.
- If the bridge cannot be removed, the snapshot is kept and restore can be run again. A reboot clears everything that is not saved as a service.
- `--apply` applies the saved config once and exits.

## USB adapter recovery

While a setup is active, USB network adapters (e.g. RTL8153 / `r8152`) are checked every few seconds. If the driver no longer reaches the adapter (`ethtool` reports "No such device") for about 6 s, the script re-authorizes the USB device (like unplug/replug) and, if needed, reloads the driver module. Then it puts the port back into the bridge (or re-attaches the IFB redirect) and re-applies the WAN parameters. At most 3 automatic attempts per 10 minutes. Manual: web button RECOVER USB ADAPTER, TUI key `f`. Ports that are not USB adapters are not touched.

## Single instance

At start the script looks for other running copies (also under the former name `wansim.py`), lists them (PID, terminal, state) and asks: `k` kills the old process(es) and starts, `a` aborts (default). Without a terminal it exits with an error; `--kill-old` stops them without asking. An active systemd service `wansime.service` is stopped with `systemctl`. Killing a process does not change the network state; use `--restore` for that.

## Command line

```
wansime.py [--daemon] [--no-web] [--port 8082] [--bind 0.0.0.0] [--auth USER:PASS]
          [--install] [--install-service] [--remove-service]
          [--apply] [--restore] [--status] [--all-ifaces] [--kill-old] [--version]
```

| Option | Description |
|---|---|
| `--daemon` | Headless, web GUI only (default without a terminal) |
| `--no-web` | TUI without web GUI |
| `--port`, `--bind` | Web port (default 8082), bind address (default 0.0.0.0) |
| `--auth USER:PASS` | HTTP basic auth for the web GUI (or env `WANSIM_AUTH`) |
| `--install` | Install required packages / kernel modules |
| `--install-service`, `--remove-service` | Install and start / stop and remove the systemd service `wansime.service` |
| `--apply` | Apply the saved config once and exit |
| `--restore` | Restore the original state and exit |
| `--status` | Show the current state and exit |
| `--all-ifaces` | Also list virtual Ethernet interfaces |
| `--kill-old` | Stop other running instances without asking |
| `--version` | Print the version |

## Notes

- Bridge mode flushes the IP configuration of both ports; NetworkManager ports are set to unmanaged meanwhile. Use a third interface (e.g. WLAN) for management. If a port carries your session or the default route, the TUI / web GUI asks for confirmation first.
- Single mode impairs all traffic of the interface, including your own management session on it.
- Every system command has a timeout (8-15 s). If a NIC hangs (e.g. a USB adapter that resets), operations report an error instead of blocking, and the TUI and web GUI stay responsive and show `busy`.

## Project structure

```
├── wansime.py            # simulator: TUI + web GUI + bridge / netem control
├── e-install.sh          # installer / updater
├── screenshots/          # images for this README
├── LICENSE
└── README.md
```
