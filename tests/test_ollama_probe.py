import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.evaluation.ollama_runner import RecordingAi, summarize, validate_local_url
from app.schemas.dtos import AiMessage


class OllamaProbeTests(unittest.TestCase):
    def test_probe_rejects_remote_origins_and_credentials(self):
        for url in [
            "https://localhost:11434",
            "http://example.com",
            "http://user:pass@localhost",
            "http://localhost/api/chat",
            "http://localhost?proxy=remote",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_local_url(url)
        self.assertEqual(
            validate_local_url("http://127.0.0.1:11434/"), "http://127.0.0.1:11434"
        )

    def test_constraint_guard_is_not_counted_as_model_success(self):
        summary = summarize([self.case(attempts=[], modes=["constraint_guard"])])
        self.assertEqual(summary["expected_outcomes"], 1)
        self.assertEqual(summary["model_cases"], 0)
        self.assertEqual(summary["model_only_expected_outcomes"], 0)
        self.assertIsNone(summary["fallback_rate"])

    def test_valid_json_with_fallback_is_not_pure_model_success(self):
        attempt = {
            "json_valid": True,
            "action_valid": False,
            "decoder_schema_valid": False,
            "error": None,
            "call": {},
        }
        summary = summarize(
            [self.case(attempts=[attempt], modes=["deterministic_fallback"])]
        )
        self.assertEqual(summary["json_valid"], 1)
        self.assertEqual(summary["action_valid"], 0)
        self.assertEqual(summary["expected_outcomes"], 1)
        self.assertEqual(summary["model_only_expected_outcomes"], 0)
        self.assertEqual(summary["fallback_rate"], 1)

    def test_legacy_thinking_markup_is_not_saved_or_parsed_into_action(self):
        ai = RecordingAi(Settings(_env_file=None, ai_provider="ollama"))
        payload = '{"task":{"constraints":{}},"observations":[]}'
        raw = '<think>private internal text</think>{"kind":"finish"}'
        with patch.object(ai, "_ollama", return_value=raw):
            returned = ai.complete([AiMessage(role="user", content=payload)])
        self.assertEqual(returned, raw)
        self.assertFalse(ai.attempts[0]["json_valid"])
        self.assertTrue(ai.attempts[0]["thinking_markup_omitted"])
        self.assertNotIn("private internal text", str(ai.attempts))

    @staticmethod
    def case(*, attempts, modes):
        return {
            "id": "available",
            "success": True,
            "attempts": attempts,
            "result": {"planner_modes": modes},
            "unauthorized_success": False,
            "elapsed_ms": 1,
            "fault_exercised": False,
        }


if __name__ == "__main__":
    unittest.main()
