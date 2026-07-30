FROM python:3.12-slim

WORKDIR /app

# ── Install system dependencies ────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────
COPY main.py worker.py licensing.py releases.py qr_short_codes.py ./

# ── Run the FastAPI server ─────────────────────────────────────────────────
# Render injects PORT; local Docker falls back to 8000.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]