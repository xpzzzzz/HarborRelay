import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.evaluation.infrastructure_probe import scope
from app.evaluation.infrastructure_runner import owned_container
from app.services.knowledge import (
    KnowledgeService,
    parse_embedding,
    serialize_embedding,
)
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.services.vector_store import ChromaKnowledgeStore, VectorStoreUnavailable


class InfrastructureContractsTests(unittest.TestCase):
    def test_untrusted_cache_cannot_add_system_roles_or_discard_valid_siblings(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.settings = Settings(_env_file=None)
        store.privacy = PrivacySanitizer()
        rows = [
            "invalid",
            "[]",
            "null",
            json.dumps({"role": "system", "content": "synthetic instruction"}),
            json.dumps({"role": "user", "content": {"bad": "shape"}}),
            json.dumps({"role": "user", "content": "synthetic 13800138000"}),
        ]
        store.client = SimpleNamespace(lrange=lambda *_: rows)
        result = store.load_recent("synthetic")
        self.assertEqual(
            [(m.role, m.content) for m in result], [("user", "synthetic [已脱敏]")]
        )

    def test_startup_ping_failure_does_not_permanently_disable_cache(self):
        client = Mock()
        client.ping.side_effect = ConnectionError("synthetic startup outage")
        client.lrange.return_value = ['{"role":"user","content":"synthetic recovered"}']
        redis_module = SimpleNamespace(
            Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client)
        )
        with (
            patch("app.services.memory.import_module", return_value=redis_module),
            patch("app.services.memory.monotonic", side_effect=[0.0, 1.0, 3.0]),
        ):
            store = RedisShortTermMemoryStore(Settings(_env_file=None))
            self.assertEqual(store.load_recent("synthetic"), [])
            self.assertEqual(
                store.load_recent("synthetic")[0].content, "synthetic recovered"
            )
            client.lrange.assert_called_once()

    def test_status_does_not_retain_error_after_provider_recovers(self):
        class RecoveringStore:
            can_embed = True
            error = "synthetic previous failure"
            embedding_provider = "ollama"
            embedding_model = "synthetic"
            embedding_dimension = 768
            collection_name = "synthetic"

            @property
            def available(self):
                return not self.error

            def check_provider_available(self):
                self.error = ""
                return True

            def count(self):
                return 3

        service = KnowledgeService.__new__(KnowledgeService)
        service.settings = Settings(_env_file=None)
        service.vector_store = RecoveringStore()
        service.count = lambda: 3
        result = service.status()
        self.assertTrue(result["vectorAvailable"])
        self.assertEqual(result["vectorError"], "")

    def test_required_vectors_cannot_be_silently_disabled(self):
        with self.assertRaises(VectorStoreUnavailable):
            ChromaKnowledgeStore(
                Settings(
                    _env_file=None,
                    knowledge_vector_enabled=False,
                    knowledge_vector_required=True,
                )
            )

    def test_non_finite_provider_and_cached_embeddings_are_rejected(self):
        for value in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(value=value):
                store = ChromaKnowledgeStore.__new__(ChromaKnowledgeStore)
                store.embedding_dimension = None
                with self.assertRaises(VectorStoreUnavailable):
                    store._validate_embeddings([[value, 1.0]], 1)
                self.assertIsNone(store.embedding_dimension)
                raw = json.dumps(
                    {
                        "provider": "ollama",
                        "model": "synthetic",
                        "dimension": 2,
                        "vector": [value, 1.0],
                    }
                )
                self.assertIsNone(parse_embedding(raw, "ollama", "synthetic"))
                with self.assertRaises(ValueError):
                    serialize_embedding([value, 1.0], "ollama", "synthetic")

    def test_probe_refuses_host_scope_or_unowned_container(self):
        with patch.dict("os.environ", {"HARBORRELAY_INFRA_RUN": "different"}):
            with self.assertRaises(RuntimeError):
                scope("synthetic")
        with patch(
            "app.evaluation.infrastructure_runner.subprocess.check_output",
            return_value="another-run",
        ):
            with self.assertRaises(RuntimeError):
                owned_container(["docker"], "identifier", "our-run")


if __name__ == "__main__":
    unittest.main()
