"""dah-waker CLI (SLICE.md section 9)."""

from __future__ import annotations

import argparse
import os
import sys

from dah.bridge.client import GameApiError, GameClient
from dah.waker.core import POST_GAME_MINUTES, Waker
from dah.waker.dreamteam import DreamTeamClient

TOKEN_ENV = "DREAMTEAM_API_TOKEN"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dah-waker",
                                     description="Будит сессию DreamTeam, когда её месту в игре пора действовать.")
    parser.add_argument("--server", default=os.environ.get("DAH_SERVER"), help="адрес сервера игры (DAH_SERVER)")
    parser.add_argument("--key", default=os.environ.get("DAH_KEY"), help="ключ места (DAH_KEY)")
    parser.add_argument("--dt-url", default="http://127.0.0.1:8765", help="адрес хаба DreamTeam")
    parser.add_argument("--dt-session", required=True, help="ID сессии DreamTeam, созданной через /api/v1")
    parser.add_argument("--stop-on-game-over", action="store_true",
                        help="после game_over остановить сессию (POST .../stop) вместо итогового continue")
    parser.add_argument("--post-game-minutes", type=float, default=POST_GAME_MINUTES,
                        help="сколько минут после game_over следить за расходом сессии, пока она не остановится "
                             f"или не затихнет (по умолчанию {POST_GAME_MINUTES:g})")
    args = parser.parse_args(argv)
    if not args.server or not args.key:
        parser.error("нужны --server и --key (или DAH_SERVER и DAH_KEY)")
    if args.post_game_minutes < 0:
        parser.error("--post-game-minutes не может быть отрицательным")
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
        Waker(game, dreamteam, args.dt_session, stop_on_game_over=args.stop_on_game_over,
              post_game_minutes=args.post_game_minutes).run()
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
