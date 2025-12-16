FROM python:3.11-slim

# Work directory
WORKDIR /app

# System deps (numpy üçün kifayətdir)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL app files
COPY . .

# Run PRO Quant entrypoint
CMD ["python", "main.py"]
