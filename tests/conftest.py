import os
from urllib.parse import urlsplit, urlunsplit
import psycopg
from psycopg import sql
import pytest
from jobsearch.config import Settings
from jobsearch.db import DB


@pytest.fixture(scope="session")
def test_database():
    source = Settings().database
    name = "jobsearch_test_" + str(os.getpid())
    u = urlsplit(source)
    admin = urlunsplit((u.scheme, u.netloc, "/postgres", u.query, u.fragment))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = urlunsplit((u.scheme, u.netloc, "/" + name, u.query, u.fragment))
    yield database
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def writing_skill(tmp_path, monkeypatch):
    """Synthetic instruction bundle; tests do not depend on a user's home files."""
    skill = tmp_path / "writing-skill" / "SKILL.md"
    guide = skill.parent / "references/ai-writing-detection.md"
    guide.parent.mkdir(parents=True)
    skill.write_text("# Fixture writing skill\nSelf-check before returning text.\n")
    guide.write_text("# Fixture reference\nRemove unsupported enthusiasm.\n")
    monkeypatch.setenv("JSA_NO_AI_SLOP_SKILL", str(skill))
    return skill


@pytest.fixture
def db(test_database, tmp_path, writing_skill):
    settings = Settings(
        database=test_database,
        state=tmp_path / "private",
        workspace=tmp_path / "source",
        origin="http://127.0.0.1:8766",
        headless=True,
    )
    settings.workspace.mkdir()
    settings.fixture_workspace.mkdir(parents=True)
    database = DB(settings)
    database.migrate()
    tables = database.all(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'schema_migrations'"
    )
    with database.tx() as c:
        c.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier(t["tablename"]) for t in tables)
            )
        )
    return database
