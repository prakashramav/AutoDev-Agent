# ─── Base image ────────────────────────────────────────────────
FROM python:3.12-slim

# ─── System dependencies ───────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install docker CLI (to talk to the host docker daemon via mounted socket)
RUN curl -fsSL https://get.docker.com | sh 2>/dev/null || \
    (apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*)

# ─── Working directory ─────────────────────────────────────────
WORKDIR /app

# ─── Python dependencies ───────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Application source ────────────────────────────────────────
COPY . .

# ─── Run migrations, then start the API ────────────────────────
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
