FROM python:3.11-slim

# İş qovluğu
WORKDIR /app

# Əvvəlcə asılılıqları kopyala
COPY requirements.txt requirements.txt

# Paketləri quraşdır
RUN pip install --no-cache-dir -r requirements.txt

# Bütün layihə fayllarını kopyala
COPY . .

# Scanner-i işə sal
CMD ["python3", "scanner_websocket.py"]
