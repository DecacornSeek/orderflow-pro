FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/ ./agents/
COPY core/   ./core/

CMD ["python", "-m", "agents.exchange_agent"]
