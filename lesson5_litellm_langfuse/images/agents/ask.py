"""Ask an A2A agent one question from the command line.

    python ask.py http://risk-manager:8103 "Можно ли выдать счёту 2378 кредит 150 000 CZK на 24 месяца?"
    python ask.py http://risk-manager:8103 "А если 300 000?" --context <id from the previous answer>

Prints the answer, which pod answered and the conversation id (context_id) — pass it
with --context to continue the same conversation. Used inside the cluster (kubectl run,
the CronJob in part 8) and from the laptop.
"""

import argparse
import asyncio
import uuid

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers.proto_helpers import get_artifact_text, get_message_text, new_text_part
from a2a.types import Message, Role, SendMessageRequest, TaskState


async def ask(url: str, question: str, context_id: str | None = None) -> dict:
    """Send one message; return {"answer", "pod", "context_id", "state"}."""
    http = httpx.AsyncClient(timeout=600)                    # agents may think a while
    card = await A2ACardResolver(http, url).get_agent_card()
    # The card holds ONE address (for the risk manager — the outside one, risk.agents.test). Inside the
    # cluster the same agent is reached by its Service name. So we send to the address we came to.
    card.supported_interfaces[0].url = url.rstrip("/") + "/"
    client = await create_client(card, client_config=ClientConfig(streaming=False, httpx_client=http))
    message = Message(role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[new_text_part(question)])
    if context_id:
        message.context_id = context_id                     # continue this conversation
    try:
        async for event in client.send_message(SendMessageRequest(message=message)):
            task = event.task
            result = {"state": TaskState.Name(task.status.state), "context_id": task.context_id}
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                artifact = task.artifacts[0]
                return result | {"answer": get_artifact_text(artifact), "pod": artifact.metadata["pod"]}
            return result | {"answer": get_message_text(task.status.message), "pod": "?"}
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ask an A2A agent")
    parser.add_argument("url", help="agent address, e.g. http://risk-manager:8103")
    parser.add_argument("question")
    parser.add_argument("--context", help="context_id of a previous answer: continue that conversation")
    args = parser.parse_args()
    reply = asyncio.run(ask(args.url, args.question, args.context))
    print(reply["answer"])
    print(f"\n— ответил под: {reply['pod']}   разговор (--context): {reply['context_id']}   статус: {reply['state']}")
