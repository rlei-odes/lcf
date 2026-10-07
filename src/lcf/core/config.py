"""Configuration, and the one file it comes from.

The env file is resolved to an absolute path rather than left as `.env`, which
pydantic-settings would read relative to the working directory: running
`lcf serve` from anywhere but the source checkout silently lost every setting and
presented as "database not configured". The same path is what the setup wizard
writes, so reading and writing can never disagree about where config lives.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def env_file() -> Path:
    """Where configuration is read from and written to.

    `LCF_ENV_FILE` wins, so a deployment can put it anywhere. Otherwise a source
    checkout keeps it beside `pyproject.toml`, which is where the README has
    always told people to put it, and an installed copy uses the XDG config
    directory rather than writing next to the package.
    """
    explicit = os.environ.get("LCF_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve()

    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file():
        return root / ".env"

    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser().resolve() / "lcf" / ".env"


def data_dir() -> Path:
    """Where the app keeps bytes of its own, when nothing external holds them."""
    explicit = os.environ.get("LCF_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()

    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file():
        return root / "var"

    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser().resolve() / "lcf"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LCF_", env_file=env_file(), env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = 8090
    log_level: str = "INFO"
    debug: bool = False

    # PostgreSQL or SQLite, and no default either way: an installation that has
    # not chosen is not configured, and the setup wizard exists to say so rather
    # than let a guess through.
    db_url: str = ""

    llm_base_url: str = ""
    llm_api_key: str = "not-needed"
    llm_model: str = ""
    llm_max_images_per_call: int = 4
    # A ceiling, not a target. Without it a constrained array schema lets the
    # model emit rows until it exhausts the context — minutes of generation for
    # a table that should have four entries. Set generously: a JSON grammar also
    # permits unlimited whitespace, and truncating mid-object costs a full retry.
    llm_max_tokens: int = 4096
    llm_context_window: int = 32768
    llm_temperature: float = 0.2
    llm_timeout_s: int = 180
    llm_concurrency: int = 4

    # Object storage. An empty endpoint is not a missing configuration: it means
    # the local disk holds the bytes, which is what most single installations
    # want and what needs no setup at all.
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_bucket_uploads: str = "lcf-uploads"
    s3_bucket_templates: str = "lcf-templates"
    s3_bucket_exports: str = "lcf-exports"
    # Where the local backend keeps them. Relative paths resolve under `data_dir`.
    storage_dir: str = ""

    worker_in_process: bool = True

    # The event log grows with every model call and nothing else bounds it, so
    # the bound is here rather than in a cron nobody installed. Rotation runs
    # when a background job finishes — the thing that fills the log is the thing
    # that trims it, which is what makes a busy installation nobody looks at
    # safe as well as a quiet one.
    event_log_keep: int = 20000
    # Store the resolved prompt and the reply alongside a model call, so "why did
    # it write that" is answerable on the admin page rather than only in theory
    # (DESIGN §5.8). They are the author's material, so this is a switch: off,
    # the log keeps what a call cost and nothing it said. Rotation applies either
    # way — an exchange belongs to its log row and goes when that row goes.
    log_prompts: bool = True

    # The evidence desk. `auto` prefers docling for PDFs when the extra is
    # installed and falls back to the built-in parser without complaint when it
    # is not; `builtin` never reaches for it even if it is there.
    ingest_parser: str = "auto"
    # Characters, not tokens, and knowingly so: nothing in the application counts
    # tokens yet, so the bound is set low enough that the difference cannot
    # matter. When a tokenizer lands it replaces this rather than wrapping it.
    ingest_chunk_chars: int = 1800
    ingest_max_file_mb: int = 25
    # How many chunks one question may spend on the assistant. The cost dial:
    # a case of forty chunks and four questions costs at most four times this.
    extract_top_k: int = 6
    # A model-written pattern runs over megabytes of somebody else's text, and
    # `re` has no timeout. This is the backtracking guard, in seconds.
    extract_pattern_timeout_s: float = 2.0

    # The house style every generated starter template is built onto — a .docx
    # holding header, footer, logo, fonts and colours, and no content.
    #
    # The setup page uploads one into object storage, and that takes precedence
    # over this. The path remains for an installation that bakes its branding into
    # an image, where there is nobody to upload anything.
    docx_base_template: str = ""

    @property
    def buckets(self) -> list[str]:
        return [self.s3_bucket_uploads, self.s3_bucket_templates, self.s3_bucket_exports]

    @property
    def configured(self) -> bool:
        """Whether there is enough here to run. Storage and the assistant both
        have working defaults; a database does not."""
        return bool(self.db_url.strip())

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_dir).expanduser() if self.storage_dir else data_dir() / "storage"


@lru_cache
def settings() -> Settings:
    return Settings()


def reload() -> Settings:
    """Re-read the env file after it has been written.

    Every cache that holds a derived value has to go with it, or the app keeps
    serving the old database on a new configuration.
    """
    from lcf.core import db
    from lcf.storage import store

    settings.cache_clear()
    db.engine.cache_clear()
    db.session_factory.cache_clear()
    store.cache_clear()
    return settings()
