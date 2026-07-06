# xEdge Hardware Compatibility Assessment

**Date:** 2026-07-05
**Scope:** Device-by-device compatibility check for deploying xEdge (v0.1.0, Phase 1-2 MVP) on
Raspberry Pi, Teltonika, Advantech, Siemens, Moxa, Dell, and Toradex hardware.
**Method:** Each device is checked against xEdge's own stated platform requirements (below), not
generic vendor marketing claims — research current as of 2026-07-05; vendor firmware/OS images
change over time, so re-verify before a purchasing decision.

## Baseline: xEdge's stated requirements

From `docs/requirements/HLR.md` (NFR-C-001..005) and the watchdog's systemd dependency
(`xedge/core/watchdog.py`, which no-ops gracefully if systemd isn't present — a lost feature, not a
hard blocker):

| Requirement | Value |
|---|---|
| Kernel | ≥ 5.10 LTS |
| CPU architecture | AMD64, ARM64, or ARMv7 (explicitly **not** MIPS) |
| OS | Ubuntu 22.04/24.04, Debian 11/12, Alpine Linux 3.18+, or Yocto (kirkstone+) — "without modification" |
| Deployment | Docker ≥24.0, Podman ≥4.0, or bare-metal systemd ≥248 |
| Filesystem | Writable `/data`, `/var/log`, `/tmp`; supports read-only root elsewhere |
| Python | ≥ 3.11 |

## Summary

| Tier | Count | Devices |
|---|---|---|
| **Recommended** | 5 | Raspberry Pi 4/5 (+CM4/5), Advantech ARK-series, Advantech UNO-series, Siemens SIMATIC IOT2050, Toradex Verdin + industrial carrier |
| **Conditional** | 3 | Teltonika RUTC40/41/50, Dell Edge Gateway 3000-series, Moxa UC-8100A-ME-T |
| **Not compatible** | 5 | Teltonika RUT9xx/RUTX/TRB1/TRB2, Advantech ECU-series, Advantech WISE-series, Moxa MGate-series, Siemens IOT2040 (legacy) |

---

## Recommended — zero/minimal modification

Ships (or can ship) an OS on xEdge's exact supported list, at or above the kernel floor, with
systemd and/or Docker present out of the box.

### Raspberry Pi 4 / 5, Compute Module 4 / 5

| | |
|---|---|
| Architecture | ARM64 (Cortex-A72/A76) |
| OS | Ubuntu Server 24.04 (arm64) or Raspberry Pi OS Bookworm 64-bit |
| Kernel | 6.1–6.6, well above the floor |
| Init / runtime | systemd native; Docker/Podman work normally |
| Python | 3.11 (Bookworm) / 3.12 (Ubuntu 24.04) by default |
| RS-485 | Sequent Microsystems or generic RS-485 HAT; USB-RS485 adapter also works |

The only real-world gap: bare boards aren't ruggedized. For 24/7 industrial duty, pair a Pi 4/5 or
CM4/5 with a **Revolution Pi** (Kunbus) DIN-rail carrier — EN 61131-2 rated, 24V industrial power
in. Pi 3B+/Zero 2 W are architecturally fine but RAM-starved (1 GB / 512 MB) for the full protocol
stack running concurrently.

### Advantech ARK-series (e.g. ARK-1222, ARK-2230L, ARK-3500)

| | |
|---|---|
| Architecture | AMD64 (Atom / Celeron / Core i3–i7) |
| OS | Ubuntu — Advantech is a Canonical-certified hardware vendor |
| Runtime | Docker/Podman standard on Ubuntu x86 |
| Serial | 2–8× RS-232/422/485 depending on model |
| Rating | Fanless, −20–60°C+, DIN-rail/panel mount, shock/vibration rated |
| RAM/storage | 4–32 GB DDR, SSD/M.2 |

Genuinely industrial hardware built for exactly this workload class — the strongest true-industrial
option researched.

### Advantech UNO-series (e.g. UNO-2372V3)

| | |
|---|---|
| Architecture | AMD64 (Celeron / Atom / Twin Lake N-series) |
| OS | Ubuntu 24.04 explicitly supported (newer V3 models) |
| Serial | 2× RS-232/422/485, expandable via iDoor/mPCIe modules |
| Rating | Fanless, −20–60°C, 50G shock, DIN-rail/wall mount |

Older UNO-2372G ships Ubuntu 18.04 — fine as a base but needs a manual Python 3.11+ install; the V3
refresh clears the bar directly.

### Siemens SIMATIC IOT2050 (Basic / Advance)

| | |
|---|---|
| Architecture | ARM64 (TI Sitara AM65x, quad Cortex-A53) |
| OS | Siemens-maintained Debian 12/Bookworm image (`meta-iot2050`) |
| Kernel | 6.1, with a 6.12 track in progress |
| Init / runtime | systemd native; Docker a first-class build layer |
| Serial | 1× RS-232/485 + Arduino header for expansion |

Purpose-built as an open Linux IIoT gateway — not just Debian-compatible but Debian-native,
maintained directly by Siemens.

### Toradex Verdin (iMX8M Plus/Mini) + industrial carrier (e.g. Zinnia/Ivy)

| | |
|---|---|
| Architecture | ARM64 (NXP i.MX8M, quad Cortex-A53) |
| OS | Torizon OS — Debian-based, Docker-native by design |
| Init / runtime | systemd present; containers are the primary app model |
| Serial | RS-485/RS-232/CAN via carrier board, isolated |

Best fit for OEM design-in or volume deployment rather than an off-the-shelf single-unit purchase —
pricing is quote-based.

---

## Conditional — real caveats

A real path exists, but only for specific SKUs, and only with a caveat that needs verifying or
working around before committing.

### Teltonika RUTC40 / RUTC41 / RUTC50

The only Docker-capable models in the entire RutOS lineup.

| | |
|---|---|
| Architecture | ARM64 (Cortex-A53) — in spec |
| RAM/flash | 1 GB / 8 GB — workable via container |
| Docker | Present — the one differentiator vs. the rest of the Teltonika line |
| Kernel | Unconfirmed; RutOS base has run 5.4, below the 5.10 floor |
| Watchdog | RutOS uses procd, not systemd — sd_notify feature is lost |

A Docker container with a glibc Python 3.11 image sidesteps RutOS's musl userspace entirely — **but
confirm the host kernel is actually ≥5.10** before treating this as viable. Every other Teltonika
line (RUT9xx, RUTX, TRB1/TRB2, non-Docker TRB5) does not have this path.

### Dell Edge Gateway 3000-series (3001/3002/3003)

| | |
|---|---|
| Architecture | AMD64 (Atom E3805, Bay Trail) — in spec |
| Stock OS | Ubuntu 16/18.04 or Windows 10 IoT — not on the supported list |
| Serial | 2× RS-232 only, no native RS-485 |
| Status | ~2014-era SoC; no confirmed EOL date but reads as legacy |

Architecture clears the bar, but you'd be installing a newer distro yourself and adding a
USB/PCIe RS-485 adapter — not "without modification."

### Moxa UC-8100A-ME-T (and general-purpose UC-series)

| | |
|---|---|
| Architecture | ARMv7 (Cortex-A8) — in spec |
| OS | Moxa Industrial Linux = Debian 9 on kernel 4.4 |
| Serial | 2× RS-232/422/485 — best serial story of the group |

Would need a full BSP/kernel upgrade to reach 5.10+ — out of policy for "without modification."
Newer Moxa UC-2100/8200-generation boards may ship fresher Debian; documentation wasn't conclusive
enough to move them out of this tier.

---

## Not compatible

Wrong CPU architecture, wrong device class entirely, or a stock OS/kernel far enough below the
floor that reaching compliance isn't realistic.

### Teltonika RUT9xx, RUTX, TRB1/TRB2 (non-Docker)

| | |
|---|---|
| Architecture | MIPS (RUT9xx, TRB2) — explicitly outside AMD64/ARM64/ARMv7 |
| RAM/flash | 128–256 MB / 16–256 MB |
| Userspace | musl libc, procd init — no systemd, pip installs frequently fail |

A router/firewall/cellular-modem appliance built on constrained embedded Linux, not a general
compute host — the RUTC line above is the one narrow exception.

### Advantech ECU-series

| | |
|---|---|
| Architecture | ARM Cortex-A8/A9, 256 MB RAM (v1) |
| OS | Proprietary RT-Linux (v1); Ubuntu 20.04 on V2, still RAM-starved |

Designed as a fixed-function Modbus/IEC-60870 translation appliance (EdgeLink), not a Python
application host — wrong fit even where Ubuntu is present.

### Advantech WISE-series

Wireless/Ethernet I/O module — sensor/I-O node, not a computer. No Linux userspace to deploy custom
applications on at all. WISE-PaaS/EdgeHub is Advantech's own management layer, not a target
runtime — not applicable here.

### Moxa MGate-series

Fixed-function protocol converter. No general Linux userspace exposed — can't run a
Python/FastAPI application at all.

### Siemens SIMATIC IOT2040 (legacy)

| | |
|---|---|
| Architecture | Intel Quark X1020 — single-core, 32-bit-only x86 |
| OS | Originally shipped Yocto, not Debian |

Fails the AMD64/ARM64/ARMv7 requirement outright — treat as end-of-life, not a real candidate. Use
the IOT2050 instead.

---

## Notes

- "Conditional" entries need a hands-on kernel/OS check on real hardware before purchasing at
  volume; "Not compatible" entries are ruled out on architecture or device class, not just
  inconvenience.
- This assessment doesn't cover NFR-C-004 (read-only root filesystem) or NFR-C-005 (hotplug
  detection) per device — those need validation once specific target hardware is selected.
