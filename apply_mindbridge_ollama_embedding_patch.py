from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path.cwd()
BACKUP_ROOT = ROOT / "target" / f"ollama-embedding-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    destination = ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="\n")


def backup(path: str) -> None:
    source = ROOT / path
    if not source.exists():
        return
    destination = BACKUP_ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_block(text: str, start: str, end: str, replacement: str, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker not found: {start!r}")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker not found: {end!r}")
    return text[:start_index] + replacement.rstrip() + "\n\n" + text[end_index:]


def set_env_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    line = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


VECTOR_STORE_CLASS = r'''class ChromaKnowledgeStore:
    """Provider-aware embeddings stored and queried in a persistent Chroma collection."""

    SUPPORTED_EMBEDDING_PROVIDERS = {"openai", "ollama"}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.embedding_provider = settings.knowledge_embedding_provider.strip().lower()
        self.embedding_model = self._configured_embedding_model()
        self.embedding_dimension: int | None = None
        self.can_embed = False
        self.error = ""
        self.persist_dir = self._resolve_path(settings.chroma_persist_dir)
        self.collection_name = self._build_collection_name()
        self.client = None
        self.collection = None

        if not settings.knowledge_vector_enabled:
            self.error = "Chroma vector retrieval is disabled."
            return
        if self.embedding_provider not in self.SUPPORTED_EMBEDDING_PROVIDERS:
            self._set_unavailable(
                f"Unsupported KNOWLEDGE_EMBEDDING_PROVIDER: {self.embedding_provider!r}. "
                "Expected 'openai' or 'ollama'."
            )
            return
        if self.embedding_provider == "openai" and not settings.openai_api_key:
            self._set_unavailable(
                f"OPENAI_API_KEY is missing; OpenAI embeddings are unavailable. "
                f"Falling back to {FALLBACK_RETRIEVAL_LABEL}."
            )
            return

        try:
            import chromadb
        except ImportError as exc:
            self._set_unavailable(
                f"chromadb is not installed; vector retrieval is unavailable. "
                f"Falling back to {FALLBACK_RETRIEVAL_LABEL}.",
                exc,
            )
            return

        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(self.persist_dir))
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=None,
                metadata=self._collection_metadata(),
            )
            metadata = self.collection.metadata or {}
            stored_dimension = metadata.get("embedding_dimension")
            if isinstance(stored_dimension, int) and stored_dimension > 0:
                self.embedding_dimension = stored_dimension
            self.can_embed = True
        except Exception as exc:
            self._set_unavailable(
                f"Failed to initialize Chroma collection {self.collection_name!r}: "
                f"{type(exc).__name__}: {exc}",
                exc,
            )

    @property
    def available(self) -> bool:
        return self.can_embed and not self.error

    def check_provider_available(self) -> bool:
        if not self.can_embed:
            return False
        if self.embedding_provider != "ollama":
            return True
        try:
            response = httpx.get(
                self._ollama_endpoint("tags"),
                timeout=self.settings.embedding_timeout_seconds,
            )
            response.raise_for_status()
            models = response.json().get("models", [])
            names = {
                str(value)
                for item in models
                if isinstance(item, dict)
                for value in (item.get("name"), item.get("model"))
                if value
            }
            if self.embedding_model not in names:
                raise VectorStoreUnavailable(
                    f"Ollama model {self.embedding_model!r} is not present in /api/tags."
                )
            self.error = ""
            return True
        except Exception as exc:
            self.record_error(exc)
            return False

    def upsert_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        if len(chunks) != len(embeddings):
            raise VectorStoreUnavailable("Embedding count did not match the knowledge chunk count.")
        pairs = [
            (chunk, embedding)
            for chunk, embedding in zip(chunks, embeddings)
            if chunk.id is not None and chunk.content.strip()
        ]
        if not pairs:
            return 0
        rows = [chunk for chunk, _ in pairs]
        vectors = self._validate_embeddings([embedding for _, embedding in pairs], len(pairs))
        ids = [self._id(int(chunk.id)) for chunk in rows]
        documents = [chunk.content for chunk in rows]
        metadatas = [
            {"db_id": int(chunk.id), "source": chunk.source, "source_index": int(chunk.source_index)}
            for chunk in rows
        ]
        self.collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=vectors)
        self.snapshot()
        return len(rows)

    def sync_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        stale_ids = sorted(current_ids - valid_ids)
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        return self.upsert_chunks(chunks, embeddings)

    def has_exact_chunk_ids(self, chunks: list[KnowledgeChunk]) -> bool:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        return current_ids == valid_ids

    def delete_source(self, source: str) -> None:
        if not self.can_embed:
            return
        self.collection.delete(where={"source": source})

    def query(self, query_embedding: list[float], top_k: int) -> list[VectorSearchHit]:
        vector = self._validate_embeddings([query_embedding], 1)[0]
        result = self.collection.query(
            query_embeddings=[vector],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorSearchHit(
                    chunk_id=int(metadata["db_id"]) if metadata.get("db_id") is not None else None,
                    source=str(metadata.get("source", "")),
                    source_index=int(metadata.get("source_index", 0)),
                    content=document or "",
                    score=1.0 / (1.0 + max(0.0, distance)),
                )
            )
        return hits

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.can_embed:
            raise VectorStoreUnavailable(self.error or "Vector embeddings are unavailable.")
        if not texts:
            return []
        try:
            if self.embedding_provider == "ollama":
                embeddings = self._embed_ollama(texts)
            else:
                embeddings = self._embed_openai(texts)
            self.error = ""
            return embeddings
        except VectorStoreUnavailable as exc:
            self.record_error(exc)
            raise
        except Exception as exc:
            wrapped = VectorStoreUnavailable(
                f"{self.embedding_provider} embedding request failed: {type(exc).__name__}: {exc}"
            )
            self.record_error(wrapped)
            raise wrapped from exc

    def snapshot(self) -> str | None:
        if not self.can_embed:
            return None
        if not self.persist_dir.exists():
            return None
        snapshot_root = self._resolve_path(self.settings.chroma_snapshot_dir)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        destination = snapshot_root / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copytree(self.persist_dir, destination)
        self._prune_snapshots(snapshot_root)
        return str(destination)

    def count(self) -> int:
        if not self.can_embed:
            return 0
        return int(self.collection.count())

    def record_error(self, exc: Exception) -> None:
        self.error = f"{type(exc).__name__}: {exc}"

    def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.settings.openai_embedding_model,
            "input": [text if text.strip() else " " for text in texts],
        }
        response = httpx.post(
            f"{self.settings.openai_base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json=payload,
            timeout=self.settings.embedding_timeout_seconds,
        )
        response.raise_for_status()
        rows = sorted(response.json().get("data", []), key=lambda item: item.get("index", 0))
        return self._validate_embeddings([row.get("embedding") for row in rows], len(texts))

    def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.settings.ollama_embedding_model,
            "input": [text if text.strip() else " " for text in texts],
        }
        response = httpx.post(
            self._ollama_endpoint("embed"),
            json=payload,
            timeout=self.settings.embedding_timeout_seconds,
        )
        response.raise_for_status()
        return self._validate_embeddings(response.json().get("embeddings", []), len(texts))

    def _validate_embeddings(self, embeddings: object, expected_count: int) -> list[list[float]]:
        if not isinstance(embeddings, list) or len(embeddings) != expected_count:
            raise VectorStoreUnavailable(
                f"Embedding response count mismatch: expected {expected_count}, "
                f"received {len(embeddings) if isinstance(embeddings, list) else 'non-list'}."
            )
        resolved: list[list[float]] = []
        for embedding in embeddings:
            if not isinstance(embedding, list) or not embedding:
                raise VectorStoreUnavailable("Embedding response contained an empty or invalid vector.")
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in embedding):
                raise VectorStoreUnavailable("Embedding response contained a non-numeric value.")
            resolved.append([float(value) for value in embedding])
        dimensions = {len(embedding) for embedding in resolved}
        if len(dimensions) != 1:
            raise VectorStoreUnavailable("Embedding response contained inconsistent vector dimensions.")
        dimension = dimensions.pop()
        if self.embedding_dimension is not None and self.embedding_dimension != dimension:
            raise VectorStoreUnavailable(
                f"Embedding dimension mismatch for {self.embedding_provider}:{self.embedding_model}: "
                f"expected {self.embedding_dimension}, received {dimension}."
            )
        self._record_embedding_dimension(dimension)
        return resolved

    def _record_embedding_dimension(self, dimension: int) -> None:
        if self.embedding_dimension == dimension:
            return
        self.embedding_dimension = dimension
        if self.collection is not None:
            self.collection.modify(metadata=self._collection_metadata())

    def _configured_embedding_model(self) -> str:
        if self.embedding_provider == "ollama":
            return self.settings.ollama_embedding_model
        return self.settings.openai_embedding_model

    def _build_collection_name(self) -> str:
        identity = f"{self.embedding_provider}:{self.embedding_model}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        safe_base = re.sub(r"[^A-Za-z0-9._-]+", "-", self.settings.chroma_collection_name).strip("._-")
        safe_base = safe_base or "mindbridge-knowledge"
        safe_provider = re.sub(r"[^A-Za-z0-9_-]+", "-", self.embedding_provider).strip("_-") or "embedding"
        suffix = f"-{safe_provider}-{digest}"
        base_limit = max(3, 63 - len(suffix))
        base = safe_base[:base_limit].rstrip("._-") or "rag"
        return f"{base}{suffix}"

    def _collection_metadata(self) -> dict[str, str | int]:
        metadata: dict[str, str | int] = {
            "hnsw:space": "cosine",
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
        }
        if self.embedding_dimension is not None:
            metadata["embedding_dimension"] = self.embedding_dimension
        return metadata

    def _ollama_endpoint(self, endpoint: str) -> str:
        base = self.settings.ollama_base_url.rstrip("/")
        if base.endswith("/api"):
            return f"{base}/{endpoint.lstrip('/')}"
        return f"{base}/api/{endpoint.lstrip('/')}"

    def _set_unavailable(self, message: str, exc: Exception | None = None) -> None:
        if self.settings.knowledge_vector_required:
            if exc is None:
                raise VectorStoreUnavailable(message)
            raise VectorStoreUnavailable(message) from exc
        self.error = message

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.settings.project_root / path

    def _prune_snapshots(self, snapshot_root: Path) -> None:
        keep = max(1, self.settings.chroma_snapshot_keep)
        snapshots = sorted([path for path in snapshot_root.iterdir() if path.is_dir()], reverse=True)
        for stale in snapshots[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    def _id(self, chunk_id: int) -> str:
        return f"knowledge-chunk-{chunk_id}"
'''

STATUS_METHOD = r'''    def status(self) -> dict:
        vector_chunks = None
        vector_error = getattr(self.vector_store, "error", "")
        vector_available = self.vector_store.available
        if self.vector_store.can_embed:
            if not self.vector_store.check_provider_available():
                vector_available = False
                vector_error = self.vector_store.error
            try:
                vector_chunks = self.vector_store.count()
            except Exception as exc:
                self.vector_store.record_error(exc)
                vector_available = False
                vector_error = self.vector_store.error
        return {
            "retrievalOrder": [
                PRIMARY_RETRIEVAL_LABEL,
                f"{FALLBACK_RETRIEVAL_LABEL} when Chroma or the configured embedding provider is unavailable",
            ],
            "primaryRetrieval": PRIMARY_RETRIEVAL_LABEL,
            "fallbackRetrieval": FALLBACK_RETRIEVAL_LABEL,
            "databaseChunks": self.count(),
            "vectorEnabled": self.settings.knowledge_vector_enabled,
            "vectorAvailable": vector_available,
            "vectorRequired": self.settings.knowledge_vector_required,
            "embeddingProvider": self.vector_store.embedding_provider,
            "embeddingModel": self.vector_store.embedding_model,
            "embeddingDimension": self.vector_store.embedding_dimension,
            "vectorChunks": vector_chunks,
            "chromaPersistDir": self.settings.chroma_persist_dir,
            "chromaBaseCollectionName": self.settings.chroma_collection_name,
            "chromaCollectionName": self.vector_store.collection_name,
            "chromaSnapshotDir": self.settings.chroma_snapshot_dir,
            "candidateK": self.settings.knowledge_candidate_k,
            "hybridVectorWeight": self.settings.knowledge_hybrid_vector_weight,
            "hybridBm25Weight": self.settings.knowledge_hybrid_bm25_weight,
            "rerankEnabled": self.settings.knowledge_rerank_enabled,
            "vectorError": vector_error,
        }
'''

REBUILD_METHOD = r'''    def rebuild_vector_index(self) -> int:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma vector retrieval is unavailable.")
        rows = (
            self.db.query(KnowledgeChunk)
            .order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc())
            .all()
        )
        try:
            embeddings = self._embeddings_for_chunks(rows, force_refresh=True)
            for chunk, embedding in zip(rows, embeddings):
                chunk.embedding_json = serialize_embedding(
                    embedding,
                    self.vector_store.embedding_provider,
                    self.vector_store.embedding_model,
                )
            indexed = self.vector_store.sync_chunks(rows, embeddings)
            self.db.commit()
            return indexed
        except Exception as exc:
            self.db.rollback()
            self.vector_store.record_error(exc)
            raise RuntimeError(self.vector_store.error) from exc
'''

ENSURE_METHOD = r'''    def _ensure_vector_index(self) -> None:
        rows = (
            self.db.query(KnowledgeChunk)
            .order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc())
            .all()
        )
        if not rows:
            return
        if (
            self.vector_store.count() == len(rows)
            and all(
                parse_embedding(
                    row.embedding_json,
                    self.vector_store.embedding_provider,
                    self.vector_store.embedding_model,
                )
                is not None
                for row in rows
            )
            and self.vector_store.has_exact_chunk_ids(rows)
        ):
            return
        self._sync_vector_chunks(rows)
        self.db.commit()
'''

INDEX_METHOD = r'''    def _index_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = serialize_embedding(
                    embedding,
                    self.vector_store.embedding_provider,
                    self.vector_store.embedding_model,
                )
            self.vector_store.upsert_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("index", exc)
'''

SYNC_METHOD = r'''    def _sync_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = serialize_embedding(
                    embedding,
                    self.vector_store.embedding_provider,
                    self.vector_store.embedding_model,
                )
            self.vector_store.sync_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("sync", exc)
'''

EMBEDDINGS_METHOD = r'''    def _embeddings_for_chunks(
        self,
        chunks: list[KnowledgeChunk],
        force_refresh: bool = False,
    ) -> list[list[float]]:
        embeddings: list[list[float] | None] = []
        missing_indexes = []
        missing_texts = []
        for index, chunk in enumerate(chunks):
            embedding = None if force_refresh else parse_embedding(
                chunk.embedding_json,
                self.vector_store.embedding_provider,
                self.vector_store.embedding_model,
            )
            embeddings.append(embedding)
            if embedding is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.content)
        if missing_texts:
            new_embeddings = self.vector_store.embed_texts(missing_texts)
            for index, embedding in zip(missing_indexes, new_embeddings):
                embeddings[index] = embedding
        resolved = [embedding for embedding in embeddings if embedding is not None]
        if len(resolved) != len(chunks):
            raise ValueError("Embedding response count did not match knowledge chunks.")
        return resolved
'''

HANDLE_METHOD = r'''    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        self.vector_store.record_error(exc)
        if self.settings.knowledge_vector_required:
            raise exc
        logger.warning(
            "%s %s failed; falling back to %s: %s",
            PRIMARY_RETRIEVAL_LABEL,
            action,
            FALLBACK_RETRIEVAL_LABEL,
            exc,
        )
'''

PARSE_FUNCTIONS = r'''def serialize_embedding(embedding: list[float], provider: str, model: str) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "dimension": len(embedding),
        "vector": [float(value) for value in embedding],
    }
    return json.dumps(payload, separators=(",", ":"))


def parse_embedding(
    raw: str | None,
    provider: str | None = None,
    model: str | None = None,
) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        if provider is not None and data.get("provider") != provider:
            return None
        if model is not None and data.get("model") != model:
            return None
        vector = data.get("vector")
        dimension = data.get("dimension")
        if not isinstance(vector, list) or not vector:
            return None
        if not isinstance(dimension, int) or dimension != len(vector):
            return None
        data = vector
    elif provider is not None or model is not None:
        # Legacy bare arrays have no provider/model identity and must not be reused
        # after switching embedding backends.
        return None

    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in data):
        return None
    return [float(item) for item in data]
'''

TEST_FILE = r'''from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.services.knowledge import KnowledgeService, parse_embedding, serialize_embedding
from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable


class OllamaEmbeddingTests(unittest.TestCase):
    def _store(self, provider: str, required: bool = False) -> ChromaKnowledgeStore:
        settings = Settings(
            _env_file=None,
            knowledge_embedding_provider=provider,
            knowledge_vector_required=required,
            ollama_base_url="http://ollama:11434",
            ollama_embedding_model="nomic-embed-text:latest",
            openai_base_url="https://api.openai.test/v1",
            openai_api_key="test-key",
            openai_embedding_model="text-embedding-3-small",
        )
        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store.settings = settings
        store.embedding_provider = provider
        store.embedding_model = (
            settings.ollama_embedding_model if provider == "ollama" else settings.openai_embedding_model
        )
        store.embedding_dimension = None
        store.can_embed = True
        store.error = ""
        store.collection = None
        return store

    @patch("app.services.vector_store.httpx.post")
    def test_ollama_uses_batch_embed_endpoint(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"embeddings": [[1.0, 0.0], [0.0, 1.0]]}
        post.return_value = response
        store = self._store("ollama")

        result = store.embed_texts(["first", "second"])

        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(store.embedding_dimension, 2)
        post.assert_called_once_with(
            "http://ollama:11434/api/embed",
            json={"model": "nomic-embed-text:latest", "input": ["first", "second"]},
            timeout=30.0,
        )

    @patch("app.services.vector_store.httpx.post")
    def test_openai_route_remains_supported(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }
        post.return_value = response
        store = self._store("openai")

        result = store.embed_texts(["first", "second"])

        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])
        post.assert_called_once_with(
            "https://api.openai.test/v1/embeddings",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "text-embedding-3-small", "input": ["first", "second"]},
            timeout=30.0,
        )

    @patch("app.services.vector_store.httpx.post")
    def test_embedding_count_mismatch_is_rejected(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"embeddings": [[1.0, 0.0]]}
        post.return_value = response
        store = self._store("ollama")

        with self.assertRaises(VectorStoreUnavailable):
            store.embed_texts(["first", "second"])
        self.assertIn("count mismatch", store.error)

    def test_cache_identity_prevents_cross_model_reuse(self) -> None:
        raw = serialize_embedding([0.25, 0.75], "ollama", "nomic-embed-text:latest")

        self.assertEqual(
            parse_embedding(raw, "ollama", "nomic-embed-text:latest"),
            [0.25, 0.75],
        )
        self.assertIsNone(parse_embedding(raw, "openai", "text-embedding-3-small"))
        self.assertIsNone(parse_embedding("[0.25,0.75]", "ollama", "nomic-embed-text:latest"))

    def test_collection_identity_changes_with_provider_and_model(self) -> None:
        ollama = self._store("ollama")
        openai = self._store("openai")

        self.assertNotEqual(ollama._build_collection_name(), openai._build_collection_name())
        self.assertLessEqual(len(ollama._build_collection_name()), 63)

    def test_optional_vector_failure_keeps_fallback_semantics(self) -> None:
        service = KnowledgeService.__new__(KnowledgeService)
        service.settings = Settings(_env_file=None, knowledge_vector_required=False)
        service.vector_store = Mock(error="")

        service._handle_vector_error("retrieve", RuntimeError("ollama unavailable"))

        service.vector_store.record_error.assert_called_once()

    def test_required_vector_failure_is_raised(self) -> None:
        service = KnowledgeService.__new__(KnowledgeService)
        service.settings = Settings(_env_file=None, knowledge_vector_required=True)
        service.vector_store = Mock(error="")

        with self.assertRaises(RuntimeError):
            service._handle_vector_error("retrieve", RuntimeError("ollama unavailable"))


if __name__ == "__main__":
    unittest.main()
'''


def patch_config() -> None:
    path = "app/core/config.py"
    text = read(path)
    if "ollama_embedding_model:" not in text:
        text = replace_once(
            text,
            '    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"\n',
            '    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"\n'
            '    ollama_embedding_model: str = "nomic-embed-text:latest"\n',
            path,
        )
    if "knowledge_embedding_provider:" not in text:
        text = replace_once(
            text,
            '    chat_history_limit: int = 10\n',
            '    chat_history_limit: int = 10\n'
            '    knowledge_embedding_provider: str = "openai"\n',
            path,
        )
    write(path, text)


def patch_vector_store() -> None:
    path = "app/services/vector_store.py"
    text = read(path)
    if "import hashlib\n" not in text:
        text = replace_once(text, "import shutil\n", "import hashlib\nimport re\nimport shutil\n", path)
    text = replace_block(
        text,
        "class ChromaKnowledgeStore:",
        "ChromaKnowledgeVectorStore = ChromaKnowledgeStore",
        VECTOR_STORE_CLASS,
        path,
    )
    write(path, text)


def patch_knowledge() -> None:
    path = "app/services/knowledge.py"
    text = read(path)
    text = replace_block(text, "    def status(self) -> dict:", "    def rebuild_vector_index", STATUS_METHOD, f"{path}:status")
    text = replace_block(text, "    def rebuild_vector_index", "    def backup_vector_index", REBUILD_METHOD, f"{path}:rebuild")
    text = replace_block(text, "    def _ensure_vector_index", "    def _delete_vector_source", ENSURE_METHOD, f"{path}:ensure")
    text = replace_block(text, "    def _index_vector_chunks", "    def _sync_vector_chunks", INDEX_METHOD, f"{path}:index")
    text = replace_block(text, "    def _sync_vector_chunks", "    def _embeddings_for_chunks", SYNC_METHOD, f"{path}:sync")
    text = replace_block(text, "    def _embeddings_for_chunks", "    def _handle_vector_error", EMBEDDINGS_METHOD, f"{path}:embeddings")
    text = replace_block(text, "    def _handle_vector_error", "    def _expand_best", HANDLE_METHOD, f"{path}:handle")
    text = replace_block(text, "def parse_embedding(", "def tokenize(", PARSE_FUNCTIONS, f"{path}:parse")
    write(path, text)


def patch_env_example() -> None:
    path = ".env.example"
    text = read(path)
    text = set_env_value(text, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
    text = set_env_value(text, "KNOWLEDGE_EMBEDDING_PROVIDER", "ollama")
    write(path, text)


def patch_env() -> None:
    path = ".env"
    text = read(path)
    expected = "OLLAMA_BASE_URL=http://host.docker.internal:11434"
    if expected not in text:
        raise RuntimeError(
            ".env safety check failed: expected the preserved Docker Ollama address "
            f"{expected!r}. No .env changes were made."
        )
    text = set_env_value(text, "KNOWLEDGE_EMBEDDING_PROVIDER", "ollama")
    text = set_env_value(text, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
    write(path, text)


def patch_compose() -> None:
    path = "docker-compose.yml"
    text = read(path)
    if "KNOWLEDGE_EMBEDDING_PROVIDER:" not in text:
        text = replace_once(
            text,
            "      AI_PROVIDER: ${AI_PROVIDER:-ollama}\n",
            "      AI_PROVIDER: ${AI_PROVIDER:-ollama}\n"
            "      KNOWLEDGE_EMBEDDING_PROVIDER: ${KNOWLEDGE_EMBEDDING_PROVIDER:-ollama}\n",
            path,
        )
    if "OPENAI_BASE_URL:" not in text:
        text = replace_once(
            text,
            "      OLLAMA_EMBEDDING_MODEL: ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text:latest}\n",
            "      OLLAMA_EMBEDDING_MODEL: ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text:latest}\n"
            "      OPENAI_BASE_URL: ${OPENAI_BASE_URL:-https://api.openai.com/v1}\n"
            "      OPENAI_API_KEY: ${OPENAI_API_KEY:-}\n"
            "      OPENAI_EMBEDDING_MODEL: ${OPENAI_EMBEDDING_MODEL:-text-embedding-3-small}\n",
            path,
        )
    write(path, text)


def main() -> None:
    required = [
        "app/core/config.py",
        "app/services/vector_store.py",
        "app/services/knowledge.py",
        ".env",
        ".env.example",
        "docker-compose.yml",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    if missing:
        raise SystemExit(f"Run this script from the project root. Missing: {missing}")

    for path in required:
        backup(path)
    backup("tests/test_ollama_embeddings.py")

    patch_config()
    patch_vector_store()
    patch_knowledge()
    patch_env_example()
    patch_env()
    patch_compose()
    write("tests/test_ollama_embeddings.py", TEST_FILE)

    print("Patch applied successfully.")
    print(f"Backup directory: {BACKUP_ROOT}")
    print("Changed files:")
    for path in [
        "app/core/config.py",
        "app/services/vector_store.py",
        "app/services/knowledge.py",
        ".env",
        ".env.example",
        "docker-compose.yml",
        "tests/test_ollama_embeddings.py",
    ]:
        print(f"  - {path}")
    print("README.md, Dockerfile, requirements.txt, routes.py, dtos.py, report.py and harness files were not changed.")


if __name__ == "__main__":
    main()
