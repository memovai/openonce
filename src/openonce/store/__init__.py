from typing import Any

from .base import Store
from .memory import InMemoryStore
from .sqlite import SQLiteStore

__all__ = ["InMemoryStore", "PostgresStore", "SQLiteStore", "Store"]


def __getattr__(name: str) -> Any:
    # Lazy: keep the core import stdlib-only; psycopg loads only when asked for.
    if name == "PostgresStore":
        from .postgres import PostgresStore

        return PostgresStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
