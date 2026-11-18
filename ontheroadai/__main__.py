"""Command line entry point.

    python -m ontheroadai init-db          # create/upgrade the database
    python -m ontheroadai run              # start the dashboard on http://127.0.0.1:5000
    python -m ontheroadai run --port 8000
"""

from __future__ import annotations

import argparse

from .db import connect, default_db_path, init_db


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="ontheroadai")
    parser.add_argument("--db", help="SQLite file (default: data/ontheroadai.db or $ONTHEROADAI_DB)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Create tables and seed businesses/brands/accounts")
    run = sub.add_parser("run", help="Start the dashboard")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=5000)
    run.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db or str(default_db_path())
    if args.command == "init-db":
        conn = connect(db_path)
        init_db(conn)
        conn.close()
        print(f"Database ready at {db_path}")
    elif args.command == "run":
        from .web import create_app

        app = create_app(db_path)
        print(f"Dashboard: http://{args.host}:{args.port}  (database: {db_path})")
        app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
