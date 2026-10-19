import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.enums import RiskCaseStatus
from app.models.entities import CaseNote, RiskCase, UserAccount
from app.services.tools import ToolOrchestrationService


class CaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.actor = UserAccount(username="counselor", display_name="Counselor", password_hash="unused", roles_csv="ROLE_COUNSELOR")
        self.db.add(self.actor)
        self.case = RiskCase(
            report_id=1,
            risk_level="HIGH",
            status=RiskCaseStatus.OPEN.value,
            owner="unassigned",
            summary="需要跟进",
            handoff_summary="交接摘要",
        )
        self.db.add(self.case)
        self.db.commit()
        self.service = ToolOrchestrationService(self.db, None)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_acknowledge_follow_up_and_close_preserve_history(self):
        case = self.service.acknowledge_case(self.case.id, self.actor, "已接手")
        self.assertEqual(case.status, RiskCaseStatus.ACKNOWLEDGED.value)
        self.assertEqual(case.owner, "counselor")
        self.service.add_case_note(case.id, self.actor, "完成初次联系")
        self.service.close_case(case.id, self.actor, "已转交线下咨询")

        notes = self.db.query(CaseNote).filter(CaseNote.case_id == case.id).order_by(CaseNote.id).all()
        self.assertEqual([row.note for row in notes], ["已接手", "完成初次联系", "关闭个案：已转交线下咨询"])
        self.assertEqual(self.db.get(RiskCase, case.id).status, RiskCaseStatus.CLOSED.value)
        with self.assertRaises(ValueError):
            self.service.add_case_note(case.id, self.actor, "不应再修改")

    def test_invalid_transitions_do_not_create_notes(self):
        with self.assertRaises(ValueError):
            self.service.close_case(self.case.id, self.actor, "未接手")
        with self.assertRaises(ValueError):
            self.service.add_case_note(self.case.id, self.actor, "未接手")
        self.service.acknowledge_case(self.case.id, self.actor)
        with self.assertRaises(ValueError):
            self.service.acknowledge_case(self.case.id, self.actor)
        with self.assertRaises(ValueError):
            self.service.close_case(self.case.id, self.actor, "  ")
        self.assertEqual(self.db.query(CaseNote).count(), 1)


if __name__ == "__main__":
    unittest.main()
