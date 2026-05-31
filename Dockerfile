FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

# Только прод-зависимости (без pytest/линтеров — см. requirements-dev.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ создаётся и передаётся непривилегированному пользователю
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

# Liveness по heartbeat-файлу, который планировщик трогает каждый цикл.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "main.py"]
