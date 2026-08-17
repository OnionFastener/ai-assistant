FROM python:3.12-slim

WORKDIR /app

# Git is required by the assistant for repository operations.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY assistant/ assistant/
COPY config/ config/
COPY web/ web/
COPY paths/ paths/
COPY .opencode/ .opencode/

RUN mkdir -p /tmp/assistant

EXPOSE 8010

CMD ["uvicorn", "assistant.main:app", "--host", "0.0.0.0", "--port", "8010"]
