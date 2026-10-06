from __future__ import annotations

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
            timeout=store.settings.embedding_timeout_seconds,
        )

    def test_record_dimension_does_not_modify_chroma_metadata(self) -> None:
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
            timeout=store.settings.embedding_timeout_seconds,
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
