from functools import partial

from app.agents.contracts import TurnState
from app.agents.runtime import AgentRuntimeService


class LangGraphAgentRuntimeService(AgentRuntimeService):
    """Deterministic safety shell with explicit, isolated per-node state updates."""

    framework_name = "langgraph"

    def __init__(self, db, settings):
        super().__init__(db, settings)
        self.graph = self._build_graph()

    def run(self, user, session, original_input, model_input):
        state = self.graph.invoke(
            self.initial_state(user, session, model_input),
            {"recursion_limit": self.max_steps * 3 + 2},
        )
        return self.result_from_state(state)

    def _build_graph(self):
        from langgraph.graph import END, StateGraph

        graph = StateGraph(TurnState)
        graph.add_node("controller", lambda state: {})
        operations = [
            "precheck",
            "memory",
            "risk_guardian",
            "supervisor",
            "knowledge",
            "companion",
            "counselor",
            "referral",
        ]
        for operation in operations:
            graph.add_node(operation, partial(self.execute_step, operation=operation))
            graph.add_edge(operation, "controller")
        graph.set_entry_point("controller")
        graph.add_conditional_edges(
            "controller",
            self.select_next_operation,
            {**{name: name for name in operations}, "end": END},
        )
        return graph.compile()
