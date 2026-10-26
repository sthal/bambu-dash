# H2S Mission Control in a container (amd64 and arm64, e.g. Raspberry Pi 5).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV MC_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "server.py"]
