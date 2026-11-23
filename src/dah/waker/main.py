"""dah-waker CLI (SLICE.md section 9)."""

from __future__ import annotations

import argparse
import os
import sys

from dah.bridge.client import GameApiError, GameClient
from dah.waker.core import Waker
from dah.waker.dreamteam import DreamTeamClient

TOKEN_ENV = "DREAMTEAM_API_TOKEN"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dah-waker",
                                     description="Будит сессию DreamTeam, когда её месту в игре пора действовать.")
    parser.add_argument("--server", default=os.environ.get("DAH_SERVER"), help="адрес сервера игры (DAH_SERVER)")
    parser.add_argument("--key", default=os.environ.get("DAH_KEY"), help="ключ места (DAH_KEY)")
    parser.add_argument("--dt-url", default="http://127.0.0.1:8765", help="адрес хаба DreamTeam")
    parser.add_argument("--dt-session", required=True, help="ID сессии DreamTeam, созданной через /api/v1")
    args = parser.parse_args(argv)
    if not args.server or not args.key:
        parser.error("нужны --server и --key (или DAH_SERVER и DAH_KEY)")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"dah-waker: задайте токен DreamTeam в переменной {TOKEN_ENV}", file=sys.stderr)
        sys.exit(2)
    game = GameClient(args.server, args.key, timeout=30.0, host="waker")
    dreamteam = DreamTeamClient(args.dt_url, token)
    try:
        Waker(game, dreamteam, args.dt_session).run()
    except GameApiError as exc:
        print(f"dah-waker: сервер игры отказал: {exc.code} {exc.message}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        game.close()
        dreamteam.close()


if __name__ == "__main__":
    main()
