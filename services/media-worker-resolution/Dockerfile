# ======================================================
# Cloud Function: video-worker
# Basée sur l'image commune : Python + FFmpeg + MP4Box
# ======================================================

FROM gcr.io/verse-dev-433901/base-ffmpeg-python:latest

# -------- Variables d’environnement générales --------
ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto \
    FUNCTION_TARGET=video_worker \
    FUNCTION_SIGNATURE_TYPE=http \
    PORT=8080

# -------- Installer les dépendances Python --------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -------- Copier le code source --------
COPY . .

# -------- Lancer via Python (plus fiable que CMD direct) --------
CMD ["python", "-m", "functions_framework", "--target=video_worker", "--port=8080"]
