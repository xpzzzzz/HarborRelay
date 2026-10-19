"""Atomic logical collection exports; never copy live SQLite/HNSW files.

The caller holds the shared process lock for the entire export. Embedded Chroma
has a single writer process; this lock is deliberately not a distributed lease.
"""

import hashlib
import os
import shutil
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


_locks: dict[str, object] = {}
_registry_lock = threading.Lock()
FORMAT = "harborrelay-chroma-logical-v1"
INCOMPLETE = ".harborrelay-restore-incomplete"


def vector_lock(path: Path):
    key = str(path.resolve()).casefold() if os.name == "nt" else str(path.resolve())
    with _registry_lock:
        return _locks.setdefault(key, threading.RLock())


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with vector_lock(self.persist_dir):
            return method(self, *args, **kwargs)

    return wrapped


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str = Field(min_length=1)
    document: str
    metadata: dict[str, str | int | float | bool]
    embedding: list[float] = Field(min_length=1)


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: str
    collection: str
    provider: str
    model: str
    dimension: int | None
    count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str
    consistency: str = "single_process_application_lock"


def export_collection(store) -> str:
    root = store._resolve_path(store.settings.chroma_snapshot_dir).resolve()
    if root == store.persist_dir.resolve() or root.is_relative_to(
        store.persist_dir.resolve()
    ):
        raise ValueError(
            "Snapshot directory must be outside the active vector directory"
        )
    with vector_lock(root):
        return _export_collection_locked(store, root)


def _export_collection_locked(store, root) -> str:
    root.mkdir(parents=True, exist_ok=True)
    name = (
        "logical-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        + "-"
        + uuid4().hex[:8]
    )
    partial = root / (".partial-" + name)
    destination = root / name
    partial.mkdir()
    # A failed export remains visibly partial for diagnosis; it cannot be restored
    # or pruned as a completed snapshot. Never remove an arbitrary configured path.
    digest = hashlib.sha256()
    seen = set()
    count = store.count()
    with (partial / "records.jsonl").open("wb") as target:
        for offset in range(0, count, 256):
            page = store.collection.get(
                limit=256,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            for index, identifier in enumerate(page["ids"]):
                if identifier in seen:
                    raise ValueError("Duplicate vector ID during export")
                seen.add(identifier)
                record = SnapshotRecord(
                    id=identifier,
                    document=page["documents"][index],
                    metadata=page["metadatas"][index],
                    embedding=[float(value) for value in page["embeddings"][index]],
                )
                if len(record.embedding) != store.embedding_dimension:
                    raise ValueError("Vector dimension changed during export")
                payload = (record.model_dump_json() + "\n").encode("utf-8")
                target.write(payload)
                digest.update(payload)
    if len(seen) != count or store.count() != count:
        raise ValueError("Vector count changed during export")
    manifest = SnapshotManifest(
        format=FORMAT,
        collection=store.collection_name,
        provider=store.embedding_provider,
        model=store.embedding_model,
        dimension=store.embedding_dimension,
        count=count,
        sha256=digest.hexdigest(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    (partial / "manifest.json").write_bytes(
        manifest.model_dump_json(indent=2).encode("utf-8")
    )
    partial.rename(destination)
    # Only completed exports in this format belong to automatic retention.
    completed = []
    for candidate in root.glob("logical-*"):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            saved = SnapshotManifest.model_validate_json(
                (candidate / "manifest.json").read_bytes()
            )
        except (ValueError, OSError):
            continue
        if saved.format == FORMAT:
            completed.append(candidate)
    completed.sort(reverse=True)
    for stale in completed[max(1, store.settings.chroma_snapshot_keep) :]:
        if stale.resolve().parent != root:
            raise ValueError(
                "Snapshot retention target escaped its configured directory"
            )
        shutil.rmtree(stale)
    return str(destination)


def validate_snapshot(path: Path) -> SnapshotManifest:
    path = path.resolve()
    if path.name.startswith(".partial-"):
        raise ValueError("Incomplete export cannot be restored")
    manifest = SnapshotManifest.model_validate_json(
        (path / "manifest.json").read_bytes()
    )
    if (
        manifest.format != FORMAT
        or manifest.consistency != "single_process_application_lock"
    ):
        raise ValueError("Unsupported snapshot format or consistency contract")
    digest = hashlib.sha256()
    seen = set()
    with (path / "records.jsonl").open("rb") as records:
        for raw in records:
            digest.update(raw)
            record = SnapshotRecord.model_validate_json(raw)
            if record.id in seen or len(record.embedding) != manifest.dimension:
                raise ValueError("Duplicate vector ID or invalid dimension in snapshot")
            seen.add(record.id)
    if len(seen) != manifest.count or digest.hexdigest() != manifest.sha256:
        raise ValueError("Snapshot count/checksum mismatch")
    if manifest.count and (manifest.dimension is None or manifest.dimension <= 0):
        raise ValueError("Non-empty snapshot must declare a dimension")
    return manifest


def restore_collection(store_class, snapshot: str | Path, settings):
    """Restore to a new directory only. Failed imports remain marked unusable."""
    source = Path(snapshot).resolve()
    manifest = validate_snapshot(source)
    identity = store_class.__new__(store_class)
    identity.settings = settings
    identity.embedding_provider = settings.knowledge_embedding_provider.strip().lower()
    identity.embedding_model = identity._configured_embedding_model()
    if (manifest.provider, manifest.model, manifest.collection) != (
        identity.embedding_provider,
        identity.embedding_model,
        identity._build_collection_name(),
    ):
        raise ValueError(
            "Snapshot provider/model/collection does not match restore settings"
        )
    destination = identity._resolve_path(settings.chroma_persist_dir).resolve()
    if (
        destination == source
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("Restore destination must be separate from snapshot")
    with vector_lock(destination):
        destination.mkdir(parents=True, exist_ok=False)
        marker = destination / INCOMPLETE
        marker.write_text(
            "Restore in progress; do not open this directory.\n", encoding="utf-8"
        )
        store = store_class(settings, _restoring=True)
        if not store.can_embed:
            raise RuntimeError(store.error or "Restore vector store unavailable")
        batch = []
        imported_digest = hashlib.sha256()
        with (source / "records.jsonl").open("rb") as records:
            for raw in records:
                imported_digest.update(raw)
                batch.append(SnapshotRecord.model_validate_json(raw))
                if len(batch) == 256:
                    _restore_batch(store, batch)
                    batch = []
        if batch:
            _restore_batch(store, batch)
        if (
            store.count() != manifest.count
            or imported_digest.hexdigest() != manifest.sha256
        ):
            raise ValueError("Restored collection count does not match snapshot")
        store.embedding_dimension = manifest.dimension
        marker.unlink()
        return store


def _restore_batch(store, batch):
    embeddings = store._validate_embeddings(
        [record.embedding for record in batch], len(batch)
    )
    store.collection.upsert(
        ids=[r.id for r in batch],
        documents=[r.document for r in batch],
        metadatas=[r.metadata for r in batch],
        embeddings=embeddings,
    )


def main(argv=None):
    import argparse
    from app.core.config import get_settings
    from app.services.vector_store import ChromaKnowledgeStore

    parser = argparse.ArgumentParser(
        description="Restore a logical vector snapshot to a NEW directory; never switches the active index."
    )
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    settings = get_settings().model_copy(
        update={"chroma_persist_dir": args.destination}
    )
    store = ChromaKnowledgeStore.restore_snapshot(args.snapshot, settings)
    print(
        f"Restored {store.count()} vectors to {store.persist_dir}; active configuration was not changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
