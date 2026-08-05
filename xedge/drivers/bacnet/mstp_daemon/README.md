# xEdge BACnet MS/TP daemon

Sprint P7 (XEDGE-166). A standalone C program, one instance per RS-485
port, that links `third_party/bacnet-stack` directly as an MS/TP master
and exposes a minimal ReadProperty-only client over a local Unix domain
socket. See `docs/planning/license-audit.md` §4 item 11 for why this is
a separate linked daemon rather than an in-process Python C-extension
binding, and `main.c`'s own top-of-file comment for the full design
rationale.

xEdge's Python driver (`xedge/drivers/bacnet/mstp_client.py`, a later PR
in this sprint) owns one daemon subprocess per configured MS/TP driver
instance — starts it, talks to it, and lets `DriverSupervisor`'s existing
restart/backoff handle a daemon crash by simply respawning it, the same
as any other driver connection failure.

## Building

```sh
git submodule update --init --recursive   # if not already done
cd xedge/drivers/bacnet/mstp_daemon
make
```

Requires `build-essential` (gcc, make) and nothing else beyond `pthread`
and `libm` — no external dependencies for this build slice. Verified
building clean on a stock `ubuntu:24.04` container (Sprint P7 PR history).

## Running

```sh
./xedge-bacnet-mstp-daemon \
  --iface /dev/ttyUSB0 --mac 3 --baud 38400 \
  --max-info-frames 1 --max-master 127 \
  --device-instance 4194302 --socket /run/xedge/bacnet-mstp-3.sock
```

- `--mac`: this daemon's own MS/TP MAC address on the segment (0-127).
- `--device-instance`: this daemon's own BACnet device instance number —
  distinct from any device instance it *reads from*, which is supplied
  per-request instead (see protocol below).
- One daemon per physical RS-485 port; run a separate instance (distinct
  `--iface`/`--mac`/`--socket`) for each configured MS/TP driver instance.

## IPC protocol

Newline-delimited JSON over the Unix domain socket named by `--socket`.
One request in flight at a time, matching the single Python driver
process that owns this daemon 1:1 — there is no request queueing or
concurrent-client support, by design.

Request:
```json
{"device_instance": 260001, "mac": 1, "object_type": "analog-input", "object_instance": 1, "property_id": "present-value"}
```

- `device_instance`/`mac`: the *target* device being read, not this
  daemon's own identity. `mac` is required (0-255) — there is no WhoIs/
  I-Am discovery; the target's MS/TP MAC address is expected to already
  be known from the driver's own tag configuration, the same way every
  other xEdge driver already requires an explicit device address (a
  Modbus slave ID, an EtherNet/IP host) rather than auto-discovering one.
- `object_type`/`property_id`: BACnet standard string names
  (`analog-input`, `binary-value`, `present-value`, `object-name`, ...)
  — anything `bactext_object_type_strtol`/`bactext_property_strtol`
  (bacnet-stack) recognize.

Response, success:
```json
{"ok": true, "value": 72.5}
```
`value` is a JSON number, boolean, or string depending on the property's
BACnet application tag. Deliberately no further semantic coercion here
(e.g. "is this Enumerated actually a boolean BinaryPV") — that logic
already exists for the BACnet/IP driver
(`xedge/drivers/bacnet/client.py::_coerce_value`) and belongs in Python,
not duplicated in C.

Response, failure:
```json
{"ok": false, "error": "timeout"}
```
`error` covers: malformed request JSON, an unknown object type/property
name, a BACnet Error/Abort/Reject from the target device, or a timeout
(no response within ~10s — well past bacnet-stack's own APDU
timeout×retries, as a last-resort backstop).

## Verification

Built and run against a real `bacnet-stack` server (`apps/server`,
built with `BACDL=mstp`) over a `socat`-created virtual serial pair —
a genuine MS/TP token-passing join and a real ReadProperty round trip,
not a mocked transport. Confirmed both the success path (numeric and
string property reads) and the error paths (unknown object, unreachable
MAC, malformed request) — including that the daemon keeps answering
correctly after every error case, not just the first one.
