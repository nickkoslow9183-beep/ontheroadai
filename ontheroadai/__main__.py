"""Command line entry point.

    python -m ontheroadai init-db          # create/upgrade the database
    python -m ontheroadai run              # start the dashboard on http://127.0.0.1:5000
    python -m ontheroadai run --port 8000
    python -m ontheroadai research --all   # one Trend Scout run from the command line
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
    run.add_argument("--no-scheduler", action="store_true", help="Don't run scheduled research while the app is open")
    research = sub.add_parser("research", help="Run Trend Scout once (fetches enabled, due sources)")
    research.add_argument("--all", action="store_true", help="Fetch every enabled source now, ignoring intervals")
    args = parser.parse_args(argv)

    db_path = args.db or str(default_db_path())
    if args.command == "init-db":
        conn = connect(db_path)
        init_db(conn)
        conn.close()
        print(f"Database ready at {db_path}")
    elif args.command == "run":
        from .web import create_app

        app = create_app(db_path, start_scheduler=not args.no_scheduler and not args.debug)
        print(f"Dashboard: http://{args.host}:{args.port}  (database: {db_path})")
        app.run(host=args.host, port=args.port, debug=args.debug)
    elif args.command == "research":
        from .agents.orchestrator import Orchestrator

        conn = connect(db_path)
        init_db(conn)
        run_id = Orchestrator(conn).run_research(trigger="cli", force=args.all)
        run = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
        print(f"Run #{run_id}: {run['status']}. {run['sources_ok']} ok, {run['sources_failed']} failed, "
              f"{run['sources_skipped']} not due; {run['items_new']} new items; {run['stories_new']} new stories.")
        for r in conn.execute("SELECT s.name, rs.status, rs.item_count, rs.error FROM research_run_sources rs "
                              "JOIN research_sources s ON s.id = rs.source_id WHERE rs.run_id = ?", (run_id,)):
            print(f"  {r['status']:<18} {r['item_count']:>4} items  {r['name']}  {r['error'][:80]}")
        conn.close()


if __name__ == "__main__":
    main()
