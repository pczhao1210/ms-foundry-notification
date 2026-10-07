import pytest

from core.store import decode_snapshot, encode_snapshot, from_entity, pick_latest, snapshot_name, to_entity

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
