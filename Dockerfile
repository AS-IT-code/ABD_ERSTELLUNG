# Cloud Run / Gunicorn
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY abd_sync.py abd_to_dhl.py app.py ./

# Non-root
RUN useradd -m -u 10001 appuser
USER appuser

EXPOSE 8080

# Cloud Run $PORT kullanir; timeout uzun joblar icin yuksek tutulmali
CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 4 --timeout 3600 app:app
