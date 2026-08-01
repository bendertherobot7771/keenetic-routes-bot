from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys

from . import __version__
from .app import BotApp
from .config import Config, ConfigError
from .rci import KeeneticRciClient, RciError
from .telegram import TelegramClient


def configure_logging(config: Config) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.log_file,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Telegram bot for native Keenetic routes"
    )
    parser.add_argument(
        "--config",
        default="/opt/etc/keenetic-routes-bot/config.env",
        help="path to KEY=VALUE configuration file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and Keenetic RCI, then exit",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(config)
    router = KeeneticRciClient(
        config.rci_url,
        token=config.rci_token,
        token_header=config.rci_token_header,
        token_prefix=config.rci_token_prefix,
        timeout=config.request_timeout,
    )
    if args.check:
        try:
            version = router.version()
            groups = router.list_groups()
            rules = router.list_dns_routes()
            routes = router.list_ipv4_routes()
        except RciError as exc:
            print(f"RCI check failed: {exc}", file=sys.stderr)
            return 1
        release = (
            version.get("release")
            or version.get("version")
            or version.get("title")
            or "unknown"
        )
        print(
            f"OK: KeeneticOS={release}, groups={len(groups)}, "
            f"dns_routes={len(rules)}, ipv4_routes={len(routes)}"
        )
        return 0
    telegram = TelegramClient(config.bot_token, timeout=config.request_timeout)
    app = BotApp(config, telegram, router)

    def stop(signum: int, _frame) -> None:
        logging.getLogger(__name__).info("Received signal %s, stopping", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        app.run()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot stopped")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
