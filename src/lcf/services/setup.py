"""First run: getting an unconfigured installation to a working one.

The application boots without configuration on purpose — `settings()` is never
constructed at import time — so this can serve itself from an app that has no
database to talk to yet. Every step tests before it saves, and the commands it
offers are generated from what the deployer typed, so a copied command and the
stored configuration can never disagree.

Two things guard it, and they are deliberately not a login. The wizard refuses
once the installation is configured, and it refuses a request that did not come
from this machine. A page that writes a database URL to disk is remote code
execution wearing a form, and "first run, from localhost" is the smallest rule
that closes it without inventing accounts this product does not have.
"""

import asyncio
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from lcf.core.config import env_file, reload, settings

# Everything the wizard is allowed to write. A key not on this list cannot be set
# by an HTTP request, whatever the form contains.
WRITABLE = (
    "LCF_DB_URL",
    "LCF_S3_ENDPOINT",
    "LCF_S3_ACCESS_KEY",
    "LCF_S3_SECRET_KEY",
    "LCF_STORAGE_DIR",
    "LCF_LLM_BASE_URL",
    "LCF_LLM_API_KEY",
    "LCF_LLM_MODEL",
)


class Refused(Exception):
    """The wizard declined to act, and the reason is for the deployer to read."""


def is_local(host: str | None) -> bool:
    """Whether a request came from this machine.

    The hostname is taken from the connection, never from a header: an
    `X-Forwarded-For` a caller controls would make this check decorative.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def needed() -> bool:
    """Whether the app cannot serve yet, and every route should divert here.

    Deliberately cheap: it runs on every request, so it asks whether a database
    is configured, not whether it answers. A database that is configured and
    down is a broken installation, not an unconfigured one, and the admin page
    is the better place to learn that.
    """
    return not settings().configured


async def live() -> bool:
    """Whether the database is reachable *and* the schema is current.

    This is what "already set up" means. Configuring a URL is not the same as
    having a working installation, and gating on the URL alone locked the
    deployer out of the wizard one step before the migrations they still needed
    to run.
    """
    s = settings()
    if not s.configured:
        return False
    ok, _ = await test_database(s.db_url)
    if not ok:
        return False
    ready, _ = await _schema_state()
    return ready


async def guard(
    client_host: str | None,
    *,
    changes_database: bool = False,
    writes: tuple[str, ...] = (),
) -> None:
    """Both rules, and they are not a login.

    Locality is checked on every write: this page has no accounts, and a form
    that writes a database URL to disk must not be reachable across a network.
    The database itself is frozen once the schema is live, because repointing a
    running installation at another server from a browser is not setup, it is a
    migration, and it belongs in the configuration file where it can be reviewed.
    """
    if not is_local(client_host):
        raise Refused(
            "Setup can only be run from the machine the application runs on. "
            f"Open it at http://localhost:{settings().port}, or use an SSH tunnel."
        )
    # Checked before anything is tested: a step whose result can never be saved
    # should say so immediately, not after a connection attempt that succeeds
    # and then turns out to have been pointless.
    blocked = shadowed(list(writes))
    if blocked:
        raise Refused(
            f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} set as an "
            "environment variable, which takes precedence over the configuration "
            "file. Saving here would have no effect. Unset it, or change it where "
            "it is set — your shell, the systemd unit, or the compose file."
        )
    if changes_database and await live():
        raise Refused(
            "This installation already has a working database, so the wizard will "
            f"not repoint it. Edit LCF_DB_URL in {env_file()} and restart."
        )


# --- the configuration file --------------------------------------------------


def _quote(value: str) -> str:
    """A value that survives a round-trip through the env file."""
    if value == "" or re.search(r"[\s#'\"$]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def shadowed(keys: list[str] | None = None) -> list[str]:
    """Keys set as real environment variables, which outrank the file.

    pydantic-settings reads the process environment before the env file, so a
    `LCF_DB_URL` exported by a systemd unit or a compose file wins over anything
    written here. Without this check the wizard would save, report success, and
    the application would go on using a different database — the worst kind of
    failure, because nothing looks wrong.
    """
    return [k for k in (keys or WRITABLE) if k in os.environ]


def write(values: dict[str, str]) -> Path:
    """Merge `values` into the env file, keeping every other line as it was.

    Rewriting the file from the settings object would discard comments and any
    key this application does not know about, which is how a deployer loses the
    note they left themselves about why a timeout is what it is.
    """
    unknown = set(values) - set(WRITABLE)
    if unknown:
        raise Refused(f"not settable here: {', '.join(sorted(unknown))}")

    blocked = shadowed(list(values))
    if blocked:
        raise Refused(
            f"{', '.join(blocked)} {'is' if len(blocked) == 1 else 'are'} set as an "
            "environment variable, which takes precedence over the configuration "
            "file. Saving here would have no effect. Unset it, or change it where "
            "it is set — your shell, the systemd unit, or the compose file."
        )

    path = env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").split("\n") if path.is_file() else []

    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Written by the setup wizard.")
        out.extend(f"{k}={_quote(v)}" for k, v in remaining.items())

    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    path.chmod(0o600)  # it holds a database password and an API key
    reload()
    return path


# --- step one: the database --------------------------------------------------


@dataclass
class Database:
    host: str = "localhost"
    port: str = "5432"
    name: str = "lcf"
    user: str = "lcf"
    password: str = ""

    def url(self) -> str:
        return (
            f"postgresql+asyncpg://{quote(self.user, safe='')}:"
            f"{quote(self.password, safe='')}@{self.host}:{self.port}/{self.name}"
        )

    def commands(self) -> list[tuple[str, str]]:
        """Copy-paste ways to create what this configuration expects.

        Generated from the values typed in, never from an example, so the
        command and the configuration cannot drift apart.
        """
        pw = self.password or "choose-a-password"
        return [
            (
                "If PostgreSQL is already running on this machine",
                "Create the role and the database inside it. Needs an account that "
                "may create databases — usually the postgres superuser:",
                f'sudo -u postgres psql -c "CREATE ROLE {self.user} LOGIN PASSWORD \'{pw}\';" '
                f'\\\n  -c "CREATE DATABASE {self.name} OWNER {self.user};"',
            ),
            (
                "If you have no PostgreSQL at all",
                "Start one in Docker. It creates the role and the database on first "
                "boot, so there is nothing else to run:",
                f"docker run -d --name lcf-postgres -p {self.port}:5432 \\\n"
                f"  -v lcf-pgdata:/var/lib/postgresql/data \\\n"
                f"  -e POSTGRES_USER={self.user} -e POSTGRES_PASSWORD={pw} "
                f"-e POSTGRES_DB={self.name} \\\n  postgres:18",
            ),
        ]


def parse_url(url: str) -> Database:
    """Split a URL back into fields, so the form can show what is configured."""
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    return Database(
        host=parts.hostname or "localhost",
        port=str(parts.port or 5432),
        name=(parts.path or "/lcf").lstrip("/") or "lcf",
        user=parts.username or "lcf",
        password=parts.password or "",
    )


async def test_database(url: str) -> tuple[bool, str]:
    """Open one connection and let go of it. Never creates anything."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    if not url.strip():
        return False, "No database URL."
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(select(text("version()")))
        return True, str(version).split(" on ")[0]
    except Exception as exc:  # noqa: BLE001 — every failure is reported the same way
        return False, _explain(exc)
    finally:
        await engine.dispose()


def _explain(exc: Exception) -> str:
    """Turn the driver's wording into the thing to go and do."""
    text = str(exc)
    low = text.lower()
    if "does not exist" in low and "database" in low:
        return f"{text}\nThe server is reachable — the database itself is missing."
    if "password authentication failed" in low or "no password supplied" in low:
        return f"{text}\nThe server is reachable — the user or password is wrong."
    if "connect call failed" in low or "connection refused" in low:
        return f"{text}\nNothing is listening there. Is PostgreSQL running?"
    if "could not translate host name" in low or "name or service not known" in low:
        return f"{text}\nThat hostname does not resolve from this machine."
    return text


async def migrate() -> tuple[bool, str]:
    """Bring the schema to head, exactly as `alembic upgrade head` would.

    Run in a thread: Alembic drives a synchronous engine of its own, and calling
    it straight from the event loop deadlocks against the running server.
    """

    def run() -> str:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parents[3]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        cfg.set_main_option("sqlalchemy.url", settings().db_url)

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            command.upgrade(cfg, "head")
        return out.getvalue().strip()

    try:
        output = await asyncio.to_thread(run)
        return True, output or "Schema is at head."
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# --- step two: storage -------------------------------------------------------


async def test_storage() -> tuple[bool, str]:
    """Create the buckets and write a byte, because a store that lists but
    cannot be written to is a store that fails on the first export instead."""
    import lcf.storage as storage

    def run() -> str:
        backend = storage.store()
        outcome = backend.ensure(settings().buckets)
        failed = [f"{n}: {s}" for n, s in outcome.items() if s.startswith("failed")]
        if failed:
            raise RuntimeError("; ".join(failed))
        uri = backend.put(settings().s3_bucket_exports, ".lcf-write-test", b"ok", "text/plain")
        backend.get(settings().s3_bucket_exports, ".lcf-write-test")
        return f"{backend.describe()} — buckets ready, write verified ({uri})"

    try:
        return True, await asyncio.to_thread(run)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# --- step three: the assistant ----------------------------------------------


async def test_llm(base_url: str, model: str, api_key: str) -> tuple[bool, str]:
    from openai import AsyncOpenAI

    if not base_url.strip():
        return False, "No endpoint."
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")
        served = [m.id async for m in (await client.models.list())]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    if not model.strip():
        return False, "Reachable. It serves: " + (", ".join(served) or "nothing")
    if model in served:
        return True, f"Reachable, serving {model}."
    return False, f"Reachable, but it does not serve {model!r}. It offers: {', '.join(served)}"


SUGGESTED = [
    ("Ollama", "http://localhost:11434/v1"),
    ("vLLM", "http://localhost:8000/v1"),
    ("LM Studio", "http://localhost:1234/v1"),
]


# --- the whole of it ---------------------------------------------------------


@dataclass
class Step:
    key: str
    title: str
    blurb: str
    done: bool = False
    detail: str = ""


@dataclass
class State:
    steps: list[Step] = field(default_factory=list)
    env_path: Path = field(default_factory=env_file)

    @property
    def done(self) -> bool:
        return all(s.done for s in self.steps)


async def state() -> State:
    """Where setup has got to, recomputed rather than remembered: the deployer
    may have created the database in a terminal while this page was open."""
    s = settings()

    db_ok, db_detail = (False, "Not configured yet.")
    schema_ok, schema_detail = False, "Waiting on the database."
    if s.db_url:
        db_ok, db_detail = await test_database(s.db_url)
        if db_ok:
            schema_ok, schema_detail = await _schema_state()

    import lcf.storage as storage

    backend = storage.store()
    store_ok, store_detail = True, f"{backend.describe()} (local disk, nothing to set up)"
    if backend.scheme == "s3":
        store_ok, store_detail = await test_storage()

    llm_ok, llm_detail = False, "Optional. Drafting and the quality gate need it."
    if s.llm_base_url:
        llm_ok, llm_detail = await test_llm(s.llm_base_url, s.llm_model, s.llm_api_key)

    return State(
        steps=[
            Step("database", "Database", "PostgreSQL, for everything the app remembers.",
                 db_ok, db_detail),
            Step("schema", "Schema", "The tables, brought to the current version.",
                 schema_ok, schema_detail),
            Step("storage", "Storage", "Where exports and Word templates are kept.",
                 store_ok, store_detail),
            Step("assistant", "Assistant", "Any OpenAI-compatible endpoint.",
                 llm_ok, llm_detail),
        ]
    )


async def _schema_state() -> tuple[bool, str]:
    """Whether the tables are there and at the version this build expects.

    Asked over the async driver the application already depends on. Stripping
    `+asyncpg` to borrow a synchronous engine looks simpler and silently needs
    psycopg2, which is not installed — so the check failed on every healthy
    installation and reported it as a broken schema.
    """
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    try:
        root = Path(__file__).resolve().parents[3]
        head = await asyncio.to_thread(
            lambda: ScriptDirectory(str(root / "alembic")).get_current_head()
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not read the migration history: {exc}"

    engine = create_async_engine(settings().db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            current = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception as exc:  # noqa: BLE001
        if "alembic_version" in str(exc) and "does not exist" in str(exc):
            return False, "No tables yet. Run the migrations."
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()

    if current == head:
        return True, f"At head ({current})."
    return False, f"At {current or 'nothing'}, head is {head}. Run the migrations."
