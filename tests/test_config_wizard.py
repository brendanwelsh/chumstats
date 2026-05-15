"""config_wizard ini read/write/idempotency."""

from __future__ import annotations

from pathlib import Path

from carball.config_wizard import read_ini, write_packet_rate


SAMPLE_INI = """\
[TAGame.MatchStatsExporter_TA]

; Port the client will listen for connections on
Port=49123

; How many times per second the game sends the update state (capped at 120, 0 disables this feature)
PacketSendRate=0
"""


def test_read_ini(tmp_path):
    p = tmp_path / "DefaultStatsAPI.ini"
    p.write_text(SAMPLE_INI, encoding="utf-8")
    st = read_ini(p)
    assert st.port == 49123
    assert st.packet_send_rate == 0
    assert st.enabled is False


def test_write_enable(tmp_path):
    p = tmp_path / "DefaultStatsAPI.ini"
    p.write_text(SAMPLE_INI, encoding="utf-8")
    before, after, bak = write_packet_rate(p, 30)
    assert before.packet_send_rate == 0
    assert after.packet_send_rate == 30
    assert bak is not None and bak.is_file()
    # The comment line above PacketSendRate must be preserved.
    txt = p.read_text(encoding="utf-8")
    assert "PacketSendRate=30" in txt
    assert "; Port the client will listen for connections on" in txt
    assert "; How many times per second" in txt


def test_write_idempotent(tmp_path):
    p = tmp_path / "DefaultStatsAPI.ini"
    p.write_text(SAMPLE_INI.replace("PacketSendRate=0", "PacketSendRate=30"), encoding="utf-8")
    before, after, bak = write_packet_rate(p, 30)
    assert before.packet_send_rate == 30
    assert after.packet_send_rate == 30
    assert bak is None  # no backup created when nothing changes


def test_write_creates_missing(tmp_path):
    p = tmp_path / "DefaultStatsAPI.ini"
    assert not p.is_file()
    before, after, bak = write_packet_rate(p, 30)
    assert before.packet_send_rate == 0
    assert after.packet_send_rate == 30
    assert p.is_file()
    assert bak is None  # nothing to back up


def test_disable(tmp_path):
    p = tmp_path / "DefaultStatsAPI.ini"
    p.write_text(SAMPLE_INI.replace("PacketSendRate=0", "PacketSendRate=30"), encoding="utf-8")
    before, after, bak = write_packet_rate(p, 0)
    assert before.enabled is True
    assert after.enabled is False
