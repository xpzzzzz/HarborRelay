from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.core.config import Settings
from app.models.entities import KnowledgeChunk


PRIMARY_RETRIEVAL_LABEL = "Chroma vector + BM25 hybrid + local reranker"
FALLBACK_RETRIEVAL_LABEL = "local BM25 + hybrid_score reranker"


class VectorStoreUnavailable(RuntimeError):
    pass


@dataclass
class VectorSearchHit:
    chunk_id: int | None
    source: str
    source_index: int
    content: str
    score: float


class ChromaKnowledgeStore:
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
            from chromadb.config import Settings as ChromaSettings
        except ImportError as exc:
            self._set_unavailable(
                f"chromadb is not installed; vector retrieval is unavailable. "
                f"Falling back to {FALLBACK_RETRIEVAL_LABEL}.",
                exc,
            )
            return

        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(
                path=str(self.persist_dir),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=None,
                metadata=self._collection_metadata(),
            )
            metadata = self.collection.metadata or {}
            stored_dimension = metadata.get("embedding_dimension")
            if isinstance(stored_dimension, int) and stored_dimension > 0:
                self.embedding_dimension = stored_dimension
            else:
                self.embedding_dimension = self._infer_collection_dimension()
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

ChromaKnowledgeVectorStore = ChromaKnowledgeStore
