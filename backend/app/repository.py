"""Transactional sample stores and durable identities for local development."""
from copy import deepcopy
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar

from .errors import DomainError
from .seed import seed_state

T = TypeVar("T")


def sample_state(store_id: str, initial_state: dict | None = None) -> dict:
    state = deepcopy(initial_state) if initial_state else seed_state()
    state["store"]["id"] = store_id
    state["agent_runs"] = {}
    state["idempotency"] = {}
    return state


class SQLiteRepository:
    storage_name = "sqlite"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS store_state (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS store_owners (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO store_state VALUES (?, ?)", ("demo", json.dumps(seed_state(), ensure_ascii=False)))

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def create_store(self, store_id: str, owner_id: str, initial_state: dict | None = None) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT owner_id, name FROM store_owners WHERE id=?", (store_id,)).fetchone()
            if existing:
                if existing[0] != owner_id:
                    raise DomainError("not_found", "店舗が見つかりません。", 404)
                return {"id": store_id, "owner_id": owner_id, "name": existing[1], "created": False}
            state = sample_state(store_id, initial_state)
            name = state["store"]["name"]
            connection.execute("INSERT INTO store_owners VALUES (?, ?, ?)", (store_id, owner_id, name))
            connection.execute("INSERT INTO store_state VALUES (?, ?)", (store_id, json.dumps(state, ensure_ascii=False)))
            return {"id": store_id, "owner_id": owner_id, "name": name, "created": True}

    def get_store(self, store_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT owner_id, name FROM store_owners WHERE id=?", (store_id,)).fetchone()
            return {"id": store_id, "owner_id": row[0], "name": row[1]} if row else None

    def for_store(self, store_id: str):
        return SQLiteScopedRepository(self, store_id)

    # Engine tests and explicit local maintenance; never exposed by API.
    def read(self) -> dict:
        return self.for_store("demo").read()

    def transact(self, operation: Callable[[dict], T]) -> T:
        return self.for_store("demo").transact(operation)

    def reset(self):
        return self.for_store("demo").reset()


class SQLiteScopedRepository:
    storage_name = "sqlite"

    def __init__(self, root: SQLiteRepository, store_id: str):
        self.root, self.store_id, self.path = root, store_id, root.path

    def read(self) -> dict:
        with self.root._connect() as connection:
            row = connection.execute("SELECT payload FROM store_state WHERE id=?", (self.store_id,)).fetchone()
            if not row:
                raise DomainError("not_found", "店舗が見つかりません。", 404)
            return json.loads(row[0])

    def transact(self, operation: Callable[[dict], T]) -> T:
        with self.root._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM store_state WHERE id=?", (self.store_id,)).fetchone()
            if not row:
                raise DomainError("not_found", "店舗が見つかりません。", 404)
            state = json.loads(row[0])
            result = operation(state)
            connection.execute("UPDATE store_state SET payload=? WHERE id=?", (json.dumps(state, ensure_ascii=False), self.store_id))
            return result

    def reset(self):
        def replace(state):
            revision = state["revision"] + 1
            state.clear()
            state.update(sample_state(self.store_id))
            state["revision"] = revision
            return state
        return self.transact(replace)


class SQLiteIdentityRepository:
    """Token hashes and quotas outside resettable sample data."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS quotas (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL)")

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def put_session(self, token_hash: str, record: dict):
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (token_hash, json.dumps(record, ensure_ascii=False)))

    def get_session(self, token_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            return json.loads(row[0]) if row else None

    def delete_session(self, token_hash: str):
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def consume_quota(self, key: str, limit: int, window_seconds: int, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT count, expires_at FROM quotas WHERE key=?", (key,)).fetchone()
            count, expires_at = row if row and row[1] > timestamp else (0, timestamp + window_seconds)
            if count >= limit:
                return False
            connection.execute("INSERT OR REPLACE INTO quotas VALUES (?, ?, ?)", (key, count + 1, expires_at))
            return True
