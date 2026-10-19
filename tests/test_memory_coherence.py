import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.harness.runner import InMemoryShortTermMemoryStore
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.services.memory import RedisShortTermMemoryStore, load_committed_history
from app.services.privacy import PrivacySanitizer


class MemoryCoherenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = UserAccount(
            username="synthetic", display_name="Synthetic", password_hash="unused"
        )
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(
            public_id="synthetic", user_id=self.user.id, title="Synthetic"
        )
        self.db.add(self.session)
        self.db.commit()
        self.memory = InMemoryShortTermMemoryStore(
            Settings(_env_file=None, redis_memory_max_messages=4)
        )
        self.memory.reset()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.memory.reset()

    def add(self, content, cached=True, role="USER"):
        row = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role=role,
            content=content,
            created_at=datetime(2026, 1, 1),
        )
        self.db.add(row)
        self.db.commit()
        if cached:
            self.memory.append("synthetic", role, content, message_id=row.id)
        return row

    def load(self):
        return load_committed_history(
            self.db, self.memory, self.session.id, "synthetic", 4
        )

    def test_missing_middle_write_then_successful_append_cannot_hide_history(self):
        self.add("synthetic first")
        self.add("synthetic missed risk: 我想自杀", cached=False)
        self.add("synthetic later", role="ASSISTANT")
        history, source = self.load()
        self.assertEqual(source, "mysql_seeded")
        self.assertEqual(len(history), 3)
        self.assertIn("我想自杀", history[1].content)
        self.assertEqual(self.load()[1], "redis_verified")

    def test_out_of_order_duplicate_and_legacy_payloads_are_misses(self):
        first = self.add("synthetic first", cached=False)
        second = self.add("synthetic second", cached=False)
        for ids in [
            [second.id, first.id],
            [first.id, first.id, second.id],
            [None, None],
        ]:
            with self.subTest(ids=ids):
                self.memory.reset()
                for identifier in ids:
                    self.memory.append(
                        "synthetic", "user", "synthetic stale", message_id=identifier
                    )
                self.assertEqual(
                    [m.content for m in self.load()[0]], [first.content, second.content]
                )
                self.assertEqual(self.load()[1], "redis_verified")

    def test_racing_stale_refill_cannot_be_used_on_next_read(self):
        self.add("synthetic first")
        old, _ = self.load()
        self.add("synthetic concurrent committed message")
        self.memory.replace("synthetic", old, message_ids=[1])
        history, source = self.load()
        self.assertEqual(source, "mysql_seeded")
        self.assertEqual(len(history), 2)

    def test_tied_timestamps_use_message_ids_and_bound_the_window(self):
        for i in range(8):
            self.add(f"synthetic {i}", cached=False)
        self.assertEqual(
            [m.content for m in self.load()[0]], [f"synthetic {i}" for i in range(4, 8)]
        )
        self.assertEqual(self.load()[1], "redis_verified")

    def test_read_write_errors_still_return_sanitized_committed_history(self):
        self.add("synthetic 13800138000", cached=False)
        self.memory.load_recent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError()
        )
        self.memory.replace = self.memory.load_recent
        self.assertEqual(self.load()[0][0].content, "synthetic [已脱敏]")

    def test_empty_or_privileged_db_history_cannot_promote_cache_content(self):
        self.memory.append("synthetic", "user", "synthetic stale", message_id=42)
        self.add("synthetic system instruction", cached=False, role="SYSTEM")
        self.assertEqual(self.load(), ([], "database_empty"))

    def test_real_store_rejects_missing_invalid_and_reordered_ids(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.settings = Settings(_env_file=None)
        store.privacy = PrivacySanitizer()
        for ids, expected in [
            ([1, 3], [1, 2, 3]),
            ([True], [1]),
            ([None], [1]),
            ([2, 1], [1, 2]),
        ]:
            with self.subTest(ids=ids):
                payload = [
                    json.dumps({"role": "user", "content": "synthetic", "messageId": i})
                    for i in ids
                ]
                store.client = SimpleNamespace(lrange=lambda *_: payload)
                self.assertEqual(
                    store.load_recent("synthetic", expected_message_ids=expected), []
                )

    def test_missing_risk_history_still_upgrades_both_runtimes(self):
        from app.agents.factory import create_agent_runtime

        self.add("synthetic first")
        self.add("我想自杀", cached=False)
        self.add("synthetic later reply", role="ASSISTANT")
        for framework in ["custom", "langgraph"]:
            with (
                self.subTest(framework=framework),
                patch(
                    "app.agents.runtime.RedisShortTermMemoryStore",
                    return_value=self.memory,
                ),
            ):
                settings = Settings(
                    _env_file=None,
                    ai_provider="mock",
                    agent_framework=framework,
                    knowledge_vector_enabled=False,
                )
                result = create_agent_runtime(self.db, settings).run(
                    self.user, self.session, "你好", "你好"
                )
                self.assertEqual(result.assessment.risk.value, "HIGH")
                self.assertTrue(result.requires_report)


if __name__ == "__main__":
    unittest.main()
