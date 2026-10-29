from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from pypay import Channel

from .config import DEFAULT_DATABASE_PATH, ServiceConfig
from .db import Database
from .projects_service import (
    create_project,
    list_project_views,
    rotate_project_credentials,
    sync_channel_accounts,
)
from .security import SecretBox
from .timeutil import utcnow

logger = logging.getLogger("pypay_service.cli")


def _database_path() -> Path:
    return Path(os.environ.get("PYPAY_SERVICE_DATABASE_PATH", DEFAULT_DATABASE_PATH)).expanduser()


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("PYPAY_SERVICE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _channel_map(values: list[str]) -> dict[Channel, str]:
    mapping: dict[Channel, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--channel must be channel=account_id, got {value!r}")
        channel_name, account_id = value.split("=", 1)
        try:
            channel = Channel(channel_name)
        except ValueError as exc:
            raise SystemExit(f"unknown channel: {channel_name}") from exc
        mapping[channel] = account_id
    return mapping


def _cmd_migrate(args: argparse.Namespace) -> int:
    database = Database(_database_path())
    if args.direction == "up":
        applied = database.migrate_up()
        print("applied:", ", ".join(applied) if applied else "nothing to do")
    else:
        reverted = database.migrate_down(args.steps)
        print("reverted:", ", ".join(reverted) if reverted else "nothing to do")
    return 0


def _cmd_create_project(args: argparse.Namespace) -> int:
    config = ServiceConfig.from_env()
    database = Database(config.database_file)
    database.migrate_up()
    sync_channel_accounts(database, config, utcnow())
    box = SecretBox(config.master_key)
    created = create_project(
        database,
        box,
        config,
        project_id=args.project_id,
        name=args.name,
        notify_url=args.notify_url,
        return_urls=args.return_url or [],
        channels=_channel_map(args.channel),
    )
    print("project created; secrets are shown once:")
    print(f"PYPAY_BASE_URL={config.public_base_url}")
    print(f"PYPAY_KEY_ID={created.project.api_key_id}")
    print(f"PYPAY_API_SECRET={created.api_secret}")
    print(f"PYPAY_NOTIFY_SECRET={created.notify_secret}")
    return 0


def _cmd_list_projects(args: argparse.Namespace) -> int:
    database = Database(_database_path())
    projects = list_project_views(database)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": project.id,
                        "name": project.name,
                        "status": project.status.value,
                        "api_key_id": project.api_key_id,
                        "notify_url": project.notify_url,
                    }
                    for project in projects
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for project in projects:
        print(
            f"{project.id}\t{project.status.value}\t{project.name}\t"
            f"{project.api_key_id}\t{project.notify_url}"
        )
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    config = ServiceConfig.from_env()
    database = Database(config.database_file)
    rotated = rotate_project_credentials(
        database,
        SecretBox(config.master_key),
        args.project_id,
        rotate_api=args.api,
        rotate_notify=args.notify,
    )
    print(f"PYPAY_KEY_ID={rotated.api_key_id}")
    if rotated.api_secret:
        print(f"PYPAY_API_SECRET={rotated.api_secret}")
    if rotated.notify_secret:
        print(f"PYPAY_NOTIFY_SECRET={rotated.notify_secret}")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from .gateways import GatewayRegistry
    from .notifier import HttpNotifier
    from .worker import PaymentWorker

    config = ServiceConfig.from_env()
    database = Database(config.database_file)
    database.migrate_up()
    box = SecretBox(config.master_key)
    worker = PaymentWorker(
        database,
        config,
        box,
        HttpNotifier(config),
        GatewayRegistry(config),
    )
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        logger.info("worker stopped")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pypay-service")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="apply or roll back database migrations")
    migrate.add_argument("direction", choices=["up", "down"])
    migrate.add_argument("--steps", type=int, default=1)
    migrate.set_defaults(func=_cmd_migrate)

    create = sub.add_parser("create-project", help="create a project and print its credentials")
    create.add_argument("--id", dest="project_id", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--notify-url", required=True)
    create.add_argument("--return-url", action="append")
    create.add_argument(
        "--channel",
        action="append",
        required=True,
        help="channel=account_id, repeatable (e.g. wechat=wechat-default)",
    )
    create.set_defaults(func=_cmd_create_project)

    listing = sub.add_parser("list-projects", help="list projects")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_cmd_list_projects)

    rotate = sub.add_parser("rotate-secret", help="rotate a project secret")
    rotate.add_argument("--project", dest="project_id", required=True)
    rotate.add_argument("--api", action="store_true")
    rotate.add_argument("--notify", action="store_true")
    rotate.set_defaults(func=_cmd_rotate)

    worker = sub.add_parser("worker", help="run outbox delivery and reconciliation")
    worker.set_defaults(func=_cmd_worker)

    serve = sub.add_parser("serve", help="run the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "rotate-secret" and not (args.api or args.notify):
        parser.error("choose --api, --notify, or both")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
