"""Persistence contract tests with an in-memory Firestore transaction double.

The double checks read-before-write and atomic commit/retry behavior. These tests
do not contact Firestore or replace a deployment smoke test with its real SDK.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import sys
from types import ModuleType, SimpleNamespace
import zlib

import pytest

from app import cloud_repository as cloud
from app.errors import DomainError
from app.seed import seed_state


class Snapshot:
    def __init__(self, value):
        self.exists = value is not None
        self.value = deepcopy(value)

    def to_dict(self):
        return deepcopy(self.value)


class Reference:
    def __init__(self, client, key):
        self.client, self.key = client, key

    def get(self, transaction=None):
        if transaction:
            assert not transaction.writes, "Firestore reads must precede writes"
        return Snapshot(self.client.documents.get(self.key))

    def set(self, value):
        self.client.documents[self.key] = deepcopy(value)

    def delete(self):
        self.client.documents.pop(self.key, None)


class Transaction:
    def __init__(self, client, max_attempts):
        self.client, self.max_attempts = client, max_attempts
        self.writes = []

    def set(self, reference, value):
        self.writes.append(("set", reference.key, deepcopy(value)))

    def update(self, reference, value):
        self.writes.append(("update", reference.key, deepcopy(value)))

    def delete(self, reference):
        self.writes.append(("delete", reference.key, None))

    def commit(self):
        documents = deepcopy(self.client.documents)
        for operation, key, value in self.writes:
            if operation == "delete":
                documents.pop(key, None)
            elif operation == "update":
                if key not in documents:
                    raise KeyError("Cannot update a missing document")
                documents[key].update(value)
            else:
                documents[key] = value
        self.client.documents = documents


class Client:
    def __init__(self):
        self.documents = {}
        self.retry_next = False

    def collection(self, name):
        return SimpleNamespace(document=lambda identifier: Reference(self, (name, identifier)))

    def transaction(self, max_attempts):
        return Transaction(self, max_attempts)


@pytest.fixture
def client(monkeypatch):
    def transactional(operation):
        @wraps(operation)
        def run(transaction):
            for _ in range(transaction.max_attempts):
                transaction.writes.clear()
                result = operation(transaction)
                if transaction.client.retry_next:
                    transaction.client.retry_next = False
                    continue
                transaction.commit()
                return result
            raise RuntimeError("Transaction attempts exhausted")
        return run

    package = ModuleType("google.cloud")
    package.firestore = SimpleNamespace(transactional=transactional)
    monkeypatch.setitem(sys.modules, "google.cloud", package)
    return Client()


@pytest.fixture
def cloud_clock(monkeypatch):
    clock = SimpleNamespace(current=datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current.astimezone(tz) if tz else clock.current.replace(tzinfo=None)

    monkeypatch.setattr(cloud, "datetime", Clock)
    return clock


def test_codec_round_trip_and_chunk_limits(monkeypatch):
    monkeypatch.setattr(cloud, "CHUNK_BYTES", 32)
    monkeypatch.setattr(cloud, "MAX_CHUNKS", 1000)
    state = seed_state()
    chunks = cloud._encode(state)
    assert len(chunks) > 1
    assert all(len(chunk) <= 32 for chunk in chunks)
    assert cloud._decode(chunks) == state
    monkeypatch.setattr(cloud, "MAX_CHUNKS", 1)
    with pytest.raises(DomainError) as error:
        cloud._encode(state)
    assert error.value.code == "sample_full"


def test_codec_rejects_truncated_and_oversized_payload(monkeypatch):
    chunks = cloud._encode({"value": "x" * 1000})
    with pytest.raises(RuntimeError):
        cloud._decode([b"".join(chunks)[:-2]])
    monkeypatch.setattr(cloud, "MAX_RAW_BYTES", 50)
    with pytest.raises(RuntimeError):
        cloud._decode(chunks)
    with pytest.raises(DomainError) as error:
        cloud._encode({"value": "x" * 1000})
    assert error.value.status == 413
    with pytest.raises(RuntimeError):
        cloud._decode([zlib.compress(b"[]")])


def test_store_isolation_ownership_and_idempotent_creation(client):
    repository = cloud.FirestoreRepository(client)
    assert repository.create_store("store_a", "alice")["created"]
    assert repository.create_store("store_b", "bob")["created"]
    assert not repository.create_store("store_a", "alice")["created"]
    with pytest.raises(DomainError) as error:
        repository.create_store("store_a", "bob")
    assert error.value.status == 404
    first = repository.for_store("store_a")
    first.transact(lambda state: state["store"].update(name="Alice's sample"))
    assert repository.get_store("store_a")["name"] == "Alice's sample"
    assert repository.for_store("store_b").read()["store"]["name"] != "Alice's sample"
    detached = first.read()
    detached["store"]["name"] = "Uncommitted"
    assert first.read()["store"]["name"] == "Alice's sample"
    assert repository.get_store("missing") is None
    with pytest.raises(DomainError):
        repository.for_store("store_a/other")


def test_store_callback_failure_rolls_back_and_retry_applies_once(client):
    repository = cloud.FirestoreRepository(client)
    repository.create_store("store", "alice")
    store = repository.for_store("store")
    before = store.read()

    def rejected(state):
        state["revision"] += 100
        raise DomainError("rejected", "Rejected")

    with pytest.raises(DomainError):
        store.transact(rejected)
    assert store.read() == before
    attempts = []

    def increment(state):
        attempts.append(state["revision"])
        state["revision"] += 1
        return state["revision"]

    client.retry_next = True
    assert store.transact(increment) == before["revision"] + 1
    assert attempts == [before["revision"], before["revision"]]
    assert store.read()["revision"] == before["revision"] + 1


def test_payload_shrink_removes_obsolete_chunks_and_reset_preserves_id(client, monkeypatch):
    monkeypatch.setattr(cloud, "CHUNK_BYTES", 512)
    monkeypatch.setattr(cloud, "MAX_CHUNKS", 100)
    repository = cloud.FirestoreRepository(client)
    state = seed_state()
    state["extra"] = list(range(2000))
    repository.create_store("store", "alice", state)
    store = repository.for_store("store")
    old_chunks = repository._ref("store").get().to_dict()["chunks"]
    store.transact(lambda value: value.pop("extra"))
    new_chunks = repository._ref("store").get().to_dict()["chunks"]
    assert new_chunks < old_chunks
    assert all(not repository._chunk_ref("store", index).get().exists for index in range(new_chunks, old_chunks))
    revision = store.read()["revision"]
    reset = store.reset()
    assert reset["store"]["id"] == "store"
    assert reset["revision"] == revision + 1
    assert repository.get_store("store")["owner_id"] == "alice"


def test_imported_sample_does_not_copy_in_flight_runs_or_request_keys(client):
    repository = cloud.FirestoreRepository(client)
    state = seed_state()
    state["agent_runs"] = {"pending": {"key": "request_key"}}
    state["idempotency"] = {"request_key": {"result": {"store_id": "guest_store"}}}
    repository.create_store("account_store", "account", state)
    stored = repository.for_store("account_store").read()
    assert stored["agent_runs"] == {}
    assert stored["idempotency"] == {}
    assert state["agent_runs"]  # Creating a copy must not mutate the source.


def test_identity_sessions_and_atomic_quota_window(client):
    identity = cloud.FirestoreIdentityRepository(client)
    record = {"expires_at": 150.0, "scope": "session", "store_id": "store"}
    identity.put_session("token_hash", record)
    assert identity.get_session("token_hash") == record
    assert "expire_at" not in record
    identity.delete_session("token_hash")
    assert identity.get_session("token_hash") is None
    client.retry_next = True
    assert identity.consume_quota("ai-store", 2, 60, now=100)
    assert identity.consume_quota("ai-store", 2, 60, now=101)
    assert not identity.consume_quota("ai-store", 2, 60, now=102)
    assert identity.consume_quota("another-store", 2, 60, now=102)
    assert not identity.consume_quota("ai-store", 2, 60, now=159)
    assert identity.consume_quota("ai-store", 2, 60, now=160)


def test_guest_retention_keeps_chunks_in_sync_on_resume_and_write(client, cloud_clock):
    repository = cloud.FirestoreRepository(client)
    repository.create_store("guest_store", "guest-alice")
    store = repository.for_store("guest_store")
    before = store.read()
    first_metadata = repository._ref("guest_store").get().to_dict()
    assert first_metadata["expire_at"] == cloud_clock.current + timedelta(days=30)
    chunk_data = [repository._chunk_ref("guest_store", index).get().to_dict()["data"]
                  for index in range(first_metadata["chunks"])]

    cloud_clock.current += timedelta(days=3)
    repository.touch_store("guest_store")
    resumed = repository._ref("guest_store").get().to_dict()
    assert resumed["expire_at"] == cloud_clock.current + timedelta(days=30)
    assert store.read() == before
    for index, data in enumerate(chunk_data):
        chunk = repository._chunk_ref("guest_store", index).get().to_dict()
        assert chunk["data"] == data
        assert chunk["expire_at"] == resumed["expire_at"]

    cloud_clock.current += timedelta(days=1)
    store.transact(lambda state: state["store"].update(name="Edited guest"))
    updated = repository._ref("guest_store").get().to_dict()
    assert updated["expire_at"] == cloud_clock.current + timedelta(days=30)
    assert store.read()["store"]["name"] == "Edited guest"
    for index in range(updated["chunks"]):
        assert repository._chunk_ref("guest_store", index).get().to_dict()["expire_at"] == updated["expire_at"]


def test_account_store_has_no_expiration_and_touch_does_not_change_it(client, cloud_clock):
    repository = cloud.FirestoreRepository(client)
    repository.create_store("account_store", "firebase-alice")
    before = deepcopy(client.documents)
    assert all("expire_at" not in item for item in before.values())
    cloud_clock.current += timedelta(days=365)
    repository.touch_store("account_store")
    assert client.documents == before
    assert repository.get_store("account_store") is not None
    repository.for_store("account_store").transact(lambda state: state["store"].update(name="Account"))
    assert all("expire_at" not in item for item in client.documents.values())


def test_expired_guest_is_unavailable_before_ttl_deletion(client, cloud_clock):
    repository = cloud.FirestoreRepository(client)
    repository.create_store("guest_store", "guest-alice")
    cloud_clock.current += timedelta(days=30)
    before = deepcopy(client.documents)
    assert repository.get_store("guest_store") is None
    with pytest.raises(DomainError) as error:
        repository.for_store("guest_store").read()
    assert error.value.status == 404
    with pytest.raises(DomainError):
        repository.for_store("guest_store").transact(lambda state: state["store"].update(name="Expired"))
    with pytest.raises(DomainError) as error:
        repository.touch_store("guest_store")
    assert error.value.status == 404
    assert client.documents == before


def test_guest_resume_with_missing_chunk_does_not_partially_refresh_lease(client, cloud_clock):
    repository = cloud.FirestoreRepository(client)
    repository.create_store("guest_store", "guest-alice")
    repository._chunk_ref("guest_store", 0).delete()
    before = deepcopy(client.documents)
    cloud_clock.current += timedelta(days=2)
    with pytest.raises(KeyError):
        repository.touch_store("guest_store")
    assert client.documents == before


def test_installed_sdk_serializes_chunks_and_retries_aborted_commit():
    """Exercise actual SDK transactions/protobufs; replace only network RPCs."""
    firestore = pytest.importorskip("google.cloud.firestore")
    from datetime import datetime, timezone
    from google.api_core.exceptions import Aborted
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import types

    class Rpc:
        def __init__(self):
            self.documents = {}
            self.retry_next = False
            self.commits = 0

        def begin_transaction(self, **kwargs):
            return types.BeginTransactionResponse(transaction=b"contract-transaction")

        def batch_get_documents(self, request, **kwargs):
            for name in request["documents"]:
                if name in self.documents:
                    yield types.BatchGetDocumentsResponse(found=self.documents[name])
                else:
                    yield types.BatchGetDocumentsResponse(missing=name)

        def commit(self, request, **kwargs):
            self.commits += 1
            if self.retry_next:
                self.retry_next = False
                raise Aborted("Simulated concurrent write")
            for write in request["writes"]:
                if write.update.name:
                    if write.update_mask.field_paths:
                        document = deepcopy(self.documents[write.update.name])
                        for path in write.update_mask.field_paths:
                            document.fields[path] = deepcopy(write.update.fields[path])
                        self.documents[write.update.name] = document
                    else:
                        self.documents[write.update.name] = deepcopy(write.update)
                elif write.delete:
                    self.documents.pop(write.delete, None)
            return types.CommitResponse(commit_time=datetime.now(timezone.utc))

        def rollback(self, **kwargs):
            return None

    client = firestore.Client(project="shiftpatch-contract-test", credentials=AnonymousCredentials())
    rpc = Rpc()
    client._firestore_api_internal = rpc
    repository = cloud.FirestoreRepository(client)
    repository.create_store("store", "alice")
    store = repository.for_store("store")
    before = store.read()
    attempts = []

    def increment(state):
        attempts.append(state["revision"])
        state["revision"] += 1

    rpc.retry_next = True
    store.transact(increment)
    assert attempts == [before["revision"], before["revision"]]
    assert store.read()["revision"] == before["revision"] + 1
    assert any(document.fields.get("data") and document.fields["data"].bytes_value for document in rpc.documents.values())
    repository.create_store("guest_store", "guest-alice")
    guest_before = repository.for_store("guest_store").read()
    chunk_before = repository._chunk_ref("guest_store", 0).get().to_dict()
    repository.touch_store("guest_store")
    chunk_after = repository._chunk_ref("guest_store", 0).get().to_dict()
    assert chunk_after["data"] == chunk_before["data"]
    assert chunk_after["expire_at"] >= chunk_before["expire_at"]
    assert repository.for_store("guest_store").read() == guest_before
