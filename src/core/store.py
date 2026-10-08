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

from .events import OBSERVED, canonical_json, local_date, shift_date
from .schedule import reconcile_schedule

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

        name = snapshot_name(day, kind)
        self._container.upload_blob(
            name,
            encode_snapshot(data),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/gzip"),
        )
        index_name = f"latest/{kind}.json.gz"
        index = self._read_optional(index_name)
        current = index.get("current") if index else None
        if current and current > name:
            if not index.get("previous") or name > index["previous"]:
                self._container.upload_blob(index_name, encode_snapshot({**index, "previous": name}), overwrite=True)
            return
        if current == name:
            previous = index.get("previous")
        elif current:
            previous = current
        else:
            previous = pick_latest(self._container.list_blob_names(), kind, before=day)
        self._container.upload_blob(index_name, encode_snapshot({"current": name, "previous": previous}), overwrite=True)

    def latest_snapshot(self, kind: str, before: str | None = None) -> dict[str, Any] | None:
        snapshot_name("", kind)
        index = self._read_optional(f"latest/{kind}.json.gz")
        if index:
            name = pick_latest((name for name in index.values() if name), kind, before)
            if name:
                return decode_snapshot(self._container.download_blob(name).readall())
            if index.get("previous") is None:
                return None
        name = pick_latest(self._container.list_blob_names(), kind, before)
        if name is None:
            return None
        return decode_snapshot(self._container.download_blob(name).readall())

    def _read_optional(self, name: str) -> dict[str, Any] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return decode_snapshot(self._container.download_blob(name).readall())
        except ResourceNotFoundError:
            return None

    def source_status(self, kind: str) -> dict[str, Any] | None:
        snapshot_name("", kind)
        return self._read_optional(f"status/{kind}.json.gz")

    def save_source_status(self, kind: str, status: Mapping[str, Any]) -> None:
        day = local_date(status["last_attempt_at"])
        data = encode_snapshot(status)
        self._container.upload_blob(f"runs/{snapshot_name(day, kind)}", data, overwrite=True)
        self._container.upload_blob(f"status/{kind}.json.gz", data, overwrite=True)

    def stage_run(self, day: str, kind: str, batch: Mapping[str, Any]) -> None:
        self._container.upload_blob(f"pending/{snapshot_name(day, kind)}", encode_snapshot(batch), overwrite=True)

    def pending_runs(self, kind: str) -> Iterable[tuple[str, dict[str, Any]]]:
        for name in sorted(self._container.list_blob_names(name_starts_with="pending/")):
            match = _BLOB_NAME.fullmatch(name.removeprefix("pending/"))
            if match and match.group(2) == kind:
                yield match.group(1), decode_snapshot(self._container.download_blob(name).readall())

    def discard_run(self, day: str, kind: str) -> None:
        self._container.delete_blob(f"pending/{snapshot_name(day, kind)}")

    def commit_run(
        self, day: str, kind: str, snapshot: Mapping[str, Any], observed: Iterable[Mapping[str, Any]],
        scheduled: Iterable[Mapping[str, Any]] | None = None, *, replace: bool = True,
    ) -> dict[str, int]:
        batch = {
            "snapshot": snapshot,
            "observed": list(observed),
            "scheduled": list(scheduled) if scheduled is not None else None,
            "replace": replace,
        }
        self.stage_run(day, kind, batch)
        return self._complete_run(day, kind, batch)

    def recover_pending(self, kind: str) -> dict[str, Any] | None:
        recovered = None
        for day, batch in self.pending_runs(kind):
            self._complete_run(day, kind, batch)
            recovered = batch["snapshot"]
        return recovered

    def _complete_run(self, day: str, kind: str, batch: Mapping[str, Any]) -> dict[str, int]:
        source = "retail_prices" if kind == "prices" else kind
        if batch["replace"]:
            self.replace_observed(day, source, batch["observed"])
        else:
            self.add_observed(batch["observed"])
        counts = {}
        if batch["scheduled"] is not None:
            start = min([shift_date(day, 1), *(event["date"] for event in batch["scheduled"])])
            existing = self.schedule_dates(source, start=start)
            upserts, deletes = reconcile_schedule(existing, batch["scheduled"], day)
            self.apply_schedule(upserts, {event_id: existing[event_id] for event_id in deletes})
            counts = {"schedule_upserts": len(upserts), "schedule_deletes": len(deletes)}
        self.save_snapshot(day, kind, batch["snapshot"])
        self.discard_run(day, kind)
        return counts

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

    def schedule_dates(self, source: str, *, start: str | None = None) -> dict[str, str]:
        query = "source eq @source"
        parameters = {"source": source}
        if start:
            query += " and PartitionKey ge @start"
            parameters["start"] = start
        rows = self._tables["scheduled"].query_entities(
            query, parameters=parameters, select=["PartitionKey", "RowKey"]
        )
        return {row["RowKey"]: row["PartitionKey"] for row in rows}

    def apply_schedule(self, upserts: Iterable[Mapping[str, Any]], deletes: Mapping[str, str]) -> None:
        """`deletes` maps event id -> date (partition)."""
        operations = [("delete", {"PartitionKey": day, "RowKey": event_id}) for event_id, day in deletes.items()]
        operations += [("upsert", to_entity(event), {"mode": "replace"}) for event in upserts]
        _submit(self._tables["scheduled"], operations)

    def read_events(
        self, start: str, end: str, *, category: str | None = None, scheduled_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = "PartitionKey ge @start and PartitionKey le @end"
        parameters = {"start": start, "end": end}
        if category:
            query += " and category eq @category"
            parameters["category"] = category
        events = []
        for kind, table in self._tables.items():
            if scheduled_only and kind == OBSERVED:
                continue
            for entity in table.query_entities(query, parameters=parameters):
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
