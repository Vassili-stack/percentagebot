FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV OMP_THREAD_LIMIT=1
ENV DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
