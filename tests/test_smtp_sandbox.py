import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.evaluation.smtp_sandbox import smtp_sink
from app.models.entities import (
    AlertRecord,
    ChatSession,
    PsychologicalReport,
    ToolAuditRecord,
    ToolJob,
    UserAccount,
)
from app.services.tool_queue import ToolQueueService, ToolQueueWorker
from app.services.tools import ToolOrchestrationService


class SmtpSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite:///" + str(Path(self.temp.name) / "synthetic.sqlite3")
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        user = UserAccount(
            username="synthetic", display_name="Synthetic", password_hash="unused"
        )
        self.db.add(user)
        self.db.flush()
        session = ChatSession(public_id="synthetic", user_id=user.id, title="Synthetic")
        self.db.add(session)
        self.db.flush()
        self.report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="synthetic",
            intent="RISK",
            emotion="ANXIETY",
            emotion_score=5,
            risk_level="HIGH",
            confidence=1,
            summary="Synthetic sandbox alert",
        )
        self.db.add(self.report)
        self.db.commit()
        self.settings = Settings(
            _env_file=None,
            alert_email_delivery_mode="smtp",
            smtp_host="127.0.0.1",
            smtp_use_tls=False,
            smtp_use_ssl=False,
            smtp_username="",
            smtp_password="",
            smtp_timeout_seconds=1,
            alert_email_from="sender@synthetic.invalid",
            alert_email_to="accept@synthetic.invalid",
            excel_path=str(Path(self.temp.name) / "synthetic.xlsx"),
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_success_is_deduplicated_and_case_advances(self):
        with smtp_sink() as sink:
            self.settings.smtp_port = sink.port
            tools = ToolOrchestrationService(self.db, self.settings)
            case = tools.create_case(self.report)
            first = tools.send_case_alert(case)
            second = tools.send_case_alert(case)
            self.assertEqual(first.status, "SUCCESS")
            self.assertEqual(first.id, second.id)
            self.assertEqual(case.status, "ALERT_SENT")
            self.assertEqual(len(sink.messages), 1)
            self.assertIn(b"reportId=", sink.messages[0])

    def test_all_partial_and_data_refusals_never_mark_complete_success(self):
        for mode in ["reject", "partial", "data_error"]:
            with self.subTest(mode=mode), smtp_sink() as sink:
                sink.mode = mode
                self.settings.smtp_port = sink.port
                self.settings.alert_email_to = (
                    "accept@synthetic.invalid,reject@synthetic.invalid"
                )
                tools = ToolOrchestrationService(self.db, self.settings)
                case = tools.create_case(self.report)
                record = tools.send_case_alert(case)
                self.assertEqual(record.status, "FAILED")
                self.assertEqual(case.status, "OPEN")
                self.assertEqual(
                    self.db.query(AlertRecord).filter_by(status="SUCCESS").count(), 0
                )

    def test_tls_and_auth_fail_closed_before_data(self):
        for mode in ["tls", "auth"]:
            with self.subTest(mode=mode), smtp_sink() as sink:
                self.settings.smtp_port = sink.port
                self.settings.smtp_use_tls = mode == "tls"
                self.settings.smtp_username = "synthetic" if mode == "auth" else ""
                record = ToolOrchestrationService(self.db, self.settings).notify(
                    self.report
                )
                self.assertEqual(record.status, "FAILED")
                self.assertNotIn("DATA", sink.commands)
                self.assertEqual(sink.messages, [])

    def test_ambiguous_ack_is_failure_and_retry_can_duplicate_delivery(self):
        with smtp_sink() as sink:
            self.settings.smtp_port = sink.port
            sink.mode = "drop_after_data"
            tools = ToolOrchestrationService(self.db, self.settings)
            first = tools.notify(self.report)
            self.assertEqual(first.status, "FAILED")
            self.assertEqual(len(sink.messages), 1)
            sink.mode = "healthy"
            second = tools.notify(self.report)
            self.assertEqual(second.status, "SUCCESS")
            self.assertEqual(
                len(sink.messages), 2
            )  # Documented at-least-once limitation.

    def test_governed_queue_retries_transient_smtp_failure(self):
        jobs = ToolQueueService(self.db, self.settings).enqueue_report(
            self.report.id, "HIGH"
        )
        case_job = next(job for job in jobs if job.kind == "CASE_CREATE")
        alert_job = next(job for job in jobs if job.kind == "ALERT_SEND")
        factory = sessionmaker(self.engine)
        worker = ToolQueueWorker(self.settings)
        try:
            with (
                smtp_sink() as sink,
                patch("app.services.tool_queue.SessionLocal", factory),
            ):
                self.settings.smtp_port = sink.port
                case_job.status = "RUNNING"
                self.db.commit()
                worker._run_job(case_job.id)
                sink.mode = "data_error"
                alert_job.status = "RUNNING"
                self.db.commit()
                worker._run_job(alert_job.id)
                self.db.expire_all()
                self.assertEqual(self.db.get(ToolJob, alert_job.id).status, "PENDING")
                sink.mode = "healthy"
                alert_job.status = "RUNNING"
                alert_job.run_after = datetime.utcnow()
                self.db.commit()
                worker._run_job(alert_job.id)
                self.db.expire_all()
                self.assertEqual(self.db.get(ToolJob, alert_job.id).status, "SUCCESS")
                self.assertEqual(self.db.get(ToolJob, alert_job.id).attempts, 2)
                audits = (
                    self.db.query(ToolAuditRecord)
                    .filter_by(tool_name="ALERT_SEND")
                    .all()
                )
                self.assertEqual([a.status for a in audits], ["FAILED", "SUCCESS"])
                self.assertEqual(len(sink.messages), 1)
        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
