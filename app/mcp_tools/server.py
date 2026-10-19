import os

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import PsychologicalReport
from app.services.tools import ToolOrchestrationService
from app.services.tool_governance import ToolPolicyRegistry
from app.models.entities import ToolAuditRecord

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("mindbridge-python-tools")


def _require_backend_scope(report_id: int) -> None:
    # Private stdio adapter only. This is not authentication for a reusable MCP server.
    if os.environ.get("HARBORRELAY_MCP_REPORT_ID") != str(report_id):
        raise PermissionError("MCP adapter requires a backend-scoped report; external clients are disabled")


def _authorize_report(db, name: str, report) -> None:
    allowed, reason, policy = ToolPolicyRegistry.authorize(name, report)
    db.add(ToolAuditRecord(report_id=report.id if report else None, tool_name=name,
                           policy=policy.name if policy else "unknown", allowed=allowed,
                           status="AUTHORIZED" if allowed else "BLOCKED", reason=reason, payload="{}"))
    db.commit()
    if not allowed:
        raise PermissionError("Tool policy denied execution")


@mcp.tool()
def mindbridge_excel_report(report_id: int) -> str:
    """Write one psychological risk report into the HarborRelay Excel ledger."""
    _require_backend_scope(report_id)
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        _authorize_report(db, "EXCEL_REPORT", report)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).write_excel(report)
        return f"success: {record.file_path}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_case_create(report_id: int) -> str:
    """Create or return the active HarborRelay risk case for one psychological report."""
    _require_backend_scope(report_id)
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        _authorize_report(db, "CASE_CREATE", report)
        if report is None:
            return f"report {report_id} not found"
        case = ToolOrchestrationService(db, get_settings()).create_case(report)
        return f"success: caseId={case.id}, reportId={case.report_id}, status={case.status}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_send(case_id: int) -> str:
    """Send or record the counselor alert for one HarborRelay risk case."""
    if not os.environ.get("HARBORRELAY_MCP_REPORT_ID"):
        raise PermissionError("External MCP clients are disabled")
    db = SessionLocal()
    try:
        from app.models.entities import RiskCase

        case = db.get(RiskCase, case_id)
        if case is None:
            raise RuntimeError("Case not found")
        _require_backend_scope(case.report_id)
        _authorize_report(db, "ALERT_SEND", db.get(PsychologicalReport, case.report_id))
        record = ToolOrchestrationService(db, get_settings()).send_case_alert(case)
        if record.status != "SUCCESS":
            raise RuntimeError("Alert delivery failed; inspect the persisted alert record")
        return f"{record.status}: caseId={case_id}, {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_ack(case_id: int, actor: str, note: str = "") -> str:
    """Mark a HarborRelay risk case as acknowledged by a counselor or administrator."""
    raise PermissionError("Use authenticated case API; caller-provided actor is not an identity")


@mcp.tool()
def mindbridge_case_note_add(case_id: int, actor: str, note: str) -> str:
    """Append a follow-up note to a HarborRelay risk case."""
    raise PermissionError("Use authenticated case API; caller-provided actor is not an identity")


@mcp.tool()
def mindbridge_alert_notify(report_id: int) -> str:
    """Send a high-risk alert email and record the notification result for one psychological report."""
    _require_backend_scope(report_id)
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        _authorize_report(db, "RISK_ALERT", report)
        if report is None:
            return f"report {report_id} not found"
        record = ToolOrchestrationService(db, get_settings()).notify(report)
        if record.status != "SUCCESS":
            raise RuntimeError("Alert delivery failed; inspect the persisted alert record")
        return f"{record.status}: {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
