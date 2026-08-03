"""Embedded MQTT broker (Sprint C5, XEDGE-453/454; ADR-012 §3): `amqtt`,
promoted from test-only to a runtime dependency — see license-audit.md §4
item 6 for the license/maintenance/ARM-footprint verification (ADR-012
prerequisite P-5) that promotion required first.

A listening *service*, not a client: other MQTT clients (SCADA, cloud
integrations, xEdge's own generic MQTT subscriber/publisher if pointed at
localhost) connect *to* this — the reverse of every other northbound/
driver component in this codebase, and new attack surface on the device
by design (ADR-012 §3's own risk register R-09). TLS, authentication, and
an ACL model are therefore not optional extras (XEDGE-454) — anonymous
access defaults *off*.

Authentication and topic ACLs are amqtt's own plugins
(`amqtt.plugins.authentication`, `amqtt.plugins.topic_checking`), wired
here rather than reimplemented: `FileAuthPlugin` verifies against a
generated `username:sha512_crypt-hash` file (via the same `passlib`
already a transitive dependency of amqtt itself), and
`TopicAccessControlListPlugin` enforces per-user publish/subscribe topic
patterns using ordinary MQTT wildcard (`+`/`#`) semantics.

**A second verified, non-obvious limitation**: the two ACL directions are
NOT independently "off by default" once the plugin loads at all (i.e. as
soon as either `publish_acl` or `subscribe_acl` is non-empty). Read from
`amqtt/plugins/topic_checking.py` directly: PUBLISH has a backward-compat
carve-out ("no publish_acl configured -> assume permitted"), but SUBSCRIBE
does not -- an empty `subscribe_acl` dict is treated as "zero topics
allowed for anyone," not "unrestricted." Confirmed empirically: setting
`publish_acl` alone denies every subscribe from every user (SUBACK
'Unspecified error'). There is no way to restrict only one direction --
an operator who only wants to restrict publishing must still list every
legitimate subscriber in `subscribe_acl` (a `["#"]` entry grants that user
unrestricted subscribe access, if that is all that's wanted).

**A real, checked limitation, not an oversight**: amqtt's listener
`cafile` option — confirmed directly from its `broker.py` source while
building `tests/fixtures/mqtt_broker.py` earlier this sprint — always sets
`ssl.CERT_OPTIONAL`, never `CERT_REQUIRED`. It cannot enforce mandatory
client certificates (mTLS) no matter how this module configures it. That
is why the access-control story here is username/password + topic ACLs,
not mTLS — the one mechanism amqtt cannot actually deliver is deliberately
not offered as if it worked.

**A third verified issue, this one operational rather than a security
gap**: `Broker.shutdown()` can hang indefinitely if any connection ever
reached the broker without completing an MQTT handshake -- reproduced
directly against a bare `amqtt.broker.Broker` with a throwaway asyncio
`open_connection()`-then-close (no MQTT bytes sent at all) before calling
`shutdown()`; it never returned even after 8+ seconds. That is exactly
what a bare TCP health-check/liveness probe against this port (a load
balancer, a container orchestrator) would do. `stop()` below bounds this
with its own timeout for that reason -- a hung broker shutdown must not
be able to block the rest of this device's shutdown sequence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from amqtt.broker import Broker
from amqtt.contexts import BrokerConfig, ListenerConfig
from passlib.apps import custom_app_context as pwd_context

from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_BASE_PLUGINS: dict[str, dict[str, object]] = {
    "amqtt.plugins.logging_amqtt.EventLoggerPlugin": {},
    "amqtt.plugins.sys.broker.BrokerSysPlugin": {"sys_interval": 20},
}

_STOP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class BrokerUserCredential:
    username: str
    password: str
    """Plaintext, as supplied by config — hashed before amqtt (or disk)
    ever sees it; never the plaintext itself is persisted."""


@dataclass(frozen=True, slots=True)
class MqttBrokerConfig:
    host: str = "0.0.0.0"  # nosec B104 — a broker must accept connections from other devices/clients on the network, unlike the loopback-only REST API
    port: int = 1883
    tls_enabled: bool = False
    tls_certfile_path: str | None = None
    tls_keyfile_path: str | None = None
    allow_anonymous: bool = False
    users: tuple[BrokerUserCredential, ...] = ()
    # username -> allowed topic patterns, ordinary MQTT +/# wildcard
    # semantics (amqtt.plugins.topic_checking.TopicAccessControlListPlugin).
    # Absent entirely (both empty) means no ACL plugin is loaded at all —
    # any authenticated user may publish/subscribe anywhere, same as a
    # broker with no ACL configured.
    publish_acl: dict[str, list[str]] = field(default_factory=dict)
    subscribe_acl: dict[str, list[str]] = field(default_factory=dict)


class MqttBrokerStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class MqttBrokerService:
    """Owns the embedded broker's start/stop lifecycle. `password_file_path`
    is where the generated `username:hash` file is written on `start()` —
    a subdirectory of the device's own data_dir, matching every other
    generated-secret file in this codebase (fleet device token, self-signed
    TLS keys)."""

    def __init__(self, config: MqttBrokerConfig, password_file_path: Path) -> None:
        self._config = config
        self._password_file_path = password_file_path
        self._broker: Broker | None = None

    async def start(self) -> None:
        if self._broker is not None:
            raise MqttBrokerStateError("start() already called")
        cfg = self._config

        listener_kwargs: dict[str, object] = {"bind": f"{cfg.host}:{cfg.port}"}
        if cfg.tls_enabled:
            if not cfg.tls_certfile_path or not cfg.tls_keyfile_path:
                raise ValueError(
                    "tls_certfile_path and tls_keyfile_path are required when tls_enabled is true"
                )
            listener_kwargs["ssl"] = True
            listener_kwargs["certfile"] = cfg.tls_certfile_path
            listener_kwargs["keyfile"] = cfg.tls_keyfile_path

        plugins: dict[str, dict[str, object]] = dict(_BASE_PLUGINS)
        if cfg.allow_anonymous:
            plugins["amqtt.plugins.authentication.AnonymousAuthPlugin"] = {"allow_anonymous": True}
        else:
            _write_password_file(self._password_file_path, cfg.users)
            plugins["amqtt.plugins.authentication.FileAuthPlugin"] = {
                "password_file": str(self._password_file_path)
            }
        if cfg.publish_acl or cfg.subscribe_acl:
            plugins["amqtt.plugins.topic_checking.TopicAccessControlListPlugin"] = {
                "publish_acl": cfg.publish_acl,
                "acl": cfg.subscribe_acl,
            }

        broker_config = BrokerConfig(
            listeners={"default": ListenerConfig(**listener_kwargs)},
            plugins=plugins,
        )
        broker = Broker(broker_config)
        await broker.start()
        self._broker = broker
        logger.info(
            "mqtt_broker.started",
            host=cfg.host,
            port=cfg.port,
            tls_enabled=cfg.tls_enabled,
            allow_anonymous=cfg.allow_anonymous,
        )

    async def stop(self) -> None:
        if self._broker is None:
            return
        # Bounded: see module docstring -- a client connection that never
        # completed an MQTT handshake (e.g. a bare TCP health-check probe)
        # can make amqtt's own shutdown() hang forever. This device's
        # overall shutdown must complete regardless.
        try:
            await asyncio.wait_for(self._broker.shutdown(), timeout=_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("mqtt_broker.stop_timed_out", timeout_seconds=_STOP_TIMEOUT_SECONDS)
        self._broker = None
        logger.info("mqtt_broker.stopped")

    def is_running(self) -> bool:
        return self._broker is not None


def _write_password_file(path: Path, users: tuple[BrokerUserCredential, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{user.username}:{pwd_context.hash(user.password)}" for user in users]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
