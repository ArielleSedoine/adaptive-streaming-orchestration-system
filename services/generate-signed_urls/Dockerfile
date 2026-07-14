# ======================================================
# Cloud Function: packager-final
# Basée sur l'image commune : Python + FFmpeg + MP4Box
# ======================================================

FROM gcr.io/verse-dev-433901/base-ffmpeg-python:latest

# -------- Variables d’environnement générales --------
ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto \
    FUNCTION_TARGET=packager_final \
    FUNCTION_SIGNATURE_TYPE=http \
    PORT=8080

# -------- Installer les dépendances Python --------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -------- Copier le code source --------
COPY . .

# -------- Démarrer via Functions Framework --------
CMD ["python", "-m", "functions_framework", "--target=packager_final", "--port=8080"]
