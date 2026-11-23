"""Passive bot: never orders anything, always ready."""

from dah.bots.base import Bot


class PassiveBot(Bot):
    name = "passive"
