"""Sparkplug B session state machine: bdSeq / seq bookkeeping and topic
namespace construction (FR-NB-002, FR-NB-003, FR-NB-004).

Per the Sparkplug B v3.0 spec:
- `bdSeq` (birth-death sequence, 0-255 wrapping) identifies one *MQTT
  connection lifetime*. Its value is embedded both in the NDEATH payload
  registered as the MQTT Will *before* connecting, and as a metric in the
  NBIRTH published immediately after connecting — the same value in both,
  so a subscriber can tell a birth and its matching eventual death apart
  from a stale death of a prior session.
- `seq` (0-255 wrapping) numbers NDATA/DDATA/DBIRTH messages in order,
  resetting to 0 on every NBIRTH. NDEATH does not carry a top-level `seq`.
"""

from __future__ import annotations

_SEQ_MODULUS = 256


class SparkplugSession:
    def __init__(self) -> None:
        self._bd_seq = 0
        self._seq = 0

    @property
    def bd_seq(self) -> int:
        return self._bd_seq

    def death_payload_bd_seq(self) -> int:
        """bdSeq to embed in the NDEATH payload registered as the MQTT Will
        before connect() — call this before connecting."""
        return self._bd_seq

    def start_birth(self) -> int:
        """Call once, immediately after a successful MQTT connect, before
        publishing NBIRTH. Resets `seq` to 0 and returns the bdSeq to embed
        in the birth payload (must match the LWT registered for this
        connection, i.e. the value from the preceding death_payload_bd_seq())."""
        self._seq = 0
        return self._bd_seq

    def next_seq(self) -> int:
        """Sequence number for the next NDATA/DDATA/DBIRTH message."""
        value = self._seq
        self._seq = (self._seq + 1) % _SEQ_MODULUS
        return value

    def advance_bd_seq(self) -> None:
        """Call after a disconnect, before the next connect() — advances
        bdSeq so the new session's LWT/birth are distinguishable from the
        previous (possibly still-inflight) NDEATH."""
        self._bd_seq = (self._bd_seq + 1) % _SEQ_MODULUS


def build_topic(
    message_type: str,
    group_id: str,
    edge_node_id: str,
    device_id: str | None = None,
) -> str:
    """Build a Sparkplug B topic:
    `spBv1.0/{group_id}/{message_type}/{edge_node_id}[/{device_id}]`."""
    parts = ["spBv1.0", group_id, message_type, edge_node_id]
    if device_id is not None:
        parts.append(device_id)
    return "/".join(parts)
