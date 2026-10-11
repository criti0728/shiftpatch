"""Firestore persistence for the same sample-store/session contract as SQLite.

Only the server accesses Firestore. A store's metadata and compressed payload
chunks are read and written in one transaction; callbacks must have no external
side effects because Firestore can retry them after a concurrent write.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
import zlib

from .errors import DomainError
from .repository import sample_state

CHUNK_BYTES = 700_000
MAX_CHUNKS = 8
MAX_RAW_BYTES = 32_000_000
GUEST_RETENTION_DAYS = 30


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise DomainError("not_found", "店舗が見つかりません。", 404)
    return value


def _encode(state: dict) -> list[bytes]:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_RAW_BYTES:
        raise DomainError("sample_full", "サンプルの保存上限です。初期化して体験を続けてください。", 413)
    compressed = zlib.compress(raw)
    chunks = [compressed[i:i + CHUNK_BYTES] for i in range(0, len(compressed), CHUNK_BYTES)]
    if len(chunks) > MAX_CHUNKS:
        raise DomainError("sample_full", "サンプルの保存上限です。初期化して体験を続けてください。", 413)
    return chunks


def _decode(chunks: list[bytes]) -> dict:
    decoder = zlib.decompressobj()
    raw = decoder.decompress(b"".join(chunks), MAX_RAW_BYTES + 1)
    if len(raw) > MAX_RAW_BYTES or decoder.unconsumed_tail or not decoder.eof:
        raise RuntimeError("Invalid or oversized stored sample payload")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Invalid sample payload")
    return value


class FirestoreRepository:
    storage_name = "firestore"

    def __init__(self, client):
        self.client = client
        self.stores = client.collection("shiftpatch_stores")
        self.chunks = client.collection("shiftpatch_store_chunks")

    def _ref(self, store_id):
        return self.stores.document(_identifier(store_id))

    def _chunk_ref(self, store_id, index):
        return self.chunks.document(f"{_identifier(store_id)}_{index}")

    def create_store(self, store_id, owner_id, initial_state=None):
        from google.cloud import firestore
        ref = self._ref(store_id)

        @firestore.transactional
        def create(transaction):
            existing = ref.get(transaction=transaction)
            if existing.exists:
                value = existing.to_dict()
                if value["owner_id"] != owner_id:
                    raise DomainError("not_found", "店舗が見つかりません。", 404)
                return {"id": store_id, "name": value["name"], "owner_id": owner_id, "created": False}
            state = sample_state(store_id, initial_state)
            payload = _encode(state)
            metadata = {"name": state["store"]["name"], "owner_id": owner_id,
                        "chunks": len(payload), "created_at": datetime.now(timezone.utc)}
            if owner_id.startswith("guest-"):
                metadata["expire_at"] = datetime.now(timezone.utc) + timedelta(days=GUEST_RETENTION_DAYS)
            transaction.set(ref, metadata)
            for index, data in enumerate(payload):
                item = {"data": data, "store_id": store_id}
                if "expire_at" in metadata:
                    item["expire_at"] = metadata["expire_at"]
                transaction.set(self._chunk_ref(store_id, index), item)
            return {"id": store_id, "name": metadata["name"], "owner_id": owner_id, "created": True}

        return create(self.client.transaction(max_attempts=5))

    def get_store(self, store_id):
        snapshot = self._ref(store_id).get()
        if not snapshot.exists:
            return None
        value = snapshot.to_dict()
        if value.get("expire_at") and value["expire_at"] <= datetime.now(timezone.utc):
            return None
        return {"id": store_id, "name": value["name"], "owner_id": value["owner_id"]}

    def for_store(self, store_id):
        return FirestoreStore(self, _identifier(store_id))

    def touch_store(self, store_id):
        """Refresh a returning guest's lease; flat chunks each receive TTL too."""
        from google.cloud import firestore

        @firestore.transactional
        def touch(transaction):
            ref = self._ref(store_id)
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                raise DomainError("not_found", "店舗が見つかりません。", 404)
            metadata = snapshot.to_dict()
            if not metadata["owner_id"].startswith("guest-"):
                return
            if metadata.get("expire_at") and metadata["expire_at"] <= datetime.now(timezone.utc):
                raise DomainError("not_found", "体験の保存期間が終了しました。新しいサンプルを開いてください。", 404)
            expires = datetime.now(timezone.utc) + timedelta(days=GUEST_RETENTION_DAYS)
            metadata["expire_at"] = expires
            transaction.set(ref, metadata)
            for index in range(metadata["chunks"]):
                # update preserves the compressed payload and fails if absent.
                transaction.update(self._chunk_ref(store_id, index), {"expire_at": expires})

        return touch(self.client.transaction(max_attempts=5))


class FirestoreStore:
    def __init__(self, repository, store_id):
        self.repository = repository
        self.store_id = store_id

    def _load(self, transaction):
        repository = self.repository
        snapshot = repository._ref(self.store_id).get(transaction=transaction)
        if not snapshot.exists:
            raise DomainError("not_found", "店舗が見つかりません。", 404)
        metadata = snapshot.to_dict()
        if metadata.get("expire_at") and metadata["expire_at"] <= datetime.now(timezone.utc):
            raise DomainError("not_found", "体験の保存期間が終了しました。新しいサンプルを開いてください。", 404)
        count = metadata.get("chunks", 0)
        if not isinstance(count, int) or not 1 <= count <= MAX_CHUNKS:
            raise RuntimeError("Invalid sample chunk metadata")
        chunks = []
        for index in range(count):
            item = repository._chunk_ref(self.store_id, index).get(transaction=transaction)
            if not item.exists:
                raise RuntimeError("Missing sample payload chunk")
            chunks.append(item.to_dict()["data"])
        return metadata, _decode(chunks)

    def read(self):
        from google.cloud import firestore

        @firestore.transactional
        def read(transaction):
            return self._load(transaction)[1]

        return read(self.repository.client.transaction(max_attempts=5))

    def transact(self, operation):
        from google.cloud import firestore
        repository = self.repository

        @firestore.transactional
        def apply(transaction):
            metadata, state = self._load(transaction)
            result = operation(state)
            payload = _encode(state)
            old_count = metadata["chunks"]
            metadata.update(chunks=len(payload), name=state["store"]["name"], updated_at=datetime.now(timezone.utc))
            if metadata["owner_id"].startswith("guest-"):
                metadata["expire_at"] = datetime.now(timezone.utc) + timedelta(days=GUEST_RETENTION_DAYS)
            transaction.set(repository._ref(self.store_id), metadata)
            for index, data in enumerate(payload):
                item = {"data": data, "store_id": self.store_id}
                if "expire_at" in metadata:
                    item["expire_at"] = metadata["expire_at"]
                transaction.set(repository._chunk_ref(self.store_id, index), item)
            for index in range(len(payload), old_count):
                transaction.delete(repository._chunk_ref(self.store_id, index))
            return result

        return apply(repository.client.transaction(max_attempts=5))

    def reset(self):
        def replace(state):
            revision = state["revision"] + 1
            state.clear()
            state.update(sample_state(self.store_id))
            state["revision"] = revision
            return state
        return self.transact(replace)


class FirestoreIdentityRepository:
    def __init__(self, client):
        self.client = client
        self.sessions = client.collection("shiftpatch_sessions")
        self.quotas = client.collection("shiftpatch_quotas")

    def put_session(self, token_hash, record):
        value = deepcopy(record)
        value["expire_at"] = datetime.fromtimestamp(record["expires_at"], timezone.utc)
        self.sessions.document(_identifier(token_hash)).set(value)

    def get_session(self, token_hash):
        snapshot = self.sessions.document(_identifier(token_hash)).get()
        if not snapshot.exists:
            return None
        value = snapshot.to_dict()
        value.pop("expire_at", None)
        return value

    def delete_session(self, token_hash):
        self.sessions.document(_identifier(token_hash)).delete()

    def consume_quota(self, key, limit, window_seconds, now=None):
        from google.cloud import firestore
        current = time.time() if now is None else now
        digest = hashlib.sha256(key.encode()).hexdigest()
        ref = self.quotas.document(digest)

        @firestore.transactional
        def consume(transaction):
            snapshot = ref.get(transaction=transaction)
            record = snapshot.to_dict() if snapshot.exists else {}
            active = record.get("expires_at", 0) > current
            used = record.get("used", 0) if active else 0
            expires_at = record["expires_at"] if active else current + window_seconds
            if used >= limit:
                return False
            transaction.set(ref, {"used": used + 1, "expires_at": expires_at,
                                  "expire_at": datetime.fromtimestamp(expires_at, timezone.utc)})
            return True

        return consume(self.client.transaction(max_attempts=5))


def create_repositories(project_id):
    from google.cloud import firestore
    client = firestore.Client(project=project_id)
    return FirestoreRepository(client), FirestoreIdentityRepository(client)
