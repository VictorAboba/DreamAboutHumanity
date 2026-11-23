"""Game configs for tests of the slice flow (no setup phase) on core:slice2/slice3.

Since core 0.3.0 these scenarios open with a setup phase (faction and artifact choice, TECH_AND_SETUP.md section 2).
Tests of phases, diplomacy, the server and the waker are about the slice flow, so they drop the setup: every seat
then plays the scenario's first faction (the neutral covenant), exactly as before the setup existed."""

from __future__ import annotations

from typing import Any

from dah.game import make_config


def slice_config(**kwargs: Any) -> dict[str, Any]:
    """`make_config` without the setup phase (and without its phase limit)."""
    config = make_config(**kwargs)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    return config
