"""CLI: `python -m pitwall serve|migrate|sync|backfill|test|create-user|set-password`."""

from __future__ import annotations

import argparse
import os
import sys

from pitwall.config import get_env
from pitwall.db import init_engine
from pitwall.logging_setup import configure_logging


def _ask_password() -> str:
    import getpass

    if not sys.stdin.isatty():  # e.g. `echo "pw" | docker compose exec -T pitwall ...`
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("New password (10+ characters): ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The passwords don't match.")
    return first


def manage_user(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from pitwall.db import session_scope
    from pitwall.models import User
    from pitwall.users import AccountError, create_user, set_password

    password = _ask_password()
    try:
        with session_scope() as db:
            if args.cmd == "create-user":
                user = create_user(db, args.username, password, is_admin=args.admin)
                print(f"Created {user.username}{' (admin)' if user.is_admin else ''}.")
            else:
                user = db.scalars(
                    select(User).where(User.username == args.username.strip().lower())
                ).first()
                if user is None:
                    print(f"No user called {args.username!r}.")
                    return 1
                set_password(db, user, password)
                user.active = True
                print(f"Password set for {user.username}; the account is unlocked.")
    except AccountError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


def send_test(channel: str, username: str = "admin") -> int:
    from sqlalchemy import select

    from pitwall.db import session_scope
    from pitwall.models import User
    from pitwall.notify.builders import test_message
    from pitwall.notify.dispatch import notify
    from pitwall.notify.targets import channel_urls

    with session_scope() as db:
        user = db.scalars(select(User).where(User.username == username)).first()
        uid = user.id if user else None
    if uid is None:
        print(f"No user called {username!r}.")
        return 1
    if not channel_urls(channel, user_id=uid):
        print(f"No {channel} channels configured (add them in Settings).")
        return 1
    msg = test_message(channel)
    msg.user_id = uid
    ok, detail = notify(channel, msg)
    print(f"Test {channel}: {'sent' if ok else 'FAILED'}" + ("" if ok else f" ({detail})"))
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="pitwall")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run migrations, then the web app + scheduler")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PITWALL_PORT", "8080")))
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("sync", help="sync the schedule and results once (no scheduler)")
    sub.add_parser("backfill", help="run the historical backfill once")
    test = sub.add_parser("test", help="send a test notification (exit code 1 on failure)")
    test.add_argument("channel", choices=["email", "webhook"])
    test.add_argument("--user", default="admin", help="whose channels to test (default admin)")
    cu = sub.add_parser("create-user", help="create an account (asks for the password)")
    cu.add_argument("username")
    cu.add_argument("--admin", action="store_true", help="give the account admin rights")
    sp = sub.add_parser("set-password", help="set a user's password and unlock the account")
    sp.add_argument("username")
    args = parser.parse_args()

    os.umask(0o077)  # the DB holds notification credentials: keep new files private
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
    from pitwall.db import session_scope
    from pitwall.users import bootstrap

    with session_scope() as db:
        bootstrap(db)
    if args.cmd == "test":
        sys.exit(send_test(args.channel, args.user))
    if args.cmd in ("create-user", "set-password"):
        sys.exit(manage_user(args))
    if args.cmd == "sync":
        from pitwall import jobs

        jobs.schedule_sync_job()
        jobs.results_sync_job()
    elif args.cmd == "backfill":
        from pitwall import jobs

        jobs.backfill_job()


if __name__ == "__main__":
    main()
