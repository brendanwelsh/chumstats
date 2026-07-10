"""config_wizard ini read/write/idempotency."""

from __future__ import annotations

from pathlib import Path

from chumstats import config_wizard
from chumstats.config_wizard import (
    RLInstall,
    read_ini,
    restore_install_template,
    run_wizard,
    write_packet_rate,
)


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


# ----- integrity-verify fix: never touch the install dir --------------------

def _fake_install(tmp_path):
    """A RL install whose install template is modified (rate=30) and whose
    user-space config lives elsewhere."""
    install_dir = tmp_path / "steamapps" / "common" / "rocketleague"
    cfg_dir = install_dir / "TAGame" / "Config"
    cfg_dir.mkdir(parents=True)
    ini = cfg_dir / "DefaultStatsAPI.ini"
    ini.write_text(SAMPLE_INI.replace("PacketSendRate=0", "PacketSendRate=30"), encoding="utf-8")
    user_cfg = tmp_path / "Documents" / "My Games" / "Rocket League" / "TAGame" / "Config" / "TAStatsAPI.ini"
    return RLInstall(source="manual", install_path=install_dir, ini_path=ini, config_path=user_cfg)


def test_restore_install_template_pristine_and_cleans_baks(tmp_path):
    inst = _fake_install(tmp_path)
    # Litter the install dir with old-style backups.
    (inst.ini_path.parent / "DefaultStatsAPI.ini.bak").write_text("x", encoding="utf-8")
    (inst.ini_path.parent / "DefaultStatsAPI.ini.bak.1783048879").write_text("x", encoding="utf-8")

    rewrote, removed = restore_install_template(inst)
    assert rewrote is True
    assert removed == 2
    assert read_ini(inst.ini_path).packet_send_rate == 0
    assert not list(inst.ini_path.parent.glob("*.bak*"))

    # Idempotent: second call rewrites nothing.
    rewrote2, removed2 = restore_install_template(inst)
    assert rewrote2 is False and removed2 == 0


def test_run_wizard_writes_userspace_not_install(tmp_path, monkeypatch):
    inst = _fake_install(tmp_path)
    monkeypatch.setattr(config_wizard, "detect_install", lambda manual_path=None: inst)
    monkeypatch.setattr(config_wizard, "is_rl_running", lambda: False)

    rep = run_wizard(enable=True, rate=30)

    # Enabled the API in the USER-SPACE file...
    assert rep.error is None
    assert rep.config_path == inst.config_path
    assert inst.config_path.is_file()
    assert read_ini(inst.config_path).packet_send_rate == 30
    # ...and left the install template pristine (integrity-safe).
    assert rep.install_restored is True
    assert read_ini(inst.ini_path).packet_send_rate == 0
    # Backups go next to the user-space file, never in the install dir.
    assert not list(inst.ini_path.parent.glob("*.bak*"))


def test_run_wizard_legacy_install_write_still_targets_install(tmp_path, monkeypatch):
    inst = _fake_install(tmp_path)
    inst.ini_path.write_text(SAMPLE_INI, encoding="utf-8")  # start disabled
    monkeypatch.setattr(config_wizard, "detect_install", lambda manual_path=None: inst)
    monkeypatch.setattr(config_wizard, "is_rl_running", lambda: False)

    rep = run_wizard(enable=True, rate=30, legacy_install_write=True)

    assert rep.config_path == inst.ini_path
    assert read_ini(inst.ini_path).packet_send_rate == 30
    assert rep.install_restored is False  # legacy mode never restores
