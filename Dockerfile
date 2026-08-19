FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py bot_v2.py health_server.py historical_import.py startup.py normalization_patch.py repair.py ./

CMD ["python", "startup.py"]
