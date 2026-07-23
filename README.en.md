# 🛡️ Tether Isolator

🇬🇧 English (this page) | 🇹🇷 [Türkçe versiyon](README.md)

A tool that runs specific applications in a network namespace that is
**structurally isolated from the host's wired connection**, routing them
solely through an uplink you choose (phone USB tether / WiFi). It comes
with an Apple-inspired web panel, automatic reconnection, and an optional
supervised "relay" channel.

> This project is a professional, modular, and testable Python rewrite of
> the `tether_isolator.sh` / `tether_isolator_v2.sh` bash scripts. The
> original scripts are preserved under `legacy/`, and the core `ip netns`
> logic has been carried over as-is.

---

## Why?

There are situations where you want an application (e.g. a browser
session, an automation tool) to **absolutely never** use the PC's wired
connection, and instead go out entirely through a separate internet path
(a phone line).

There are two approaches:

| | Isolation | Leak risk | Impact on host |
|---|---|---|---|
| **veth + NAT** | Policy-based (firewall/routes) | Present (a wrong rule leaks) | Low |
| **Physical move (this project)** | **Structural (kernel-level)** | **None** | Interface leaves the host |

This project uses **physical relocation** (Model B): the uplink interface
is moved entirely into the namespace; there is no shared kernel stack left
with the host's `eth0`. The result: isolation that is not rule-dependent
but **guaranteed**.
Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Key features

- 🔒 **Structural isolation** — the uplink is physically inside the namespace.
- ♻️ **Automatic reconnection** — when the phone is unplugged and replugged,
  it reclaims the interface and renews DHCP **without killing** the
  applications (watchdog).
- 🌉 **Supervised relay** — one-click access to host/LAN (or full internet)
  when needed; fully isolated when off. Replaces the old file-queue hack.
- 💾 **Persistent profiles** — browser sessions are not wiped; multiple
  saved configurations like "work"/"personal".
- 🖥️ **Apple-inspired web panel** — live status, external IP, event stream.
- 🧩 **USB tether + WiFi** uplink support.
- ⌨️ **Scriptable CLI** + interactive mode + systemd service.
- 🧪 **`--dry-run`** — test all logic without root/hardware.

---

## Double-click to launch (desktop app)

```bash
./install.sh        # root NOT required; adds a menu + desktop shortcut
```

Then open **"Tether Isolator"** from the application menu or by
double-clicking the desktop shortcut. On first launch, a polkit (pkexec)
password prompt appears once; after that the panel opens in your browser.
To quit, use the **Exit** button in the panel.

## Quick start (terminal)

```bash
# Start the web panel (root required — for namespace operations)
sudo ./bin/tetherctl gui

# To try it without privileges/hardware:
./bin/tetherctl --dry-run gui

# Directly from the command line:
sudo ./bin/tetherctl start --uplink usb0 --app google-chrome
sudo ./bin/tetherctl status
sudo ./bin/tetherctl stop
```

The panel opens at `http://127.0.0.1:8787`.

Installation, usage, and troubleshooting: [`docs/USAGE.md`](docs/USAGE.md).

---

## Project structure

```
tether_isolator/      Python package (core engine + API + CLI)
  ├── system.py       OS helpers (running commands, interface discovery)
  ├── config.py       profile/settings management
  ├── state.py        runtime state
  ├── engine.py       namespace lifecycle (Model B)
  ├── relay.py        veth relay side-channel
  ├── apps.py         launching apps inside the isolated environment
  ├── manager.py      orchestrator + watchdog (resilience)
  ├── server.py       local HTTP API + web serving
  └── cli.py          command-line interface
webui/                Apple-inspired web UI (HTML/CSS/JS, no dependencies)
systemd/              service unit
bin/tetherctl         launcher
docs/                 architecture, features, task plan, status, TODOs
legacy/               original bash scripts (preserved)
```

Requirements: Python 3.10+, `iproute2`, `dhcpcd` (or `udhcpc`).
For WiFi uplink: `wpa_supplicant`, `iw`. For relay: `nftables` (or
`iptables` if unavailable). For VPN inside the isolated environment:
`openvpn` (2.6+). No external dependencies on the Python side (standard
library only).

## License
Personal/research use. Use at your own risk.
