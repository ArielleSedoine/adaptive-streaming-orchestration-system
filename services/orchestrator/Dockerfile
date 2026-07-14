# ======================================================
# Cloud Function: orchestrator
# Basée sur base-ffmpeg-python (Python + FFmpeg + MP4Box)
# ======================================================

FROM gcr.io/verse-dev-433901/base-ffmpeg-python:latest

# -------- Variables d’environnement --------
ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto \
    FUNCTION_TARGET=orchestrator \
    FUNCTION_SIGNATURE_TYPE=http \
    PORT=8080

# -------- Installer les dépendances Python --------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -------- Copier le code source --------
COPY . .

# -------- Lancer via Python (plus sûr pour le PATH) --------
CMD ["python", "-m", "functions_framework", "--target=orchestrator", "--port=8080"]
