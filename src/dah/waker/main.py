"""dah-waker CLI (SLICE.md section 9)."""

from __future__ import annotations

import argparse
import os
import sys

from dah.bridge.client import GameApiError, GameClient
from dah.waker.core import ACK_SECONDS, DEFAULT_WAKE_MODE, POST_GAME_MINUTES, WAKE_MODES, Waker
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
                        help="после game_over остановить сессию (POST .../stop) вместо итогового сообщения всем "
                             "(или continue при --wake-mode continue)")
    parser.add_argument("--post-game-minutes", type=float, default=POST_GAME_MINUTES,
                        help="сколько минут после game_over следить за расходом сессии, пока она не остановится "
                             f"или не затихнет (по умолчанию {POST_GAME_MINUTES:g})")
    parser.add_argument("--wake-mode", choices=WAKE_MODES, default=DEFAULT_WAKE_MODE,
                        help="как будить: all — сообщение всем участникам роя (POST .../messages, у остановленной "
                             "сессии — continue), continue — только continue (доходит до одного координатора), "
                             f"both — и то и другое (по умолчанию {DEFAULT_WAKE_MODE})")
    parser.add_argument("--ack-seconds", type=float, default=ACK_SECONDS,
                        help="за сколько секунд после пробуждения игрок должен обратиться к игре, иначе телеметрия "
                             f"wake_unanswered (по умолчанию {ACK_SECONDS:g})")
    args = parser.parse_args(argv)
    if not args.server or not args.key:
        parser.error("нужны --server и --key (или DAH_SERVER и DAH_KEY)")
    if args.post_game_minutes < 0:
        parser.error("--post-game-minutes не может быть отрицательным")
    if args.ack_seconds < 0:
        parser.error("--ack-seconds не может быть отрицательным")
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
              post_game_minutes=args.post_game_minutes, wake_mode=args.wake_mode,
              ack_seconds=args.ack_seconds).run()
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
