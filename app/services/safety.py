"""Deterministic output boundary. This is a product guard, not clinical assessment."""
from dataclasses import dataclass

from app.core.enums import RiskLevel


HIGH_RISK_RESPONSE = (
    "听起来你现在很痛苦。现在先把安全放在第一位：请尽快联系身边可信任的人，"
    "让对方陪着你，并联系学校心理中心、辅导员或当地紧急服务获取人工支持。"
    "如果可能，请先去有人陪伴的地方，远离可能伤害自己的物品。"
    "你现在身边有可以马上联系、陪着你的人吗？"
)


@dataclass(frozen=True)
class FinalResponseReview:
    approved: bool
    reason: str
    mode: str


def review_final_response(text: str, risk: RiskLevel) -> FinalResponseReview:
    # High-risk output is an allowlisted final text, never an unreviewed model draft.
    if risk == RiskLevel.HIGH:
        approved = text == HIGH_RISK_RESPONSE
        return FinalResponseReview(approved, "approved_template" if approved else "unapproved_high_risk_text", "safety_template")
    return FinalResponseReview(bool(text.strip()), "nonempty_text", "model_stream")
