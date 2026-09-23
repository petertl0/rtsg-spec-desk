FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/tmp/specdesk PORT=8080 HTTPS_ONLY=1
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
