import os
import json
import logging
import base64
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from google.cloud import storage, secretmanager
from google.oauth2 import service_account

# -----------------------------
# CONFIG
# -----------------------------
BUCKET_NAME = "vodprocessedgcp"
SERVICE_ACCOUNT_KEY_SECRET_NAME = "service-account-key"

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)


# -----------------------------
# SECRET ACCESS
# -----------------------------
def access_secret_version(secret_name):
    """Access the latest version of a secret from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/1038404886674/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": secret_path})
    return response.payload.data.decode("utf-8")


# -----------------------------
# SIGNED URL GENERATION
# -----------------------------
def generate_signed_url(bucket_name, object_name):
    """Generate a long-lived V2 Signed URL (valid for 1 year)."""
    try:
        # Load service account JSON key stored in Secret Manager
        key_str = access_secret_version(SERVICE_ACCOUNT_KEY_SECRET_NAME)
        service_account_info = json.loads(key_str)

        credentials = service_account.Credentials.from_service_account_info(
            service_account_info
        )

        storage_client = storage.Client(credentials=credentials)
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(object_name)

        expiration = datetime.utcnow() + timedelta(days=365)

        signed_url = blob.generate_signed_url(
            version="v2",
            expiration=expiration,
            method="GET",
        )
        return signed_url

    except Exception as e:
        logging.error(f"❌ Error generating signed URL: {e}")
        raise


# -----------------------------
# WRITE .TXT FILE
# -----------------------------
def write_signed_url_to_file(bucket_name, object_name, signed_url):
    """Write the generated signed URL into a .txt file."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    # Replace extension with .txt
    base_name = os.path.splitext(object_name)[0]
    signed_url_filename = f"{base_name}.txt"

    blob = bucket.blob(signed_url_filename)
    blob.upload_from_string(signed_url, content_type="text/plain")

    logging.info(f"✔ Signed URL saved to {signed_url_filename}")


# -----------------------------
# MAIN HANDLER
# -----------------------------
def handle_new_object(event):
    """Process Eventarc → Cloud Run events."""
    logging.info(f"📩 Incoming event: {event}")

    bucket_name = None
    object_name = None

    # -------- Case 1: Direct GCS event --------
    if "bucket" in event and "name" in event:
        bucket_name = event["bucket"]
        object_name = event["name"]

    # -------- Case 2: Pub/Sub wrapped event --------
    elif "message" in event:
        try:
            raw = event["message"].get("data")
            if raw:
                decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
                bucket_name = decoded.get("bucket")
                object_name = decoded.get("name")
        except Exception as e:
            logging.error(f"❌ Invalid Pub/Sub wrapper: {e}")
            return ("Bad Request", 400)

    if not bucket_name or not object_name:
        logging.error("❌ Missing bucket or object name.")
        return ("Bad Event Payload", 400)

    logging.info(f"➡ Processing: gs://{bucket_name}/{object_name}")

    # -------- Skip .txt files to avoid infinite loops --------
    if object_name.endswith(".txt"):
        logging.info("⏭ Skipping .txt file (prevent loop).")
        return ("OK", 200)

    # -------- Allowed extensions (GPAC compatible) --------
    allowed_ext = [
        '.mp4', '.m4s', '.m4a', '.mpd', '.vtt',
        '.webm', '.json', '.jpeg', '.jpg', '.png',
        '.wav', '.aac', '.ismt'
    ]

    if not any(object_name.lower().endswith(ext) for ext in allowed_ext):
        logging.info(f"⏭ Skipping unsupported file: {object_name}")
        return ("OK", 200)

    # -------- Do NOT skip small segments (fix missing segments bug) --------
    bucket = storage.Client().bucket(bucket_name)
    blob_meta = bucket.get_blob(object_name)

    if blob_meta is None:
        logging.error("❌ Blob metadata missing.")
        return ("OK", 200)

    # -------- Check if TXT already exists --------
    txt_blob_name = f"{os.path.splitext(object_name)[0]}.txt"
    txt_blob = bucket.blob(txt_blob_name)

    if txt_blob.exists():
        logging.info(f"⏭ Signed URL already exists → skipping ({txt_blob_name})")
        return ("OK", 200)

    # -------- Generate Signed URL --------
    logging.info("🔐 Generating signed URL...")
    signed_url = generate_signed_url(bucket_name, object_name)

    # -------- Save to TXT --------
    write_signed_url_to_file(bucket_name, object_name, signed_url)

    logging.info(f"🎉 Signed URL generated for: {object_name}")
    return ("OK", 200)


# -----------------------------
# FLASK ENTRYPOINT
# -----------------------------
@app.post("/")
def entrypoint():
    event = request.get_json(silent=True)
    if not event:
        logging.error("❌ No JSON event received.")
        return jsonify({"error": "No event"}), 400

    return handle_new_object(event)


# -----------------------------
# LOCAL DEV MODE
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
