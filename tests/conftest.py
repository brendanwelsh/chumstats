"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "captures"


@pytest.fixture(scope="session")
def online_capture() -> Path:
    p = CAPTURES / "rl_20260514_214503.jsonl"
    if not p.is_file():
        pytest.skip("online capture fixture missing")
    return p


@pytest.fixture(scope="session")
def exhibition_capture() -> Path:
    p = CAPTURES / "rl_20260514_215932.jsonl"
    if not p.is_file():
        pytest.skip("exhibition capture fixture missing")
    return p


@pytest.fixture(scope="session")
def all_captures() -> list[Path]:
    caps = sorted(CAPTURES.glob("rl_*.jsonl"))
    if not caps:
        pytest.skip("capture fixtures missing")  # skip, don't run on empty data
    return caps
