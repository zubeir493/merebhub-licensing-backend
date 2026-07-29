# Default Render Dockerfile for manually-created web services.
# The full backend should be deployed with render.yaml. This default image runs
# only the public FastAPI middleware service when Render looks for ./Dockerfile.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY middleware/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY middleware/main.py middleware/worker.py middleware/licensing.py middleware/releases.py ./

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
