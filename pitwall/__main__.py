"""CLI: `python -m pitwall serve|migrate|sync|backfill`."""

from __future__ import annotations

import argparse
import os

from pitwall.config import get_env
from pitwall.db import init_engine
from pitwall.logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="pitwall")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run migrations, then the web app + scheduler")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PITWALL_PORT", "8080")))
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("sync", help="sync the schedule and results once (no scheduler)")
    sub.add_parser("backfill", help="run the historical backfill once")
    args = parser.parse_args()

    env = get_env()
    configure_logging(env.log_level, env.secret_values())
    env.data_dir.mkdir(parents=True, exist_ok=True)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "pitwall.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            log_config=None,
        )
        return

    from pitwall.migrate import upgrade

    engine = init_engine(env.db_url)
    upgrade(engine)
    if args.cmd == "sync":
        from pitwall import jobs

        jobs.schedule_sync_job()
        jobs.results_sync_job()
    elif args.cmd == "backfill":
        from pitwall import jobs

        jobs.backfill_job()


if __name__ == "__main__":
    main()
