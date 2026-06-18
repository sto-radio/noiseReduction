FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/deepfilternet-cache \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libsndfile1 \
        python3 \
        python3-pip \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install --index-url https://download.pytorch.org/whl/cu124 \
        torch==2.5.1 torchaudio==2.5.1 \
    && python3 -m pip install -r requirements.txt \
    && mkdir -p /opt/deepfilternet-cache \
    && python3 -c "from df.enhance import init_df; init_df('DeepFilterNet3', post_filter=True, log_level='ERROR', log_file=None)"

COPY app ./app
COPY scripts ./scripts
COPY config ./config

RUN mkdir -p /data/input /data/output /data/work /data/models

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
