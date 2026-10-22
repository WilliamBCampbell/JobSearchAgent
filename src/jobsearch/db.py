from contextlib import contextmanager
from uuid import uuid4
import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .config import Settings, REPO


def uid():
    return str(uuid4())


def js(value):
    return Jsonb(value)


class DB:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def tx(self):
        with psycopg.connect(self.settings.database, row_factory=dict_row) as conn:
            # Never fall back to another profile's tables. Extensions live separately.
            if (
                self.settings.schema != "public"
                and not conn.execute(
                    "SELECT to_regnamespace(%s) AS name", (self.settings.schema,)
                ).fetchone()["name"]
            ):
                raise ValueError("User profile storage is missing")
            conn.execute(
                pgsql.SQL("SET LOCAL search_path TO {}, jsa_extensions").format(
                    pgsql.Identifier(self.settings.schema)
                )
            )
            yield conn

    def all(self, sql, params=()):
        with self.tx() as c:
            return c.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.tx() as c:
            return c.execute(sql, params).rowcount

    def migrate(self):
        with self.tx() as c:
            self.migrate_connection(c)

    def migrate_connection(self, c):
        c.execute("SELECT pg_advisory_xact_lock(881903)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        for path in sorted((REPO / "migrations").glob("*.sql")):
            if not c.execute(
                "SELECT 1 FROM schema_migrations WHERE name=%s", (path.name,)
            ).fetchone():
                c.execute(path.read_text())
                c.execute(
                    "INSERT INTO schema_migrations(name) VALUES(%s)", (path.name,)
                )

    def audit(self, event, subject, detail=None, conn=None):
        # Audit records reference evidence IDs. They never contain filled form values or secrets.
        sql = "INSERT INTO audit(event,subject,detail) VALUES(%s,%s,%s)"
        args = (event, subject, js(detail or {}))
        if conn:
            conn.execute(sql, args)
        else:
            self.execute(sql, args)

    def metric(self, name, value=1):
        self.execute(
            "INSERT INTO metrics(name,value) VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET value=metrics.value+EXCLUDED.value",
            (name, value),
        )

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=%s", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, js(value)),
        )
