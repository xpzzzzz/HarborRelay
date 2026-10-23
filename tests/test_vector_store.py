import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.core.config import Settings
from app.services.knowledge import KnowledgeService
from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable


class VectorStoreTests(unittest.TestCase):
    def settings(self, **overrides):
        return Settings(_env_file=None, **{"knowledge_vector_enabled": True, **overrides})

    def test_ollama_initializes_without_openai_key(self):
        settings = self.settings(embedding_provider="ollama", openai_api_key="")
        collection = SimpleNamespace(metadata={"embedding_provider": "ollama", "embedding_model": "nomic-embed-text:latest"})
        client = Mock()
        client.get_or_create_collection.return_value = collection
        chroma = SimpleNamespace(PersistentClient=Mock(return_value=client))

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("sys.modules", {"chromadb": chroma}):
                with patch.object(ChromaKnowledgeStore, "_resolve_path", return_value=Path(directory)):
                    store = ChromaKnowledgeStore(settings)

        self.assertTrue(store.can_embed)
        self.assertEqual(store.embedding_model, "nomic-embed-text:latest")

    def test_openai_requires_key_only_for_openai_provider(self):
        settings = self.settings(embedding_provider="openai", openai_api_key="", knowledge_vector_required=False)

        store = ChromaKnowledgeStore(settings)

        self.assertFalse(store.can_embed)
        self.assertIn("OPENAI_API_KEY", store.error)

    def test_required_openai_embeddings_fail_without_key(self):
        settings = self.settings(embedding_provider="openai", openai_api_key="", knowledge_vector_required=True)

        with self.assertRaises(VectorStoreUnavailable):
            ChromaKnowledgeStore(settings)

    def test_collection_from_another_model_is_rejected(self):
        settings = self.settings(embedding_provider="ollama", knowledge_vector_required=True)
        client = Mock()
        client.get_or_create_collection.return_value = SimpleNamespace(metadata={"embedding_model": "text-embedding-3-small"})
        chroma = SimpleNamespace(PersistentClient=Mock(return_value=client))

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("sys.modules", {"chromadb": chroma}):
                with patch.object(ChromaKnowledgeStore, "_resolve_path", return_value=Path(directory)):
                    with self.assertRaisesRegex(VectorStoreUnavailable, "CHROMA_COLLECTION_NAME"):
                        ChromaKnowledgeStore(settings)

    def test_optional_chroma_initialization_failure_falls_back(self):
        settings = self.settings(embedding_provider="ollama", knowledge_vector_required=False)
        chroma = SimpleNamespace(PersistentClient=Mock(side_effect=RuntimeError("storage unavailable")))

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("sys.modules", {"chromadb": chroma}):
                with patch.object(ChromaKnowledgeStore, "_resolve_path", return_value=Path(directory)):
                    store = ChromaKnowledgeStore(settings)

        self.assertFalse(store.can_embed)
        self.assertIn("storage unavailable", store.error)

    def test_ollama_uses_native_batch_embedding_endpoint(self):
        store = self.embedding_store("ollama")
        response = self.response({"embeddings": [[1, 2], [3, 4]]})

        with patch("app.services.vector_store.httpx.post", return_value=response) as post:
            embeddings = store.embed_texts(["校园压力", ""])

        self.assertEqual(embeddings, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/embed")
        self.assertEqual(post.call_args.kwargs["json"]["input"], ["校园压力", " "])
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_openai_response_is_restored_to_input_order(self):
        store = self.embedding_store("openai")
        response = self.response({"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]})

        with patch("app.services.vector_store.httpx.post", return_value=response) as post:
            embeddings = store.embed_texts(["first", "second"])

        self.assertEqual(embeddings, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/embeddings")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_incomplete_embedding_batch_is_rejected(self):
        store = self.embedding_store("ollama")

        with patch("app.services.vector_store.httpx.post", return_value=self.response({"embeddings": [[1, 2]]})):
            with self.assertRaises(VectorStoreUnavailable):
                store.embed_texts(["first", "second"])

    def test_rebuild_replaces_cached_vectors_from_previous_model(self):
        service = KnowledgeService.__new__(KnowledgeService)
        service.vector_store = Mock()
        service.vector_store.embed_texts.return_value = [[3.0, 4.0, 5.0]]
        chunk = SimpleNamespace(content="校园支持", embedding_json="[1.0,2.0]")

        embeddings = service._embeddings_for_chunks([chunk], refresh=True)

        self.assertEqual(embeddings, [[3.0, 4.0, 5.0]])
        service.vector_store.embed_texts.assert_called_once_with(["校园支持"])

    def embedding_store(self, provider):
        store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
        store.settings = self.settings(embedding_provider=provider, openai_api_key="test-key")
        store.embedding_provider = provider
        store.embedding_model = (
            store.settings.ollama_embedding_model if provider == "ollama" else store.settings.openai_embedding_model
        )
        store.can_embed = True
        store.error = ""
        return store

    def response(self, payload):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://embedding.test"))


if __name__ == "__main__":
    unittest.main()
