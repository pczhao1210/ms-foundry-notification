"""Blob snapshots + Table events, accessed with Entra ID (shared keys are disabled on the account).

Layout:
- Blob `snapshots/{YYYY-MM-DD}/{kind}.json.gz`, kind in SNAPSHOT_KINDS, date = Asia/Shanghai collection day.
- Table `events` (observed) and `schedule` (scheduled): PartitionKey = event date, RowKey = event id.
"""
from __future__ import annotations

import gzip
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .events import OBSERVED, canonical_json

SNAPSHOT_CONTAINER = "snapshots"
EVENTS_TABLE = "events"
SCHEDULE_TABLE = "schedule"
SNAPSHOT_KINDS = ("arm", "docs", "prices")

_BLOB_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})/([a-z]+)\.json\.gz$")
# Table properties are limited to 64 KiB each and 1 MiB per entity.
_CHUNK = 60_000
_MAX_CHUNKS = 16
_BATCH = 100
_EMULATOR = "UseDevelopmentStorage=true"


def snapshot_name(day: str, kind: str) -> str:
    if kind not in SNAPSHOT_KINDS:
        raise ValueError(f"unknown snapshot kind: {kind}")
    return f"{day}/{kind}.json.gz"


def pick_latest(names: Iterable[str], kind: str, before: str | None = None) -> str | None:
    """Newest snapshot blob name of `kind`, optionally strictly before day `before`."""
    days = []
    for name in names:
        match = _BLOB_NAME.match(name)
        if match and match.group(2) == kind and (before is None or match.group(1) < before):
            days.append(match.group(1))
    return snapshot_name(max(days), kind) if days else None


def encode_snapshot(data: Mapping[str, Any]) -> bytes:
    return gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)


def decode_snapshot(blob: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(blob))


def to_entity(event: Mapping[str, Any]) -> dict[str, Any]:
    body = gzip.compress(canonical_json(event).encode("utf-8"), mtime=0)
    chunks = [body[i : i + _CHUNK] for i in range(0, len(body), _CHUNK)]
    if len(chunks) > _MAX_CHUNKS:
        raise ValueError(f"event {event['id']} is too large to store ({len(body)} bytes compressed)")
    entity: dict[str, Any] = {
        "PartitionKey": event["date"],
        "RowKey": event["id"],
        "source": event["source"],
        "kind": event["kind"],
        "category": event["category"],
        "type": event["type"],
    }
    for index, chunk in enumerate(chunks):
        entity[f"body{index}"] = chunk
    return entity


def from_entity(entity: Mapping[str, Any]) -> dict[str, Any]:
    parts = []
    while f"body{len(parts)}" in entity:
        parts.append(bytes(entity[f"body{len(parts)}"]))
    return json.loads(gzip.decompress(b"".join(parts)))


class Store:
    def __init__(self, blob_service: Any, table_service: Any) -> None:
        self._container = blob_service.get_container_client(SNAPSHOT_CONTAINER)
        self._tables = {
            OBSERVED: table_service.get_table_client(EVENTS_TABLE),
            "scheduled": table_service.get_table_client(SCHEDULE_TABLE),
        }

    # Snapshots

    def save_snapshot(self, day: str, kind: str, data: Mapping[str, Any]) -> None:
        from azure.storage.blob import ContentSettings

        self._container.upload_blob(
            snapshot_name(day, kind),
            encode_snapshot(data),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/gzip"),
        )

    def latest_snapshot(self, kind: str, before: str | None = None) -> dict[str, Any] | None:
        name = pick_latest(self._container.list_blob_names(), kind, before)
        if name is None:
            return None
        return decode_snapshot(self._container.download_blob(name).readall())

    # Events

    def replace_observed(self, day: str, source: str, events: Iterable[Mapping[str, Any]]) -> None:
        """Make `events` the complete set of observed events of `source` on `day` (idempotent re-runs)."""
        table = self._tables[OBSERVED]
        entities = [to_entity(event) for event in events]
        keep = {entity["RowKey"] for entity in entities}
        stale = table.query_entities(
            "PartitionKey eq @day and source eq @source",
            parameters={"day": day, "source": source},
            select=["PartitionKey", "RowKey"],
        )
        operations = [("delete", entity) for entity in stale if entity["RowKey"] not in keep]
        operations += [("upsert", entity, {"mode": "replace"}) for entity in entities]
        _submit(table, operations)

    def add_observed(self, events: Iterable[Mapping[str, Any]]) -> None:
        _submit(self._tables[OBSERVED], [("upsert", to_entity(e), {"mode": "replace"}) for e in events])

    def schedule_dates(self, source: str) -> dict[str, str]:
        rows = self._tables["scheduled"].query_entities(
            "source eq @source", parameters={"source": source}, select=["PartitionKey", "RowKey"]
        )
        return {row["RowKey"]: row["PartitionKey"] for row in rows}

    def apply_schedule(self, upserts: Iterable[Mapping[str, Any]], deletes: Mapping[str, str]) -> None:
        """`deletes` maps event id -> date (partition)."""
        operations = [("delete", {"PartitionKey": day, "RowKey": event_id}) for event_id, day in deletes.items()]
        operations += [("upsert", to_entity(event), {"mode": "replace"}) for event in upserts]
        _submit(self._tables["scheduled"], operations)

    def read_events(self, start: str, end: str) -> list[dict[str, Any]]:
        events = []
        for table in self._tables.values():
            for entity in table.query_entities(
                "PartitionKey ge @start and PartitionKey le @end", parameters={"start": start, "end": end}
            ):
                events.append(from_entity(entity))
        return events


def connect(settings: Any, credential: Any = None) -> Store:
    """Entra ID against Azure Storage; the Azurite well-known dev account only when explicitly enabled."""
    from azure.data.tables import TableServiceClient
    from azure.storage.blob import BlobServiceClient

    if settings.use_storage_emulator:
        blob = BlobServiceClient.from_connection_string(_EMULATOR)
        tables = TableServiceClient.from_connection_string(_EMULATOR)
        # In Azure the container and tables are created by infra/.
        container = blob.get_container_client(SNAPSHOT_CONTAINER)
        if not container.exists():
            container.create_container()
        for name in (EVENTS_TABLE, SCHEDULE_TABLE):
            tables.create_table_if_not_exists(name)
        return Store(blob, tables)

    from azure.identity import DefaultAzureCredential

    credential = credential or DefaultAzureCredential()
    return Store(
        BlobServiceClient(settings.require("blob_endpoint"), credential=credential),
        TableServiceClient(endpoint=settings.require("table_endpoint"), credential=credential),
    )


def _submit(table: Any, operations: list[tuple[Any, ...]]) -> None:
    # Entity group transactions are limited to one partition and 100 operations.
    by_partition: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for operation in operations:
        by_partition[operation[1]["PartitionKey"]].append(operation)
    for batch in by_partition.values():
        for start in range(0, len(batch), _BATCH):
            table.submit_transaction(batch[start : start + _BATCH])
