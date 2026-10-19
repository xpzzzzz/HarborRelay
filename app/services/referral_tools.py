"""Pure local demonstration tools. No DB session, network, booking or notification."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.agents.contracts import StrictModel, ToolObservation
from app.services.privacy import PrivacySanitizer


class SearchFilters(StrictModel):
    channel: Literal["in_person", "remote", "any"] = "in_person"
    private: bool = True


class AvailabilityArgs(StrictModel):
    resource_id: str = Field(min_length=1, max_length=80)
    window: str = Field(min_length=1, max_length=80)


class SummaryArgs(StrictModel):
    resource_ids: list[str] = Field(min_length=1, max_length=3)


class Resource(StrictModel):
    id: str
    name: str
    channel: Literal["in_person", "remote"]
    private: bool
    privacy_policy: str
    windows: list[str]
    availability: dict[str, Literal["available", "unavailable", "unknown"]]
    source_ref: str = Field(pattern=r"^fixture://support-resources/")
    simulated: Literal[True] = True

    def public(self):
        return self.model_dump(exclude={"availability"})


TOOL_SCHEMAS = {
    "search_support_resources": SearchFilters,
    "get_resource_availability": AvailabilityArgs,
    "prepare_referral_summary": SummaryArgs,
}


class ReferralTools:
    def __init__(self, resources: list[Resource] | None = None):
        if resources is None:
            path = (
                Path(__file__).resolve().parents[1]
                / "fixtures"
                / "support_resources.json"
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("simulated") is not True:
                raise ValueError("Only simulated fixtures are enabled")
            resources = [Resource.model_validate(item) for item in data["resources"]]
        self.resources = {r.id: r for r in resources}

    def call(self, tool: str, arguments: dict) -> ToolObservation:
        if tool not in TOOL_SCHEMAS:
            raise PermissionError("Tool not allowed")
        args = TOOL_SCHEMAS[tool].model_validate(arguments)
        if tool == "search_support_resources":
            matching_channel = [
                r
                for r in self.resources.values()
                if args.channel == "any" or r.channel == args.channel
            ]
            matches = [r for r in matching_channel if not args.private or r.private]
            return ToolObservation(
                tool=tool,
                status="success" if matches else "empty",
                data={
                    "resources": [r.public() for r in matches],
                    "simulated": True,
                    "privacy_conflict": bool(
                        matching_channel and not matches and args.private
                    ),
                },
                source_refs=[r.source_ref for r in matches]
                or ["fixture://support-resources/v1"],
            )
        if tool == "get_resource_availability":
            resource = self.resources.get(args.resource_id)
            if resource is None:
                raise ValueError("Unknown resource")
            availability = resource.availability.get(args.window, "unknown")
            return ToolObservation(
                tool=tool,
                status={
                    "available": "success",
                    "unavailable": "empty",
                    "unknown": "unknown",
                }[availability],
                data={
                    "resource_id": resource.id,
                    "window": args.window,
                    "availability": availability,
                    "simulated": True,
                },
                source_refs=[resource.source_ref],
            )
        resources = [self.resources[resource_id] for resource_id in args.resource_ids]
        draft = PrivacySanitizer().sanitize(
            "模拟交接草稿：希望了解 "
            + "、".join(r.name for r in resources)
            + "。仅供用户编辑，尚未提交或发送。"
        )
        return ToolObservation(
            tool=tool,
            status="success",
            data={"draft": draft, "resource_ids": args.resource_ids, "simulated": True},
            source_refs=[r.source_ref for r in resources],
        )
