FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Create mount points
RUN mkdir -p /data /sessions

COPY app.py db.py /app/
COPY auth_cli.py /app/

# Default env; override in compose
ENV SQLITE_DB_PATH=/data/telegram_messages.db \
    SESSION_NAME=/sessions/jobsearcher \
    LOG_LEVEL=INFO

CMD ["python", "app.py"]
