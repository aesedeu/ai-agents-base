"""Small helpers shared by our three services."""

import logging
import os
import socket

POD = os.getenv("POD_NAME", socket.gethostname())       # which replica this is (set by Kubernetes)


def make_logger(name: str) -> logging.Logger:
    """Our own short log lines only — the libraries stay quiet. Every line starts with the pod name."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False                                # the SDKs put their own handler on the root logger
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(f"%(asctime)s  [{POD}]  %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(handler)
    return log


def make_llm():
    """The chat model. Where it is and which one — from the environment (ConfigMap + Secret):

        LLM_BASE_URL   OpenRouter directly (part 0)   or our gateway http://litellm:4000 (part 2)
        LLM_API_KEY    the OpenRouter key             or this agent's LiteLLM virtual key
        MODEL          a provider's model name        or a gateway alias (bank-llm)

    The code does not change when we switch to the gateway — only the environment does.
    """
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=os.getenv("MODEL", "openai/gpt-4o-mini"),
                      base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
                      api_key=os.environ["LLM_API_KEY"])
