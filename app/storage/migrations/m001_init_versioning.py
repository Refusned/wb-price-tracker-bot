"""Migration 001: initialise schema_migrations table.

This is the bootstrap migration. The ``schema_migrations`` table is created by
the runner itself (so it can record results), so this migration is essentially
a no-op marker that establishes "we are on the versioned migration system".

Future migrations (lots, lot_allocations, decision_snapshots, spp_snapshots,
personal_spp_snapshots, etc.) will be added as m002, m003, ...
"""
from __future__ import annotations

from typing import Any

VERSION = 1
NAME = "init_versioning"


async def up(conn: Any) -> None:
    # The schema_migrations table itself is created by apply_migrations() in
    # db.py before any migrations run, so nothing to do here. This file exists
    # purely to mark VERSION=1 in the registry.
    pass
