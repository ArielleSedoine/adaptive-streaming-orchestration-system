# ======================================================
# Base image: Python + FFmpeg + MP4Box (GPAC v2.4.0)
# Compatible Cloud Functions, Cloud Run, Vertex AI
# ======================================================

FROM python:3.12-slim

LABEL maintainer="Vosyn DevOps <cloud@vosyn.ai>"
LABEL description="Base image with Python 3.12, FFmpeg, MP4Box (GPAC v2.4.0) for multimedia pipelines on GCP"

# 🧩 Empêcher les prompts interactifs de debconf
ENV DEBIAN_FRONTEND=noninteractive

# -------- Installer dépendances système --------
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    libtool \
    autoconf \
    pkg-config \
    yasm \
    cmake \
    ffmpeg \
    zlib1g-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# -------- Compiler et installer MP4Box (GPAC) --------
RUN git clone https://github.com/gpac/gpac.git /tmp/gpac && \
    cd /tmp/gpac && \
    git checkout v2.4.0 && \
    ./configure && make -j$(nproc) && make install && \
    ln -s /usr/local/bin/MP4Box /usr/bin/MP4Box && \
    rm -rf /tmp/gpac

# -------- Vérification installation --------
RUN ffmpeg -version && MP4Box -version

# -------- Fuseau horaire --------
RUN ln -sf /usr/share/zoneinfo/America/Toronto /etc/localtime && echo "America/Toronto" > /etc/timezone

# -------- Utilisateur non-root --------
RUN useradd -m appuser
USER appuser

WORKDIR /app
CMD ["bash"]
