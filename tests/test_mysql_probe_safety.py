import json
import unittest
from unittest.mock import patch
from app.evaluation.mysql_runner import local_docker_context


class MysqlProbeSafetyTests(unittest.TestCase):
    def test_remote_docker_context_is_rejected(self):
        with (
            patch(
                "app.evaluation.mysql_runner.subprocess.check_output",
                side_effect=["remote\n", json.dumps("ssh://example.test")],
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            with self.assertRaises(RuntimeError):
                local_docker_context()

    def test_docker_host_override_is_rejected(self):
        with (
            patch(
                "app.evaluation.mysql_runner.subprocess.check_output",
                side_effect=["desktop\n", json.dumps("npipe:////./pipe/test")],
            ),
            patch.dict("os.environ", {"DOCKER_HOST": "tcp://example.test"}, clear=True),
        ):
            with self.assertRaises(RuntimeError):
                local_docker_context()

    def test_validated_context_is_pinned_for_container_operations(self):
        with (
            patch(
                "app.evaluation.mysql_runner.subprocess.check_output",
                side_effect=["desktop\n", json.dumps("npipe:////./pipe/test")],
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            self.assertEqual(local_docker_context(), ["docker", "--context", "desktop"])


if __name__ == "__main__":
    unittest.main()
