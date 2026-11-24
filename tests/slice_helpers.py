"""Game configs for tests of the slice flow (no setup phase) on core:slice2/slice3.

Since core 0.3.0 these scenarios open with a setup phase (faction and artifact choice, TECH_AND_SETUP.md section 2).
Tests of phases, diplomacy, the server and the waker are about the slice flow, so they drop the setup: every seat
then plays the scenario's first faction (the neutral covenant), exactly as before the setup existed. Core 0.4.0
turns espionage and conquest on in these scenarios; the flow tests turn them off again."""

from __future__ import annotations

from typing import Any

from dah.game import make_config


def slice_config(**kwargs: Any) -> dict[str, Any]:
    """`make_config` without the setup phase (and without its phase limit) and, since core 0.4.0, with espionage and
    conquest off unless the test asks for them (SPY_AND_CONQUEST.md 9: flow tests play the slice rules); since core
    0.5.0 also with the default score (territory only, no objectives) unless the test asks for `scoring`."""
    kwargs.setdefault("systems", {"espionage": False, "conquest": False})
    kwargs.setdefault("scoring", {"territory": 1})      # core 0.5.0 scores all sources; flow tests keep the land
    config = make_config(**kwargs)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    return config
