# Onboarding Walkthrough

XEDGE-493 (Sprint H1). A first-boot-to-live-data walkthrough for a new
xEdge deployment. Every command, endpoint, and field name below was
exercised against a running instance while writing this document — see
"How this was verified" at the end.

For a protocol-specific starting config, see
[Protocol Quick Starts](protocol-quick-starts.md). For the full config file
model (sections, hot-reload, secrets), see the
[Configuration Guide](configuration-guide.md).

## 1. Install and start

```bash
git clone https://github.com/sgbhavsar-cpu/XEdge
cd XEdge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,test]"
cp config/examples/modbus-minimal.yaml config/local.yaml
```

`modbus-minimal.yaml` ships with `data_dir` unset and one disabled driver —
enough to boot cleanly with no field devices reachable yet. Set `data_dir`
to a writable path before starting (a fresh directory is created for you;
it holds the SQLite cold-store tier and hot-reload version history):

```yaml
data_dir: /var/lib/xedge   # or any writable path for a first trial
```

Start it:

```bash
xedge --config config/local.yaml
```

```
Uvicorn running on https://127.0.0.1:8080 (Press CTRL+C to quit)
```

`api.port` defaults to `8080`; the Web UI and REST API share the same
HTTPS listener. The certificate is self-signed on first boot (`tls`
section) — a browser will warn about it, and `curl`/`httpx` need `-k` /
`verify=False` until it is replaced with one from a real CA (see
[Hardening Guide](../security/hardening-guide.md) §2.1).

## 2. First-login setup

Open `https://<device-host>:8080/ui/setup`. This page exists exactly once:
after the first admin password is set, every later visit to `/ui/setup`
redirects to `/ui/login` instead. It creates exactly one account, fixed
username `admin` — there is no username field, only a password:

```bash
curl -k -X POST https://127.0.0.1:8080/api/v1/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"password": "<a strong password>"}'
```

Then log in (`/ui/login`, or `POST /api/v1/auth/login` with
`{"username": "admin", "password": "..."}`). Create a named account per
operator immediately afterward (`/ui/users`) with the least-privileged
role their job needs — `readonly`, `operator`, `auditor`, or `admin` — per
[Hardening Guide](../security/hardening-guide.md) §2.2. Don't run a
multi-operator deployment on the shared `admin` login.

## 3. Add your first driver

`/ui/config/drivers/new` lists every registered driver type (Modbus TCP/RTU,
OPC UA, BACnet/IP, EtherNet/IP, SNMP client, MQTT subscriber, SNMP trap
receiver) and renders a schema-driven form for whichever one you pick — the
same JSON Schema in `config/schema/drivers/*.schema.json` that validates
the file underneath, so the form can never accept something the file
format would reject. Field help text comes from the schema's own
`description`s.

Fill in the device connection (host/port, or serial port for RTU), save,
then add a tag group (`.../tag-groups/new`) and at least one tag
(`.../tags/new`) — an address/OID/node-id and an `id` xEdge will use to
refer to it everywhere else (`{instance_id}/{tag_id}`, e.g.
`plc_01/temperature`).

Prefer starting from a working example over a blank form: every protocol
has one under `config/examples/` (see
[Protocol Quick Starts](protocol-quick-starts.md)) — copy its `drivers:`
entry into the raw-YAML "Advanced" editor (`/ui/config/advanced`) if you'd
rather paste a known-good block than click through the form tag-by-tag.

Config changes save to disk and hot-reload automatically (`config_management.
hot_reload_enabled: true` by default, polling every
`poll_interval_seconds` — 2 seconds unless changed) — no restart needed to
see a newly added driver start polling.

## 4. Confirm live data

`/ui/dashboard` lists every configured driver instance with its connection
state; click through to `/ui/dashboard/drivers/{instance_id}` for that
instance's own tags, current values, and quality. The same data is
available as JSON without a browser:

```bash
curl -k -b cookies.txt https://127.0.0.1:8080/api/v1/drivers
curl -k -b cookies.txt https://127.0.0.1:8080/api/v1/drivers/plc_01/tags
```

If a tag shows Bad quality or the driver never reaches Connected, check
`/ui/config/drivers/{driver_id}/validate` (also runs automatically on
save) before assuming the device itself is unreachable — most first-time
issues are a wrong address/OID/object-identifier, not a network problem.

## 5. Set up alarms and notifications (optional)

`assets` and `alarms` are both optional metadata layers over the tags you
just configured — nothing below is required to get live data flowing, but
most real deployments want at least threshold alarms. Add a rule under the
`alarms` section's `rules` list — a `tag_id` (the fully-qualified
`{instance_id}/{tag_id}` from step 3) plus any of `high`/`high_high`/`low`/
`low_low`/`rate_of_change_per_second` — via the raw-YAML Advanced editor
(`/ui/config/core/alarms` links there: `rules` is array-typed, the same
schema_forms gap `alarms.rules` has always had — see
[Configuration Guide](configuration-guide.md#web-ui-vs-raw-yaml) for why),
then configure SMTP (`smtp-example.yaml`) or SNMP TRAP/INFORM
(`snmp-notify-example.yaml`) if you want raises/clears pushed out rather
than only visible on `/ui/alarms`.

## 6. Publish data northbound (optional)

The `northbound` section turns on the MQTT publisher (Sparkplug B by
default, or `generic_json` for a plain configurable payload — CRD §4.10).
Point `northbound.mqtt.host` at your broker (an external one, or xEdge's
own embedded broker — `mqtt_broker.enabled: true`,
[mqtt-broker-example.yaml](../../config/examples/mqtt-broker-example.yaml))
and data already flowing through the dashboard starts publishing on the
same `publish_interval_seconds` cadence.

## 7. Enroll in a Fleet Manager (optional, multi-device only)

Skip this section entirely for a single, standalone device. For a fleet:
on the Fleet Manager, provision a join token for this device's chosen
`device_id` (`POST /api/v1/fleet/join-tokens`). On the device, set:

```yaml
fleet:
  enabled: true
  manager_url: https://fleet-manager.example:8090      # enrollment/admin port
  device_manager_url: https://fleet-manager.example:8091 # mTLS device port
  device_id: plc-line-1
  join_token: "${SECRET:fleet_join_token}"
```

On next start, the agent generates its own keypair/CSR, redeems the join
token, and receives a CA-signed certificate plus a device token — every
heartbeat and config pull after that authenticates with the certificate,
never the (now-consumed) join token. `GET /api/v1/fleet/status` on the
device, or the Fleet Manager's own device list, confirms enrollment
succeeded.

## How this was verified

Every endpoint, request body, and response shape above (`/api/v1/auth/
setup`'s single-`password`-field body, `/api/v1/auth/login`'s username+
password pair, the full `/ui/*` and `/api/v1/*` route list, `/ui/setup`
existing exactly once) was read from the running application's own
OpenAPI schema and exercised with real requests against a live instance
started from `modbus-minimal.yaml` during Sprint H1 — not inferred from
route naming conventions or copied from another project.
