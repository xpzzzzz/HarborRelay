import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.config import Settings
from app.services.vector_snapshots import (
    INCOMPLETE,
    export_collection,
    validate_snapshot,
    vector_lock,
)
from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable


class VectorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SimpleNamespace(
            settings=Settings(
                _env_file=None,
                chroma_snapshot_dir=str(self.root / "snapshots"),
                chroma_snapshot_keep=2,
            ),
            persist_dir=self.root / "vectors",
            embedding_dimension=2,
            collection_name="synthetic",
            embedding_provider="ollama",
            embedding_model="synthetic",
            count=lambda: 1,
            _resolve_path=lambda value: Path(value),
            collection=SimpleNamespace(
                get=lambda **_: {
                    "ids": ["synthetic-1"],
                    "documents": ["Synthetic"],
                    "metadatas": [{"source": "synthetic", "db_id": 1}],
                    "embeddings": [[1.0, 0.0]],
                }
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_export_checksum_count_and_retention_preserve_unowned_directories(self):
        legacy = self.root / "snapshots" / "legacy-untouched"
        legacy.mkdir(parents=True)
        for _ in range(3):
            snapshot = Path(export_collection(self.store))
            self.assertEqual(validate_snapshot(snapshot).count, 1)
        self.assertEqual(len(list(legacy.parent.glob("logical-*"))), 2)
        self.assertTrue(legacy.is_dir())
        self.assertFalse((snapshot / "chroma.sqlite3").exists())

    def test_modified_content_is_rejected_before_destination_creation(self):
        snapshot = Path(export_collection(self.store))
        path = snapshot / "records.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Synthetic", b"Corrupted"))
        destination = self.root / "restore"
        settings = self.store.settings.model_copy(
            update={"chroma_persist_dir": str(destination)}
        )
        with self.assertRaisesRegex(ValueError, "checksum"):
            ChromaKnowledgeStore.restore_snapshot(snapshot, settings)
        self.assertFalse(destination.exists())

    def test_failed_export_never_publishes_or_prunes_previous_snapshot(self):
        previous = Path(export_collection(self.store))
        self.store.collection.get = Mock(side_effect=OSError("synthetic export fault"))
        with self.assertRaises(OSError):
            export_collection(self.store)
        self.assertEqual(validate_snapshot(previous).count, 1)
        partial = next(previous.parent.glob(".partial-*"))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            validate_snapshot(partial)

    def test_nested_snapshot_directory_is_rejected(self):
        self.store.settings.chroma_snapshot_dir = str(
            self.store.persist_dir / "snapshots"
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            export_collection(self.store)

    def test_interrupted_restore_is_not_opened_as_active_collection(self):
        destination = self.root / "incomplete"
        destination.mkdir()
        (destination / INCOMPLETE).touch()
        with self.assertRaises(VectorStoreUnavailable):
            ChromaKnowledgeStore(
                self.store.settings.model_copy(
                    update={"chroma_persist_dir": str(destination)}
                )
            )

    def test_alias_paths_share_the_same_lock_across_store_instances(self):
        first = vector_lock(self.root / "vectors")
        second = vector_lock(self.root / "sub" / ".." / "vectors")
        self.assertIs(first, second)
        acquired = threading.Event()
        with first:
            thread = threading.Thread(target=lambda: self._acquire(second, acquired))
            thread.start()
            self.assertFalse(acquired.wait(0.05))
        thread.join(timeout=2)
        self.assertTrue(acquired.is_set())

    @staticmethod
    def _acquire(lock, acquired):
        with lock:
            acquired.set()


if __name__ == "__main__":
    unittest.main()
