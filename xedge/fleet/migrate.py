"""Programmatic Alembic entrypoint (Sprint P1, XEDGE-501): brings the
Fleet Manager's Postgres schema up to the latest revision. Called from
`xedge.fleet.manager_cli` on every startup — like `load_or_create_ca`/
`_load_or_create_token` elsewhere in this package, `alembic upgrade
head` is idempotent (Alembic tracks the applied revision in its own
`alembic_version` table), so this is safe to call unconditionally rather
than needing a separate first-run/upgrade distinction.

Never reads the repo-root `alembic.ini` — that file exists only for a
developer running `alembic revision --autogenerate` from a checkout.
This builds an equivalent `Config` in Python instead, pointed at
`xedge/fleet/migrations` via `importlib.resources` so it resolves
identically whether `xedge` is an editable checkout or a real installed
wheel — unlike `xedge.fleet.manager_cli._default_schema_path`, no
dual-location fallback is needed here: migrations already live inside
the `xedge` package tree (`packages = ["xedge"]` in pyproject.toml), so
there's no repo-root-vs-packaged split to resolve.
"""

from __future__ import annotations

from importlib import resources

from alembic import command
from alembic.config import Config


def run_migrations(database_url: str) -> None:
    """Upgrade the database at `database_url` to the latest revision."""
    migrations_dir = resources.files("xedge") / "fleet" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
