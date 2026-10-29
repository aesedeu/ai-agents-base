"""Analyst agent — the bank analyst from lesson 3, as a Kubernetes service.

Inside: a LangGraph graph (agent ⇄ tools) with the tools of the bank MCP server.
Outside: the A2A protocol. No memory: every question is self-contained.

In the cluster: Deployment `analyst` (several replicas) behind Service `analyst:8102`.
It is NOT exposed outside the cluster: only other agents talk to it.

    MCP_URL      where the tools are          (http://bank-mcp:8101/mcp)
    PUBLIC_URL   our address for the card     (http://analyst:8102/) — other agents send messages there
"""

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from starlette.applications import Starlette

from common import POD, make_llm, make_logger

PORT = 8102
MCP_URL = os.getenv("MCP_URL", "http://bank-mcp:8101/mcp")
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://analyst:{PORT}/")
log = make_logger("analyst")

SYSTEM = ("Ты аналитик данных банка. Отвечай по-русски, кратко, фактами и цифрами. "
          "Данные — чешский банк 1993–1998, суммы в кронах (CZK). Структуру базы узнавай через "
          "list_tables и describe_table, данные — через run_sql. Ты даёшь только факты: "
          "решений (выдавать ли кредит и т. п.) не принимаешь.")

# ── 1. the agent card ──────────────────────────────────────────────────────────
CARD = AgentCard(
    name="Bank analyst",
    description="Отвечает на вопросы по данным банка: счета, обороты, балансы, кредиты, клиенты.",
    version="1.0",
    supported_interfaces=[AgentInterface(url=PUBLIC_URL, protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=False),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[AgentSkill(id="bank-data", name="Данные банка",
                       description="Факты и цифры по базе банка: SQL-аналитика по счетам, транзакциям, кредитам.",
                       tags=["bank", "sql"],
                       examples=["Какой средний месячный приход на счёт 2378 за 1998 год?"])],
)


# ── 2. the agent: agent ⇄ tools, no memory ─────────────────────────────────────
def build_graph(llm, tools):
    llm_with_tools = llm.bind_tools(tools)

    async def agent(state: MessagesState) -> dict:
        return {"messages": [await llm_with_tools.ainvoke([("system", SYSTEM), *state["messages"]])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile()


# ── 3. the A2A wrapper ─────────────────────────────────────────────────────────
class AnalystExecutor(AgentExecutor):
    graph = None                                             # built at startup (needs the MCP tools)

    async def execute(self, context: RequestContext, event_queue) -> None:
        question = context.get_user_input()
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)            # state "submitted"
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()                           # state "working"

        log.info("📨 вопрос: %s", question)
        try:
            result = await self.graph.ainvoke({"messages": [("user", question)]})
            answer = result["messages"][-1].text
        except Exception as error:                           # e.g. the LLM provider is down
            log.info("❌ ошибка: %s", error)
            await updater.failed(updater.new_agent_message([new_text_part(f"Ошибка аналитика: {error}")]))
            return
        log.info("📤 ответ: %s", " ".join(answer.split())[:200])
        # metadata "pod": which replica answered — the caller can see the load balancing
        await updater.add_artifact([new_text_part(answer)], name="answer", metadata={"pod": POD})
        await updater.complete()                             # state "completed"

    async def cancel(self, context: RequestContext, event_queue) -> None:
        raise NotImplementedError("cancel is not supported")


executor = AnalystExecutor()


@asynccontextmanager
async def lifespan(app):
    """At startup: get the tools from the MCP server (wait for it — in a cluster it may start later)."""
    client = MultiServerMCPClient({"bank": {"transport": "streamable_http", "url": MCP_URL}})
    for attempt in range(60):
        try:
            tools = await client.get_tools()
            break
        except Exception:
            log.info("⏳ жду MCP-сервер %s (%s)…", MCP_URL, attempt + 1)
            await asyncio.sleep(2)
    else:
        raise SystemExit(f"❌ MCP-сервер не отвечает: {MCP_URL}")
    executor.graph = build_graph(make_llm(), tools)
    log.info("analyst is ready → :%s  (MCP tools: %s)", PORT, [tool.name for tool in tools])
    yield


handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=CARD)
app = Starlette(routes=create_agent_card_routes(CARD) + create_jsonrpc_routes(handler, "/"), lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
