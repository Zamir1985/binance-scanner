FROM python:3.11-slim

# Work directory
WORKDIR /app

# Install system deps (optional but useful for some libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirement file
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy scanner code
COPY scanner_websocket.py .

# Default command
CMD ["python3", "scanner_websocket.py"]
