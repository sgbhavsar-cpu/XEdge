from __future__ import annotations

from xedge.northbound.sparkplug.session import SparkplugSession, build_topic


def test_initial_state() -> None:
    session = SparkplugSession()
    assert session.bd_seq == 0
    assert session.death_payload_bd_seq() == 0


def test_start_birth_resets_seq_and_returns_matching_bd_seq() -> None:
    session = SparkplugSession()
    death_bd_seq = session.death_payload_bd_seq()
    birth_bd_seq = session.start_birth()
    assert birth_bd_seq == death_bd_seq


def test_next_seq_increments_and_wraps_at_256() -> None:
    session = SparkplugSession()
    session.start_birth()
    assert session.next_seq() == 0
    assert session.next_seq() == 1
    assert session.next_seq() == 2

    # 3 calls made so far (returned 0, 1, 2); 252 more brings the total to
    # 255 calls, so the next call is the 256th and returns 255 (the wrap
    # boundary), and the one after that wraps back to 0.
    for _ in range(252):
        session.next_seq()
    assert session.next_seq() == 255
    assert session.next_seq() == 0


def test_start_birth_resets_seq_after_prior_increments() -> None:
    session = SparkplugSession()
    session.start_birth()
    session.next_seq()
    session.next_seq()
    session.start_birth()  # re-birth (e.g. reconnect) resets seq
    assert session.next_seq() == 0


def test_advance_bd_seq_wraps_at_256() -> None:
    session = SparkplugSession()
    for _ in range(255):
        session.advance_bd_seq()
    assert session.bd_seq == 255
    session.advance_bd_seq()
    assert session.bd_seq == 0


def test_advance_bd_seq_changes_death_payload_value_for_next_connection() -> None:
    session = SparkplugSession()
    first = session.death_payload_bd_seq()
    session.advance_bd_seq()
    second = session.death_payload_bd_seq()
    assert first != second


def test_build_topic_edge_node_scope() -> None:
    assert build_topic("NBIRTH", "factory1", "edge01") == "spBv1.0/factory1/NBIRTH/edge01"


def test_build_topic_device_scope() -> None:
    assert (
        build_topic("DDATA", "factory1", "edge01", "plc01") == "spBv1.0/factory1/DDATA/edge01/plc01"
    )
