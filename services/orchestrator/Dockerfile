FROM gcr.io/verse-dev-433901/base-ffmpeg-python:latest

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "functions_framework", "--target=entrypoint", "--port=8080"]


