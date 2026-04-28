FROM python:3.11-slim

WORKDIR /app

ENV OMP_THREAD_LIMIT=1
ENV OPENBLAS_NUM_THREADS=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]
