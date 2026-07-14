FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto \
    PORT=8080

WORKDIR /app

# Installer les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copier le code source
COPY . .

# Lancer directement ton serveur Flask (main.py)
CMD ["python", "main.py"]
