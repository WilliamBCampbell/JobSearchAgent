import argparse
import asyncio
import json
import os
import webbrowser
from .config import Settings
from .db import DB


def main():
    parser = argparse.ArgumentParser(description="Local job-search workspace")
    parser.add_argument(
        "command",
        choices=[
            "migrate",
            "serve",
            "worker",
            "seed",
            "import",
            "ingest",
            "inventory",
            "open",
            "token",
            "recover",
            "submissions",
            "tracker-writes",
        ],
    )
    parser.add_argument("--candidate", choices=["real", "fixture"], default="real")
    parser.add_argument(
        "--mode", choices=["enabled", "disabled", "status"], default="status"
    )
    args = parser.parse_args()
    settings = Settings()
    db = DB(settings)
    if args.command == "tracker-writes":
        if args.mode == "status":
            print(
                "Real tracker writes are enabled; each update requires a reviewed preview."
                if settings.live_tracker_writes
                else "Real tracker writes are disabled."
            )
        else:
            from .config import atomic_write

            if os.getenv("JSA_LIVE_TRACKER_WRITES") is not None:
                raise SystemExit(
                    "Unset JSA_LIVE_TRACKER_WRITES first; the environment overrides the saved setting."
                )
            enabled = args.mode == "enabled"
            atomic_write(
                settings.state / "tracker-settings.json",
                json.dumps({"enabled": enabled}),
            )
            db.audit("tracker_mode_changed", "deployment", {"enabled": enabled})
            print(
                "Saved. Restart the API to apply this setting. No tracker update was queued."
            )
    elif args.command == "submissions":
        if args.mode != "status":
            from .config import atomic_write

            if os.getenv("JSA_LIVE_APPLICATIONS") is not None:
                raise SystemExit(
                    "Unset JSA_LIVE_APPLICATIONS before changing the saved setting; "
                    "the environment overrides it."
                )
            enabled = args.mode == "enabled"
            if enabled and not settings.live_applications:
                pending = db.one(
                    """SELECT count(*) AS count FROM applications a
                    WHERE a.candidate='real' AND (
                        a.state IN ('authorized','submission_attempt','receipt_verification')
                        OR EXISTS (SELECT 1 FROM approvals p WHERE p.application_id=a.id AND NOT p.invalidated)
                        OR EXISTS (SELECT 1 FROM jobs j WHERE j.kind='submit'
                            AND j.status IN ('queued','running')
                            AND j.input->>'application_id'=a.id)
                    )"""
                )
                if pending["count"]:
                    raise SystemExit(
                        "Resolve existing real submission approvals or active attempts before enabling sending."
                    )
            atomic_write(
                settings.state / "submission-settings.json",
                json.dumps({"enabled": enabled}),
            )
            db.audit("submission_mode_changed", "deployment", {"enabled": enabled})
            print("Saved. Restart the API and worker to apply this setting.")
        else:
            print(
                "Real sending requires per-application approval."
                if settings.live_applications
                else "Real sending is disabled."
            )
    elif args.command == "migrate":
        db.migrate()
        print("Migrations applied.")
    elif args.command == "serve":
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=8765, access_log=False)
    elif args.command == "worker":
        from .queue import Worker

        db.migrate()
        asyncio.run(Worker(db).run())
    elif args.command == "seed":
        from .fixtures import seed

        db.migrate()
        print(json.dumps(seed(db), indent=2))
    elif args.command == "import":
        from .intake import Intake

        print(json.dumps(Intake(db).run(args.candidate), indent=2))
    elif args.command == "ingest":
        from .corpus import Corpus

        print(json.dumps(Corpus(db).sync(args.candidate), indent=2))
    elif args.command == "inventory":
        from .intake import Intake

        r = Intake(db).inventory(args.candidate)
        print(
            json.dumps(
                {
                    "files": len(r["files"]),
                    "formats": r["formats"],
                    "fingerprint": r["fingerprint"],
                },
                indent=2,
            )
        )
    elif args.command == "recover":
        from .tracker import Tracker

        print(json.dumps(Tracker(db).recover(args.candidate), indent=2))
    elif args.command == "token":
        print(settings.token())
    elif args.command == "open":
        webbrowser.open(settings.origin + "/#token=" + settings.token())
        print("Opened the authenticated local workspace.")


if __name__ == "__main__":
    main()
