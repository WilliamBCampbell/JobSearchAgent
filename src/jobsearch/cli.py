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
            "handoff",
        ],
    )
    parser.add_argument("--candidate", choices=["real", "fixture"], default="real")
    parser.add_argument("--profile", default=os.getenv("JSA_PROFILE", "default"))
    parser.add_argument(
        "--application", help="Application ID for a portable Codex handoff"
    )
    parser.add_argument(
        "--mode", choices=["enabled", "disabled", "status"], default="status"
    )
    args = parser.parse_args()
    settings = Settings()
    from .user_profiles import UserProfiles, selected_settings

    profiles = UserProfiles(settings)
    if args.command not in ("serve", "worker", "migrate"):
        settings = selected_settings(settings, args.profile)
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
        if args.mode == "enabled":
            raise SystemExit(
                "In-app submission is retired. Use jobsearch handoff --application ID."
            )
        print(
            "Submission runs in Codex CLI. The app exports reviewed files and application context."
        )
    elif args.command == "handoff":
        from .submission_handoff import SubmissionHandoff

        if not args.application:
            parser.error("handoff requires --application")
        result = SubmissionHandoff(db).export(args.application)
        print(json.dumps(result, indent=2))
    elif args.command == "migrate":
        profiles.initialize()
        print("Migrations applied.")
    elif args.command == "serve":
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=8765, access_log=False)
    elif args.command == "worker":
        from .queue import Worker, ProfileWorker

        profiles.initialize()
        worker = (
            Worker(DB(profiles.resolve(args.profile)))
            if args.profile != "default"
            else ProfileWorker(profiles)
        )
        asyncio.run(worker.run())
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
        webbrowser.open(
            settings.origin + settings.profile_url("/") + "#token=" + settings.token()
        )
        print("Opened the authenticated local workspace.")


if __name__ == "__main__":
    main()
