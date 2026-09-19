FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Install the package plus the web extra (FastAPI, uvicorn, and msal for sign-in) and the
# tracing extra (Application Insights / OTLP export of agent, model and tool spans).
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install ".[web,tracing]"

EXPOSE 8000

# Serves the per-user chat UI (foundry_databricks_agent/webapp.py).
# Each request runs the agent under the signed-in user's identity for Databricks calls.
CMD ["uvicorn", "foundry_databricks_agent.webapp:app", "--host", "0.0.0.0", "--port", "8000"]
