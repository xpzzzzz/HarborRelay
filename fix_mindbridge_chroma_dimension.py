from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path.cwd()
VECTOR_PATH = ROOT / "app" / "services" / "vector_store.py"
TEST_PATH = ROOT / "tests" / "test_ollama_embeddings.py"
BACKUP_DIR = ROOT / "target" / f"chroma-dimension-hotfix-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def backup(path: Path) -> None:
    relative = path.relative_to(ROOT)
    destination = BACKUP_DIR / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def main() -> None:
    if not VECTOR_PATH.exists() or not TEST_PATH.exists():
        raise SystemExit(
            "Run this script from the MindBridge project root; "
            "app/services/vector_store.py and tests/test_ollama_embeddings.py are required."
        )

    backup(VECTOR_PATH)
    backup(TEST_PATH)

    vector_text = VECTOR_PATH.read_text(encoding="utf-8")

    old_init = '''            stored_dimension = metadata.get("embedding_dimension")
            if isinstance(stored_dimension, int) and stored_dimension > 0:
                self.embedding_dimension = stored_dimension
            self.can_embed = True
'''
    new_init = '''            stored_dimension = metadata.get("embedding_dimension")
            if isinstance(stored_dimension, int) and stored_dimension > 0:
                self.embedding_dimension = stored_dimension
            else:
                self.embedding_dimension = self._infer_collection_dimension()
            self.can_embed = True
'''
    vector_text = replace_once(
        vector_text,
        old_init,
        new_init,
        "vector_store init dimension recovery",
    )

    old_record = '''    def _record_embedding_dimension(self, dimension: int) -> None:
        if self.embedding_dimension == dimension:
            return
        self.embedding_dimension = dimension
        if self.collection is not None:
            self.collection.modify(metadata=self._collection_metadata())

    def _configured_embedding_model(self) -> str:
'''
    new_record = '''    def _record_embedding_dimension(self, dimension: int) -> None:
        # hnsw:space is immutable after collection creation in Chroma 0.5.x.
        # Do not call collection.modify() with the creation metadata here.
        # The current process keeps the dimension in memory; after restart it is
        # recovered from one persisted embedding by _infer_collection_dimension().
        self.embedding_dimension = dimension

    def _infer_collection_dimension(self) -> int | None:
        if self.collection is None or self.collection.count() == 0:
            return None
        result = self.collection.get(limit=1, include=["embeddings"])
        embeddings = result.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            return None
        first = embeddings[0]
        if first is None:
            return None
        dimension = len(first)
        return dimension if dimension > 0 else None

    def _configured_embedding_model(self) -> str:
'''
    vector_text = replace_once(
        vector_text,
        old_record,
        new_record,
        "vector_store immutable hnsw metadata fix",
    )

    VECTOR_PATH.write_text(vector_text, encoding="utf-8", newline="\n")

    test_text = TEST_PATH.read_text(encoding="utf-8")
    marker = '''    @patch("app.services.vector_store.httpx.post")
    def test_openai_route_remains_supported(self, post: Mock) -> None:
'''
    test_method = '''    def test_record_dimension_does_not_modify_chroma_metadata(self) -> None:
        store = self._store("ollama")
        store.collection = Mock()

        store._record_embedding_dimension(768)

        self.assertEqual(store.embedding_dimension, 768)
        store.collection.modify.assert_not_called()

    def test_existing_collection_dimension_is_inferred(self) -> None:
        store = self._store("ollama")
        store.collection = Mock()
        store.collection.count.return_value = 1
        store.collection.get.return_value = {"embeddings": [[0.0, 0.1, 0.2]]}

        self.assertEqual(store._infer_collection_dimension(), 3)
        store.collection.get.assert_called_once_with(limit=1, include=["embeddings"])

'''
    if "test_record_dimension_does_not_modify_chroma_metadata" not in test_text:
        test_text = replace_once(
            test_text,
            marker,
            test_method + marker,
            "test insertion point",
        )
        TEST_PATH.write_text(test_text, encoding="utf-8", newline="\n")

    print("Chroma dimension hotfix applied successfully.")
    print(f"Backup directory: {BACKUP_DIR}")
    print("Changed:")
    print("  - app/services/vector_store.py")
    print("  - tests/test_ollama_embeddings.py")
    print("No database, Docker volume, Chroma collection, or data file was deleted.")


if __name__ == "__main__":
    main()
