# Render dışında bir yere (VPS, Fly.io, vb.) taşımak istersen bu yeterli.
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV FRONTEND_DIR=/app/frontend \
    CACHE_PATH=/tmp/scans.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000
WORKDIR /app/backend
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
