# One image for the whole bank: the MCP server, the analyst, the risk manager, the data loader.
# Which of them runs is chosen by `command:` in the Kubernetes manifest.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# dependencies first: this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY common.py mcp_server.py analyst_agent.py risk_agent.py ask.py ./
COPY scripts ./scripts
# the dataset cache: the loader job works without internet
COPY data ./data
