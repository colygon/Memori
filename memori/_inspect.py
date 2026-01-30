r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

import os
import sqlite3
import sys
from typing import Any

import psycopg

from memori._cli import Cli
from memori._config import Config
from memori.storage import Manager as StorageManager

PLACEHOLDER_BY_DIALECT = {
    "sqlite": "?",
    "postgresql": "%s",
    "cockroachdb": "%s",
    "mysql": "%s",
    "oceanbase": "%s",
}


class Manager:
    def __init__(self, config: Config):
        self.config = config

    def execute(self):
        cli = Cli(self.config)
        args = sys.argv[2:]
        if not args:
            self.usage()
            cli.newline()
            return self

        options = self._parse_args(args)
        if options is None:
            self.usage()
            cli.newline()
            return self

        conn = self._resolve_connection(options)
        if conn is None:
            cli.notice(
                "No connection found. Provide --sqlite <path> or --db <conn_string> "
                "or set MEMORI_COCKROACHDB_CONNECTION_STRING."
            )
            cli.newline()
            return self

        storage = StorageManager(self.config).start(conn)
        if storage.adapter is None or storage.driver is None:
            cli.notice("Storage is not configured. Ensure your DB connection is valid.")
            cli.newline()
            return self

        dialect = storage.adapter.get_dialect()
        placeholder = PLACEHOLDER_BY_DIALECT.get(dialect)
        if placeholder is None:
            cli.notice(f"Inspect is not supported for dialect: {dialect}")
            cli.newline()
            return self

        entity_id = options.get("entity_id")
        external_id = options.get("external_id")

        if entity_id is None and external_id is None:
            cli.notice("Provide an entity ID or external ID.")
            cli.newline()
            return self

        if entity_id is None:
            entity_id = self._resolve_entity_id(
                storage.adapter, placeholder, external_id
            )
            if entity_id is None:
                cli.notice(f"No entity found for external_id: {external_id}")
                cli.newline()
                return self

        if external_id is None:
            external_id = self._resolve_external_id(
                storage.adapter, placeholder, entity_id
            )

        stats = self._fetch_entity_stats(storage.adapter, placeholder, entity_id)
        top_facts = self._fetch_top_facts(
            storage.adapter, placeholder, entity_id, options["limit"]
        )

        cli.notice("Memori Inspect")
        cli.notice(f"Entity ID: {entity_id}", 1)
        if external_id is not None:
            cli.notice(f"External ID: {external_id}", 1)

        cli.notice("", 1)
        cli.notice(f"Facts: {stats['fact_count']}", 1)
        cli.notice(f"Oldest fact: {stats['oldest']}", 1)
        cli.notice(f"Newest fact: {stats['newest']}", 1)

        if top_facts:
            cli.notice("", 1)
            cli.notice("Top facts (by frequency):", 1)
            for idx, row in enumerate(top_facts, start=1):
                content = row.get("content", "")
                num_times = row.get("num_times", 0)
                last_time = row.get("date_last_time", "")
                cli.notice(f"{idx}. {content}", 2)
                cli.notice(f"Seen: {num_times}  Last: {last_time}", 3)

        cli.newline()
        return self

    def _parse_args(self, args: list[str]) -> dict[str, Any] | None:
        options: dict[str, Any] = {
            "limit": 5,
            "sqlite_path": None,
            "db_url": None,
            "entity_id": None,
            "external_id": None,
        }

        i = 0
        while i < len(args):
            token = args[i]
            if token in {"--sqlite"}:
                if i + 1 >= len(args):
                    return None
                options["sqlite_path"] = args[i + 1]
                i += 2
                continue
            if token in {"--db"}:
                if i + 1 >= len(args):
                    return None
                options["db_url"] = args[i + 1]
                i += 2
                continue
            if token in {"--entity-id", "--id"}:
                if i + 1 >= len(args):
                    return None
                try:
                    options["entity_id"] = int(args[i + 1])
                except ValueError:
                    return None
                i += 2
                continue
            if token in {"--limit"}:
                if i + 1 >= len(args):
                    return None
                try:
                    options["limit"] = int(args[i + 1])
                except ValueError:
                    return None
                i += 2
                continue

            if options["external_id"] is None:
                options["external_id"] = token
                i += 1
                continue

            return None

        return options

    def _resolve_connection(self, options: dict[str, Any]):
        if options.get("sqlite_path"):
            return sqlite3.connect(options["sqlite_path"])

        db_url = options.get("db_url") or os.environ.get(
            "MEMORI_COCKROACHDB_CONNECTION_STRING"
        )
        if db_url:
            return psycopg.connect(db_url)

        return None

    def _resolve_entity_id(self, adapter, placeholder: str, external_id: str | None):
        if external_id is None:
            return None
        query = f"SELECT id FROM memori_entity WHERE external_id = {placeholder}"
        row = self._fetch_one(adapter, query, (external_id,))
        if row is None:
            return None
        return row.get("id")

    def _resolve_external_id(self, adapter, placeholder: str, entity_id: int):
        query = f"SELECT external_id FROM memori_entity WHERE id = {placeholder}"
        row = self._fetch_one(adapter, query, (entity_id,))
        if row is None:
            return None
        return row.get("external_id")

    def _fetch_entity_stats(self, adapter, placeholder: str, entity_id: int) -> dict:
        query = (
            "SELECT COUNT(*) AS fact_count, "
            "MIN(date_created) AS oldest, "
            "MAX(date_last_time) AS newest "
            f"FROM memori_entity_fact WHERE entity_id = {placeholder}"
        )
        row = self._fetch_one(adapter, query, (entity_id,)) or {}
        return {
            "fact_count": int(row.get("fact_count") or 0),
            "oldest": row.get("oldest") or "n/a",
            "newest": row.get("newest") or "n/a",
        }

    def _fetch_top_facts(
        self, adapter, placeholder: str, entity_id: int, limit: int
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT content, num_times, date_last_time "
            "FROM memori_entity_fact "
            f"WHERE entity_id = {placeholder} "
            "ORDER BY num_times DESC, date_last_time DESC "
            f"LIMIT {placeholder}"
        )
        return self._fetch_all(adapter, query, (entity_id, limit))

    def _fetch_one(self, adapter, query: str, binds: tuple) -> dict | None:
        result = adapter.execute(query, binds)
        if result is None:
            return None

        mappings = result.mappings() if hasattr(result, "mappings") else result
        if hasattr(mappings, "fetchone"):
            row = mappings.fetchone()
        else:
            row = None

        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return dict(row)

    def _fetch_all(self, adapter, query: str, binds: tuple) -> list[dict[str, Any]]:
        result = adapter.execute(query, binds)
        if result is None:
            return []

        mappings = result.mappings() if hasattr(result, "mappings") else result
        if hasattr(mappings, "fetchall"):
            rows = mappings.fetchall() or []
        else:
            rows = []

        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                normalized.append(row)
            else:
                normalized.append(dict(row))
        return normalized

    def usage(self):
        print(
            "usage: python -m memori inspect <external_id> [--limit N] "
            "[--sqlite <path>] [--db <conn_string>] [--entity-id <id>]"
        )
