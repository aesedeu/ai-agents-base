"""Risk manager agent — decides on loans by asking the analyst agent over A2A (lesson 3).

Here it is an A2A service too: users (and other systems) reach it from outside
the cluster through the Ingress, and it talks to the analyst inside the cluster.

Memory: the conversation = the A2A context_id. To ask a follow-up question, the client
sends the next message with the same context_id. Where the memory is kept:

    CHECKPOINT_DSN not set   → in the pod's RAM (InMemorySaver): lost on restart, not shared by replicas
    CHECKPOINT_DSN=postgres… → in Postgres: survives restarts, shared by all replicas (part 7)

    ANALYST_URL   where the analyst is        (http://analyst:8102)
    PUBLIC_URL    our address for the card    (http://risk.agents.test/)
"""

import os
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_artifact_text, get_message_text, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Message, Role, SendMessageRequest,
                       TaskState)
from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from starlette.applications import Starlette

from common import POD, make_llm, make_logger

PORT = 8103
ANALYST_URL = os.getenv("ANALYST_URL", "http://analyst:8102")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://risk.agents.test/")
CHECKPOINT_DSN = os.getenv("CHECKPOINT_DSN")
log = make_logger("risk")

POLICY = """Ты риск-менеджер банка. Решаешь, можно ли выдать кредит клиенту (счёту).
Доступа к базе у тебя нет: все факты о счёте спрашивай у аналитика (ask_analyst) — конкретными
вопросами с номером счёта и периодом. Платёж считай инструментом loan_payment.

Кредитная политика (учебная). Суммы в кронах (CZK), ставка 8% годовых:
1. Ежемесячный платёж по новому кредиту — не больше 30% среднего месячного прихода на счёт за 1998 год.
2. Если у счёта есть кредит в статусе running_in_debt или finished_default — отказ. Кредитную
   историю проверяй за ВСЁ время, а не только за 1998 год: старый кредит тоже считается.
3. Средний баланс счёта за 1998 год должен быть не меньше одного ежемесячного платежа.

Ответ: решение ОДОБРИТЬ или ОТКАЗАТЬ, затем коротко по каждому пункту политики — цифры и вывод."""

CARD = AgentCard(
    name="Risk manager",
    description="Решает, можно ли выдать кредит клиенту банка, по кредитной политике. Помнит разговор.",
    version="1.0",
    supported_interfaces=[AgentInterface(url=PUBLIC_URL, protocol_binding="JSONRPC")],
    capabilities=AgentCapabilities(streaming=False),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[AgentSkill(id="loan-decision", name="Решение по кредиту",
                       description="Одобрить или отказать в кредите по счёту с обоснованием по политике.",
                       tags=["bank", "risk", "loans"],
                       examples=["Можно ли выдать счёту 2378 кредит 150 000 CZK на 24 месяца?"])],
)


# ── tools ──────────────────────────────────────────────────────────────────────
@tool
async def ask_analyst(question: str) -> str:
    """Ask the bank's analyst agent a factual question about the bank data (accounts, transactions,
    balances, loans). Ask concrete questions with the account number and the period."""
    log.info("   📨 → аналитику: %s", question)
    client = await create_client(ANALYST_URL, client_config=ClientConfig(
        streaming=False, httpx_client=httpx.AsyncClient(timeout=300)))   # the analyst may think a while
    message = Message(role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[new_text_part(question)])
    try:
        async for event in client.send_message(SendMessageRequest(message=message)):
            task = event.task
            if task.status.state != TaskState.TASK_STATE_COMPLETED:
                return f"Аналитик не ответил: {get_message_text(task.status.message)}"
            artifact = task.artifacts[0]
            log.info("   📨 ← аналитик (pod %s): %s", artifact.metadata["pod"],
                     " ".join(get_artifact_text(artifact).split())[:150])
            return get_artifact_text(artifact)
    finally:
        await client.close()


@tool
def loan_payment(amount: float, months: int, annual_rate_percent: float = 8.0) -> str:
    """Monthly payment of an annuity loan: amount in CZK, term in months, annual rate in percent."""
    rate = annual_rate_percent / 100 / 12
    payment = amount * rate / (1 - (1 + rate) ** -months)
    return f"{payment:.2f} CZK в месяц"


# ── the A2A wrapper ────────────────────────────────────────────────────────────
class RiskExecutor(AgentExecutor):
    agent = None                                             # built at startup (needs the checkpointer)

    async def execute(self, context: RequestContext, event_queue) -> None:
        question = context.get_user_input()
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        log.info("❓ [разговор %s] %s", task.context_id[:8], question)
        config = {"configurable": {"thread_id": task.context_id}}    # memory: one thread per conversation
        try:
            result = await self.agent.ainvoke({"messages": [("user", question)]}, config)
            answer = result["messages"][-1].text
        except Exception as error:
            log.info("❌ ошибка: %s", error)
            await updater.failed(updater.new_agent_message([new_text_part(f"Ошибка риск-менеджера: {error}")]))
            return
        log.info("⚖️ %s", " ".join(answer.split())[:120])
        await updater.add_artifact([new_text_part(answer)], name="decision", metadata={"pod": POD})
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue) -> None:
        raise NotImplementedError("cancel is not supported")


executor = RiskExecutor()


@asynccontextmanager
async def lifespan(app):
    """At startup: pick the memory (RAM or Postgres) and build the agent."""
    llm = make_llm()
    if CHECKPOINT_DSN:                                       # part 7: shared memory in Postgres
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(CHECKPOINT_DSN) as checkpointer:
            await checkpointer.setup()                       # creates the checkpoint tables once
            executor.agent = create_agent(llm, [ask_analyst, loan_payment], system_prompt=POLICY,
                                          checkpointer=checkpointer)
            log.info("risk manager is ready → :%s  (memory: Postgres)", PORT)
            yield
    else:                                                    # memory in this pod's RAM
        executor.agent = create_agent(llm, [ask_analyst, loan_payment], system_prompt=POLICY,
                                      checkpointer=InMemorySaver())
        log.info("risk manager is ready → :%s  (memory: RAM of this pod)", PORT)
        yield


handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=CARD)
app = Starlette(routes=create_agent_card_routes(CARD) + create_jsonrpc_routes(handler, "/"), lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
