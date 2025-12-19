FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ---- Python tooling ----
RUN pip install --upgrade pip setuptools wheel

# ---- Dependencies ----
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- App ----
COPY scanner_websocket.py .

# ---- Run ----
CMD ["python", "scanner_websocket.py"]
