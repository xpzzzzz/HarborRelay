from __future__ import annotations

from dataclasses import dataclass, field, fields
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.assessment import PsychologicalAssessmentService, PsychologyAssessment
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore, compact_history_for_prompt
from app.services.skills import MindBridgeSkillLibrary
from app.services.privacy import PrivacySanitizer
from app.agents.contracts import Artifact, TurnState, TaskSpec
from app.agents.referral import ReferralAgent, extract_constraints, is_referral_request


GENERAL_TASK_WORDS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "总结", "解释",
    "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询", "天气", "路线",
]


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str
    task_id: str | None = None
    observation_status: str = "success"
    artifact_ref: str | None = None
    model: str | None = None
    latency_ms: float = 0
    error: str | None = None
    stop_reason: str | None = None
    planner_mode: str | None = None
    tool_arguments: dict | None = None
    source_refs: list[str] = field(default_factory=list)


@dataclass
class AgentContext:
    user: UserAccount
    session: ChatSession
    original_input: str
    model_input: str
    prechecked: bool = False
    memory_loaded: bool = False
    intent_routed: bool = False
    knowledge_handled: bool = False
    risk_assessed: bool = False
    response_planned: bool = False
    finished: bool = False
    memory_brief: str = "无相关历史记忆。"
    intent: IntentType | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    assessment: PsychologyAssessment | None = None
    knowledge_query: str = ""
    retrieved_knowledge: list[SearchResult] = field(default_factory=list)
    model_history: list[AiMessage] = field(default_factory=list)
    response_messages: list[AiMessage] = field(default_factory=list)
    response_agent: str = ""
    response_plan: str = ""
    steps: list[AgentStep] = field(default_factory=list)


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    memory_brief: str
    artifacts: list[Artifact] = field(default_factory=list)
    turn_id: str = ""
    stop_reason: str = "completed"
    model_calls: list[dict] = field(default_factory=list)
    referral_result: object | None = None

    @property
    def requires_report(self) -> bool:
        return self.intent != IntentType.CHAT or self.risk_level != RiskLevel.LOW


class AgentRuntimeService:
    max_steps = 8

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self._knowledge = None
        self._memory = None
        self.assessment = PsychologicalAssessmentService(self.ai)

    @property
    def knowledge(self):
        if self._knowledge is None:
            self._knowledge = KnowledgeService(self.db,self.settings)
        return self._knowledge

    @property
    def memory(self):
        if self._memory is None:
            self._memory = RedisShortTermMemoryStore(self.settings)
        return self._memory

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        state = self.initial_state(user, session, model_input)
        while (operation := self.select_next_operation(state)) != "end":
            state = {**state, **self.execute_step(state, operation)}
        return self.result_from_state(state)

    def initial_state(self, user, session, model_input: str) -> TurnState:
        turn_id = uuid4().hex
        context = AgentContext(user=user, session=session, original_input="", model_input=PrivacySanitizer().sanitize(model_input))
        state = self._context_values(context)
        return {**state, "turn_id": turn_id, "input_ref": f"turn:{turn_id}:input",
                "user_id": user.id, "display_name": user.display_name, "session_id": session.id,
                "session_public_id": session.public_id, "artifacts": [], "task": None,
                "observations": [], "stop_reason": "completed"}

    @staticmethod
    def _context_values(context):
        return {f.name: deepcopy(getattr(context, f.name)) for f in fields(AgentContext)
                if f.name not in {"user", "session", "original_input"}}

    def select_next_operation(self, state: TurnState) -> str:
        if state["finished"]:
            return "end"
        for key, name in [("prechecked", "precheck"), ("memory_loaded", "memory"),
                          ("risk_assessed", "risk_guardian"), ("intent_routed", "supervisor")]:
            if not state[key]:
                return name
        if self.settings.referral_enabled and state["risk_level"] != RiskLevel.HIGH and is_referral_request(self._referral_input(state)):
            return "referral"
        if state["intent"] == IntentType.CHAT:
            return "companion"
        if not state["knowledge_handled"]:
            return "knowledge"
        return "counselor"

    def execute_step(self, state: TurnState, operation: str) -> dict:
        if len(state["steps"]) >= self.max_steps:
            if not state["risk_assessed"]:
                raise RuntimeError("Safety assessment budget exhausted")
            return {"finished": True, "stop_reason": "step_budget"}
        if operation == "referral":
            return self._execute_referral(state)
        context = AgentContext(user=SimpleNamespace(id=state["user_id"], display_name=state["display_name"]),
                               session=SimpleNamespace(id=state["session_id"], public_id=state["session_public_id"]),
                               original_input=state["model_input"], model_input=state["model_input"])
        for key in self._context_values(context):
            if key in state:
                setattr(context, key, deepcopy(state[key]))
        started = perf_counter()
        if not getattr(self, f"{operation}_agent")(len(context.steps) + 1, context):
            raise RuntimeError(f"Invalid transition: {operation}")
        artifacts = list(state["artifacts"])
        payload = None
        if operation == "supervisor":
            payload = ("intent", {"intent": context.intent})
        elif operation == "risk_guardian":
            payload = ("risk_assessment", {"risk_level": context.risk_level, "emotion": context.assessment.emotion.value,
                                           "confidence": context.assessment.confidence})
        elif operation == "knowledge":
            payload = ("retrieval_evidence", {"items": [{"source": r.source, "chunk_id": r.chunk_id, "score": r.score}
                                                        for r in context.retrieved_knowledge]})
        if payload:
            refs = ([f"knowledge:{r.source}:{r.chunk_id}" for r in context.retrieved_knowledge]
                    if operation == "knowledge" and context.retrieved_knowledge else [state["input_ref"]])
            artifacts.append(Artifact(kind=payload[0], producer=operation, task_id=state["turn_id"],
                                      payload=payload[1], source_refs=refs))
        step = context.steps[-1]
        step.task_id = state["turn_id"]
        step.latency_ms = (perf_counter() - started) * 1000
        step.artifact_ref = artifacts[-1].id if payload else None
        step.model = self.settings.ollama_model if self.settings.ai_provider == "ollama" else self.settings.openai_model if self.settings.ai_provider == "openai" else "mock"
        step.stop_reason = "completed" if context.finished else None
        updates = {**self._context_values(context), "artifacts": artifacts}
        return {key: value for key, value in updates.items() if value != state.get(key)}

    @staticmethod
    def _referral_input(state):
        text = state["model_input"]
        if not is_referral_request(text) and extract_constraints(text).get("window"):
            previous = [m.content for m in state["model_history"][:-1] if m.role.lower() == "user"]
            if previous and is_referral_request(previous[-1]):
                text = previous[-1] + "\n" + text
        return text

    def _execute_referral(self, state):
        task = TaskSpec(constraints=extract_constraints(self._referral_input(state)),
                        max_steps=self.settings.referral_max_steps, max_tool_calls=self.settings.referral_max_tool_calls,
                        timeout_seconds=self.settings.referral_timeout_seconds)
        from app.agents.factory import langgraph_available
        result = ReferralAgent(self.ai, planner=self.settings.referral_planner,
                               use_graph=langgraph_available(), risk_check=lambda: state["risk_level"] == RiskLevel.HIGH).run(task)
        artifacts = list(state["artifacts"])
        artifact = Artifact(kind="referral_options", producer="referral", task_id=task.id,
                            payload={"resource_ids":[r["id"] for r in result.options], "simulated":True, "stop_reason":result.stop_reason},
                            source_refs=result.source_refs or [state["input_ref"]])
        artifacts.append(artifact)
        steps = list(state["steps"])
        for item in result.steps:
            steps.append(AgentStep(len(steps)+1, "ReferralAgent", item["action"], item.get("tool") or "structured_decision",
                                   task_id=task.id, observation_status=item["observation_status"], artifact_ref=artifact.id,
                                   latency_ms=item["latency_ms"], error=item.get("error"), planner_mode=item.get("planner_mode"),
                                   tool_arguments=item.get("tool_arguments"), source_refs=item.get("source_refs",[])))
        steps.append(AgentStep(len(steps)+1,"ReferralAgent","STOP",result.stop_reason,task_id=task.id,
                               artifact_ref=artifact.id,stop_reason=result.stop_reason))
        updates = {"task":task,"referral_result":result,"observations":result.observations,"artifacts":artifacts,
                   "steps":steps,"finished":True,"response_planned":True,"response_messages":[],"stop_reason":result.stop_reason}
        if result.stop_reason == "risk_escalated":
            updates.update(risk_level=RiskLevel.HIGH,intent=IntentType.RISK,
                           assessment=PsychologyAssessment(EmotionLabel.HIGH_RISK,4,RiskLevel.HIGH,0,"探索过程中风险升级"))
            artifacts.extend([
                Artifact(kind="risk_assessment",producer="safety_boundary",task_id=task.id,
                         payload={"risk_level":"HIGH","emotion":"HIGH_RISK","confidence":0},source_refs=[artifact.id]),
                Artifact(kind="intent",producer="safety_boundary",task_id=task.id,payload={"intent":"RISK"},source_refs=[artifact.id])])
        return updates

    def result_from_state(self, state: TurnState) -> AgentRunResult:
        if not state["risk_assessed"] or not state["response_planned"]:
            raise RuntimeError("Turn ended without required safety/response work")
        return AgentRunResult(
            intent=state["intent"] or IntentType.CHAT, risk_level=state["risk_level"],
            assessment=state["assessment"], retrieved_knowledge=state["retrieved_knowledge"],
            response_messages=state["response_messages"], steps=state["steps"], memory_brief=state["memory_brief"],
            artifacts=state["artifacts"], turn_id=state["turn_id"], stop_reason=state["stop_reason"],
            model_calls=list(self.ai.calls),
            referral_result=state.get("referral_result"),
        )

    def precheck_agent(self, step: int, context: AgentContext) -> bool:
        if context.prechecked:
            return False
        if has_high_risk_signal(context.original_input):
            context.risk_level = RiskLevel.HIGH
        context.prechecked = True
        context.steps.append(AgentStep(step, "SafetyPrecheck", "RULE_PRECHECK", f"risk={context.risk_level.value}"))
        return True

    def memory_agent(self, step: int, context: AgentContext) -> bool:
        if context.memory_loaded:
            return False
        if context.risk_level == RiskLevel.HIGH:
            context.model_history = [AiMessage(role="user",content=context.model_input)]
            context.memory_brief = "规则已命中，优先安全处理。"
            context.memory_loaded = True
            context.steps.append(AgentStep(step,"MemoryAgent","READ_MEMORY","optional_context_skipped"))
            return True
        from app.services.memory import load_committed_history

        try:
            memory = self.memory
        except Exception:
            memory = None
        history, source = load_committed_history(
            self.db, memory, context.session.id, context.session.public_id,
            self.settings.redis_memory_max_messages,
        )
        compacted_history, deterministic_brief = compact_history_for_prompt(history, self.settings, context.model_input)
        context.model_history = self._bounded_model_history(
            [*compacted_history, AiMessage(role="user", content=context.model_input)]
        )
        # Necessary context is local; optional model summarization must not precede risk checks.
        context.memory_brief = deterministic_brief
        context.memory_loaded = True
        context.steps.append(AgentStep(step, "MemoryAgent", "READ_MEMORY", f"loaded {len(history)} messages from {source}"))
        return True

    def supervisor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.risk_assessed or context.intent_routed:
            return False
        context.intent = (IntentType.RISK if context.risk_level == RiskLevel.HIGH
                          else self._classify(context.model_input, context.model_history))
        if context.risk_level == RiskLevel.MEDIUM and context.intent == IntentType.CHAT:
            context.intent = IntentType.CONSULT
        context.intent_routed = True
        if context.intent == IntentType.CHAT:
            context.knowledge_handled = True
        if context.risk_level == RiskLevel.HIGH:
            context.knowledge_handled = True  # Never delay urgent output on optional retrieval.
        context.steps.append(AgentStep(step, "SupervisorAgent", "ROUTE_INTENT", f"intent={context.intent.value}"))
        return True

    def knowledge_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.knowledge_handled or context.intent == IntentType.CHAT:
            return False
        query = self._rewrite_query(context)
        retrieved = self.knowledge.retrieve(query, self.settings.knowledge_top_k)
        context.knowledge_query = query
        context.retrieved_knowledge = retrieved
        context.knowledge_handled = True
        context.steps.append(AgentStep(step, "KnowledgeAgent", "RETRIEVE_KNOWLEDGE", f"retrieved={len(retrieved)}"))
        return True

    def risk_guardian_agent(self, step: int, context: AgentContext) -> bool:
        if not context.memory_loaded or context.risk_assessed:
            return False
        try:
            assessment = self.assessment.assess(context.model_input, context.model_history)
        except Exception:
            assessment = PsychologyAssessment(EmotionLabel.ANXIETY, 3.0, RiskLevel.MEDIUM, 0.0, "风险评估不可用，保守升级")
        recent_risk = any(has_high_risk_signal(m.content) for m in context.model_history[-5:] if m.role.lower() == "user")
        if context.risk_level == RiskLevel.HIGH or recent_risk:
            assessment.risk = RiskLevel.HIGH
            assessment.emotion_score = max(assessment.emotion_score, 4.0)
        context.assessment = assessment
        context.risk_level = assessment.risk
        context.risk_assessed = True
        context.steps.append(AgentStep(step, "RiskGuardianAgent", "ASSESS_RISK", f"risk={assessment.risk.value}, emotion={assessment.emotion.value}"))
        return True

    def companion_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.intent != IntentType.CHAT or context.response_planned:
            return False
        context.response_agent = "CompanionAgent"
        context.response_plan = "围绕用户当前问题直接、自然地回答。"
        context.response_messages = [
            PromptTemplates.answer_system_prompt(IntentType.CHAT, RiskLevel.LOW, "", context.user.display_name),
            AiMessage(role="system", content=f"当前由 CompanionAgent 负责回复。\n记忆摘要：\n{context.memory_brief}\n回复策略：\n{context.response_plan}"),
            *context.model_history,
        ]
        context.response_planned = True
        context.finished = True
        context.steps.append(AgentStep(step, "CompanionAgent", "PLAN_RESPONSE", "normal companion response planned"))
        return True

    def counselor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.risk_assessed or context.intent == IntentType.CHAT or context.response_planned:
            return False
        context.response_agent = "CounselorAgent"
        if context.risk_level == RiskLevel.HIGH:
            context.response_messages = []  # Final template is checked at the response boundary.
            context.response_planned = context.finished = True
            context.steps.append(AgentStep(step,"CounselorAgent","PLAN_RESPONSE","safety_template_required"))
            return True
        context.response_plan = "先共情，再给出具体支持步骤；高风险时优先安全。"
        knowledge_context = "\n\n".join(f"- [{item.source}] {item.content}" for item in context.retrieved_knowledge)
        skill_context = MindBridgeSkillLibrary.response_skill_context(
            context.intent or IntentType.CONSULT,
            context.risk_level,
            context.original_input,
        )
        context.response_messages = [
            PromptTemplates.answer_system_prompt(
                context.intent or IntentType.CONSULT,
                context.risk_level,
                knowledge_context,
                context.user.display_name,
                skill_context,
            ),
            AiMessage(role="system", content=(
                f"当前由 CounselorAgent 负责回复。\n记忆摘要：\n{context.memory_brief}\n"
                f"KnowledgeAgent 检索 query：\n{context.knowledge_query}\n回复策略：\n{context.response_plan}"
            )),
            *context.model_history,
        ]
        context.response_planned = True
        context.finished = True
        context.steps.append(AgentStep(step, "CounselorAgent", "PLAN_RESPONSE", f"support response planned with risk={context.risk_level.value}"))
        return True

    def _classify(self, text: str, history: list[AiMessage]) -> IntentType:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return IntentType.RISK
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_WORDS):
            return IntentType.CHAT
        try:
            label = self.ai.complete(PromptTemplates.intent_prompt(history, text)).upper()
            if "RISK" in label:
                return IntentType.RISK
            if "CONSULT" in label:
                return IntentType.CONSULT
            if "CHAT" in label:
                return IntentType.CHAT
        except Exception:
            pass
        return IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT

    def _rewrite_query(self, context: AgentContext) -> str:
        try:
            query = self.ai.complete([
                AiMessage(role="system", content="你是 HarborRelay 的 KnowledgeAgent。把学生输入改写成适合检索校园心理知识库的中文查询词，只输出查询词。"),
                AiMessage(role="user", content=f"记忆摘要：\n{context.memory_brief}\n\n当前输入：\n{context.model_input}"),
            ]).strip()
            return (query or context.model_input)[:60]
        except Exception:
            return context.model_input

    def _bounded_model_history(self, history: list[AiMessage]) -> list[AiMessage]:
        limit = max(2, self.settings.chat_history_limit * 2)
        if len(history) <= limit:
            return history
        if history[0].role == "system":
            return [history[0], *history[-(limit - 1):]]
        return history[-limit:]

    def _summarize_memory(self, history: list[AiMessage], current_input: str, fallback: str) -> str:
        max_chars = max(120, self.settings.memory_summary_max_chars)
        if not history:
            return "无相关历史记忆。"
        try:
            summary = self.ai.complete([
                AiMessage(role="system", content="你是 HarborRelay 的 MemoryAgent。只输出 1-3 条中文记忆要点，不输出风险等级或诊断。"),
                AiMessage(role="user", content=f"当前输入：\n{current_input}\n\n最近历史：\n{history[-12:]}"),
            ]).strip()
            return summary[:max_chars] or fallback
        except Exception:
            return fallback or "无相关历史记忆。"
