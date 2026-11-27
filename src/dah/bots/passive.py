"""Passive bot: never orders anything, always ready (a pending mixed find takes its default choice)."""

from dah.bots.base import Bot


class PassiveBot(Bot):
    name = "passive"
    decides_finds = False
