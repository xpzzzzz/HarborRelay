import base64
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_safety_boundaries as fixtures
from app.api.routes import router
from app.core.database import get_db
from app.core.security import hash_password
from app.models.entities import CaseNote, ChatSession, RiskCase


class ApiAuthorizationTests(unittest.TestCase):
    setUp = fixtures.SafetyBoundariesTests.setUp
    tearDown = fixtures.SafetyBoundariesTests.tearDown

    def client(self):
        for user in [self.user, self.staff, self.other]:
            user.password_hash = hash_password("synthetic-password")
        self.db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        return TestClient(app)

    @staticmethod
    def auth(username):
        token = base64.b64encode(f"{username}:synthetic-password".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_actual_api_rejects_student_and_other_owner_and_derives_actor(self):
        own = RiskCase(
            report_id=1,
            risk_level="HIGH",
            status="OPEN",
            owner="counselor",
            summary="synthetic",
        )
        other = RiskCase(
            report_id=2,
            risk_level="HIGH",
            status="OPEN",
            owner="other",
            summary="synthetic",
        )
        self.db.add_all([own, other])
        self.db.commit()
        client = self.client()
        with patch("app.api.routes.get_settings", return_value=self.settings):
            self.assertEqual(
                client.get(
                    "/api/admin/cases", headers=self.auth("student")
                ).status_code,
                403,
            )
            cases = client.get("/api/admin/cases", headers=self.auth("counselor"))
            self.assertEqual([c["id"] for c in cases.json()], [own.id])
            self.assertEqual(
                client.get(
                    f"/api/admin/cases/{other.id}/notes", headers=self.auth("counselor")
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    f"/api/admin/cases/{other.id}/acknowledge",
                    headers=self.auth("counselor"),
                    json={},
                ).status_code,
                403,
            )
            response = client.post(
                f"/api/admin/cases/{own.id}/acknowledge",
                headers=self.auth("counselor"),
                json={"actor": "other", "note": "synthetic"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["acknowledgedBy"], "counselor")
            self.assertEqual(self.db.query(CaseNote).one().actor, "counselor")

    def test_foreign_session_is_rejected_before_sse_headers(self):
        foreign = ChatSession(
            public_id="foreign", user_id=self.other.id, title="synthetic"
        )
        self.db.add(foreign)
        self.db.commit()
        client = self.client()
        response = client.post(
            "/api/chat/stream",
            headers=self.auth("student"),
            json={"message": "hello", "sessionId": "foreign"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("event: token", response.text)


if __name__ == "__main__":
    unittest.main()
