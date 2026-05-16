"""Versioned schema migrations.

Add new migration files as ``mNNN_<short_name>.py`` and append the module to
``MIGRATIONS`` below. Migrations run in the order listed.

Each migration module must expose:
    VERSION: int  - monotonic, unique, never re-used
    NAME: str     - short human-readable identifier
    async def up(conn): ...  - applies the change (idempotent if possible)

The runner in ``app/storage/db.py::apply_migrations`` records applied versions
in the ``schema_migrations`` table and skips ones already applied.
"""
from __future__ import annotations

from . import (
    m001_init_versioning,
    m002_lot_ledger,
    m003_decision_snapshots,
    m004_personal_spp_snapshots,
    m005_missed_deal_tags,
    m006_stock_arrival_tracking,
)

MIGRATIONS = [
    m001_init_versioning,
    m002_lot_ledger,
    m003_decision_snapshots,
    m004_personal_spp_snapshots,
    m005_missed_deal_tags,
    m006_stock_arrival_tracking,
]
