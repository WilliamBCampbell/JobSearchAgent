"""User profiles own database schemas and private files; workspaces are sources."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from pydantic import BaseModel, Field, field_validator

from .config import REPO, atomic_write
from .db import DB


class ProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    workspace: str = Field(min_length=1, max_length=4096)

    @field_validator("name", "workspace")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter a profile name and source document folder")
        return value


class UserProfiles:
    def __init__(self, settings):
        self.settings = settings
        self.db = DB(settings)

    def initialize(self):
        self.db.migrate()
        self.db.execute(
            "INSERT INTO public.user_profiles(id,name,workspace,schema_name) "
            "VALUES('default','My profile',%s,'public') ON CONFLICT(id) DO NOTHING",
            (str(self.settings.workspace),),
        )
        for profile in self.list():
            if profile["id"] != "default":
                DB(self.resolve(profile["id"])).migrate()

    def list(self):
        return self.db.all(
            "SELECT id,name,workspace,created_at FROM public.user_profiles ORDER BY created_at,id"
        )

    def resolve(self, profile_id):
        row = self.db.one(
            "SELECT * FROM public.user_profiles WHERE id=%s", (profile_id,)
        )
        if not row:
            raise ValueError("User profile not found")
        if profile_id == "default":
            return replace(self.settings, workspace=Path(row["workspace"]))
        # IDs and schema names come from the registry, never from a request path.
        return replace(
            self.settings,
            profile_id=row["id"],
            schema=row["schema_name"],
            workspace=Path(row["workspace"]),
            state=self.settings.state / "profiles" / row["id"],
            auth_state=self.settings.state,
            live_applications=None,
            live_tracker_writes=None,
        )

    def create(self, data: ProfileInput):
        workspace = Path(data.workspace).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Use an absolute path for the source document folder")
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise ValueError("The source document folder must already exist")
        if any(
            workspace.is_relative_to(root) or root.is_relative_to(workspace)
            for root in (REPO, self.settings.state)
        ):
            raise ValueError(
                "Choose a source folder outside the repository and private state"
            )
        profile_id = uuid4().hex
        schema = "profile_" + profile_id
        with self.db.tx() as c:
            # Serialize registration and schema migrations; publish only a complete profile.
            c.execute("SELECT pg_advisory_xact_lock(881903)")
            rows = c.execute(
                "SELECT name,workspace FROM public.user_profiles"
            ).fetchall()
            if any(row["name"].lower() == data.name.lower() for row in rows):
                raise ValueError("A user profile with that name already exists")
            for row in rows:
                other = Path(row["workspace"]).resolve()
                if workspace.is_relative_to(other) or other.is_relative_to(workspace):
                    raise ValueError("Choose a separate source folder for this profile")
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            c.execute(
                sql.SQL("SET LOCAL search_path TO {}, jsa_extensions").format(
                    sql.Identifier(schema)
                )
            )
            self.db.migrate_connection(c)
            state = self.settings.state / "profiles" / profile_id
            atomic_write(state / "submission-settings.json", '{"enabled": false}')
            atomic_write(state / "tracker-settings.json", '{"enabled": false}')
            c.execute(
                "INSERT INTO public.user_profiles(id,name,workspace,schema_name) VALUES(%s,%s,%s,%s)",
                (profile_id, data.name, str(workspace), schema),
            )
        return next(row for row in self.list() if row["id"] == profile_id)


class ProfileRoutingMiddleware:
    """Dispatch each request to a bound app; no process-wide active profile exists."""

    def __init__(self, app, profiles, factory):
        self.app, self.profiles, self.factory = app, profiles, factory
        self.apps = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope)
        header = request.headers.get("x-jsa-profile")
        query = request.query_params.get("profile")
        if header and query and header != query:
            return await JSONResponse({"detail": "Conflicting user profiles"}, 400)(
                scope, receive, send
            )
        profile_id = header or query or "default"
        if profile_id == "default":
            return await self.app(scope, receive, send)
        if profile_id not in self.apps:
            try:
                settings = self.profiles.resolve(profile_id)
            except ValueError as exc:
                return await JSONResponse({"detail": str(exc)}, 404)(
                    scope, receive, send
                )
            self.apps[profile_id] = self.factory(settings)
        if request.url.path == "/api/profiles":
            return await self.app(scope, receive, send)
        await self.apps[profile_id](scope, receive, send)


def selected_settings(settings=None, profile_id=None):
    import os
    from .config import Settings

    settings = settings or Settings()
    profile_id = profile_id or os.getenv("JSA_PROFILE", "default")
    if profile_id == "default":
        db = DB(settings)
        if not db.one("SELECT to_regclass('public.user_profiles') AS table_name")[
            "table_name"
        ]:
            return settings
        if not db.one("SELECT 1 FROM public.user_profiles WHERE id='default'"):
            return settings
    return UserProfiles(settings).resolve(profile_id)
