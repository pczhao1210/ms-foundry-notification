from unittest.mock import Mock

from azure.core.exceptions import ResourceNotFoundError
import pytest

from core.store import Store, decode_snapshot, encode_snapshot, from_entity, pick_latest, snapshot_name, to_entity

EVENT = {
    "id": "abc",
    "date": "2026-10-07",
    "source": "arm",
    "kind": "observed",
    "category": "lifecycle",
    "type": "STATUS_CHANGED",
    "model": {"format": "OpenAI", "name": "gpt-x", "version": "1"},
    "note": "模型",
}


def test_entity_round_trip():
    entity = to_entity(EVENT)
    assert (entity["PartitionKey"], entity["RowKey"]) == ("2026-10-07", "abc")
    assert (entity["source"], entity["kind"], entity["category"], entity["type"]) == (
        "arm",
        "observed",
        "lifecycle",
        "STATUS_CHANGED",
    )
    assert from_entity(entity) == EVENT


def test_large_event_is_split_across_properties():
    big = {**EVENT, "regions": [f"{i:08x}{i * 7919:08x}" for i in range(20_000)]}
    entity = to_entity(big)
    assert "body1" in entity
    assert all(len(entity[f"body{i}"]) <= 60_000 for i in range(2))
    assert from_entity(entity) == big


def test_snapshot_round_trip():
    data = {"collected_at": "2026-10-07T00:00:00Z", "models": {"a": {"name": "模型"}}}
    assert decode_snapshot(encode_snapshot(data)) == data
    assert encode_snapshot(data) == encode_snapshot(data)


def test_pick_latest():
    names = [
        "2026-10-05/arm.json.gz",
        "2026-10-07/arm.json.gz",
        "2026-10-06/prices.json.gz",
        "2026-10-06/arm.json.gz",
        "notes/arm.json.gz",
    ]
    assert pick_latest(names, "arm") == "2026-10-07/arm.json.gz"
    assert pick_latest(names, "arm", before="2026-10-07") == "2026-10-06/arm.json.gz"
    assert pick_latest(names, "prices", before="2026-10-06") is None
    assert pick_latest([], "docs") is None


def test_snapshot_name_rejects_unknown_kind():
    with pytest.raises(ValueError):
        snapshot_name("2026-10-07", "../x")


@pytest.fixture
def memory_store():
    blobs = {}
    blob_service, table_service, container = Mock(), Mock(), Mock()
    blob_service.get_container_client.return_value = container
    tables = {name: Mock() for name in ("events", "schedule")}
    table_service.get_table_client.side_effect = tables.__getitem__
    for table in tables.values():
        table.query_entities.return_value = []

    def upload(name, data, **_):
        blobs[name] = data

    def download(name):
        if name not in blobs:
            raise ResourceNotFoundError("blob not found")
        client = Mock()
        client.readall.return_value = blobs[name]
        return client

    container.upload_blob.side_effect = upload
    container.download_blob.side_effect = download
    container.list_blob_names.side_effect = lambda name_starts_with="": [
        name for name in blobs if name.startswith(name_starts_with)
    ]
    container.delete_blob.side_effect = blobs.pop
    store = Store(blob_service, table_service)
    return store, blobs, tables, container, (blob_service, table_service)


def test_pending_blob_survives_a_new_store_instance_and_replays_idempotently(memory_store):
    store, blobs, tables, _, services = memory_store
    snapshot = {"collected_at": "2026-10-07T00:00:00Z", "models": {}}
    tables["events"].submit_transaction.side_effect = RuntimeError("Table unavailable")
    with pytest.raises(RuntimeError, match="Table unavailable"):
        store.commit_run("2026-10-07", "arm", snapshot, [EVENT], [])
    assert set(blobs) == {"pending/2026-10-07/arm.json.gz"}
    assert store.latest_snapshot("arm") is None

    tables["events"].submit_transaction.side_effect = None
    recovered = Store(*services)
    recovered.recover_pending("arm")
    assert recovered.latest_snapshot("arm") == snapshot
    assert set(blobs) == {"2026-10-07/arm.json.gz", "latest/arm.json.gz"}
    [operation] = tables["events"].submit_transaction.call_args.args[0]
    assert from_entity(operation[1]) == EVENT
    count = tables["events"].submit_transaction.call_count
    recovered.recover_pending("arm")
    assert tables["events"].submit_transaction.call_count == count


def test_snapshot_index_avoids_listing_and_keeps_previous_day_on_rerun(memory_store):
    store, blobs, _, container, _ = memory_store
    store.save_snapshot("2026-10-06", "arm", {"value": "old"})
    store.save_snapshot("2026-10-07", "arm", {"value": "new"})
    store.save_snapshot("2026-10-07", "arm", {"value": "rerun"})
    container.list_blob_names.reset_mock()
    assert store.latest_snapshot("arm") == {"value": "rerun"}
    assert store.latest_snapshot("arm", before="2026-10-07") == {"value": "old"}
    container.list_blob_names.assert_not_called()
    assert decode_snapshot(blobs["latest/arm.json.gz"])["previous"] == "2026-10-06/arm.json.gz"


def test_snapshot_index_migrates_old_layout_and_allows_older_history(memory_store):
    store, blobs, _, _, _ = memory_store
    blobs["2026-10-01/arm.json.gz"] = encode_snapshot({"value": 1})
    blobs["2026-10-05/arm.json.gz"] = encode_snapshot({"value": 5})
    assert store.latest_snapshot("arm") == {"value": 5}
    store.save_snapshot("2026-10-07", "arm", {"value": 7})
    assert store.latest_snapshot("arm", before="2026-10-07") == {"value": 5}
    assert store.latest_snapshot("arm", before="2026-10-05") == {"value": 1}
    store.save_snapshot("2026-10-02", "arm", {"value": 2})
    assert store.latest_snapshot("arm") == {"value": 7}
    store.save_snapshot("2026-10-06", "arm", {"value": 6})
    assert store.latest_snapshot("arm", before="2026-10-07") == {"value": 6}


def test_event_filters_are_pushed_down_and_upcoming_skips_observed_table(memory_store):
    store, _, tables, _, _ = memory_store
    assert store.read_events("2026-10-08", "2026-10-14", category="price", scheduled_only=True) == []
    tables["events"].query_entities.assert_not_called()
    call = tables["schedule"].query_entities.call_args
    assert "category eq @category" in call.args[0]
    assert call.kwargs["parameters"] == {"start": "2026-10-08", "end": "2026-10-14", "category": "price"}


def test_schedule_reconciliation_bounds_history_without_losing_future_deletions(memory_store):
    store, _, tables, _, _ = memory_store
    store.commit_run("2026-10-07", "arm", {"models": {}}, [], [])
    call = tables["schedule"].query_entities.call_args
    assert "PartitionKey ge @start" in call.args[0]
    assert call.kwargs["parameters"]["start"] == "2026-10-08"
    store.commit_run("2026-10-07", "arm", {"models": {}}, [], [{**EVENT, "date": "2026-10-01"}])
    assert tables["schedule"].query_entities.call_args.kwargs["parameters"]["start"] == "2026-10-01"


def test_source_status_is_archived_and_missing_status_is_unknown(memory_store):
    store, blobs, _, _, _ = memory_store
    assert store.source_status("arm") is None
    status = {"status": "failed", "last_attempt_at": "2026-10-06T16:00:00Z"}
    store.save_source_status("arm", status)
    assert store.source_status("arm") == status
    assert decode_snapshot(blobs["runs/2026-10-07/arm.json.gz"]) == status
