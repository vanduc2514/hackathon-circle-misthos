"""A deployment that still sets the removed switch is told once, and runs live."""

from __future__ import annotations

import logging

from misthos import main


def test_the_removed_switch_is_named_once(caplog) -> None:
    main._legacy_switch_warned = False
    with caplog.at_level(logging.WARNING, logger="misthos.main"):
        first = main.warn_legacy_switch({"MISTHOS_SIMULATED": "true"})
        second = main.warn_legacy_switch({"MISTHOS_SIMULATED": "true"})

    assert first is not None and "simulation has been removed" in first
    assert second == first
    assert len([r for r in caplog.records if r.name == "misthos.main"]) == 1


def test_nothing_is_said_when_the_switch_is_absent() -> None:
    main._legacy_switch_warned = False
    assert main.warn_legacy_switch({}) is None
