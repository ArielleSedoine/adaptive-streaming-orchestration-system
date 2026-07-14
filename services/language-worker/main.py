import os
import json
import base64
import logging
import subprocess
import shutil
from flask import jsonify, Request
from google.cloud import storage
from google.cloud import pubsub_v1

PACKAGER_TOPIC = "verse-dev-433901-packager-tasks"

publisher = pubsub_v1.PublisherClient()


# ---------------- CONFIG ----------------
PROJECT_ID = "verse-dev-433901"
SRC_BUCKET = "vodunprocessedgcp"
DST_BUCKET = "vodprocessedgcp"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- ENTRYPOINT ----------------
def language_worker(request: Request):
    """
    HTTP entrypoint (Pub/Sub Push)
    Déclenché automatiquement par le topic Pub/Sub verse-dev-433901-lang-tasks.
    """
    tmp_dir = None
    try:
        envelope = request.get_json(silent=True)
        if not envelope or "message" not in envelope:
            logger.warning("⚠️ Invalid Pub/Sub message format.")
            return jsonify({"status": "error", "message": "Invalid Pub/Sub message"}), 200

        pubsub_message = envelope["message"]

        # Décodage du message Pub/Sub
        data = {}
        if "data" in pubsub_message:
            try:
                data = json.loads(base64.b64decode(pubsub_message["data"]).decode("utf-8"))
            except Exception as e:
                logger.warning(f"⚠️ Could not decode Pub/Sub data: {e}")
                return jsonify({"status": "error", "message": "Invalid base64 data"}), 200

        base_file = data.get("base_file_name")
        source_blob = data.get("source_blob_name")
        lang = data.get("language")
        dest_bucket = data.get("destination_bucket", DST_BUCKET)
        source_bucket = data.get("source_bucket", SRC_BUCKET)

        if not all([base_file, source_blob, lang]):
            logger.warning("⚠️ Missing fields in Pub/Sub message.")
            return jsonify({"status": "error", "message": "Incomplete payload"}), 200

        logger.info(f"🚀 Processing language: {lang} for {base_file}")

        # --- Idempotence : déjà traité ? ---
        if lang_already_done(base_file, lang, dest_bucket):
            logger.info(f"✅ Language '{lang}' for '{base_file}' already done. Skipping.")
            return ("OK", 200)

        # ---------------- Processing ----------------
        storage_client = storage.Client()
        tmp_dir = f"/tmp/{base_file}_{lang}"
        os.makedirs(tmp_dir, exist_ok=True)

        # Chemins
        local_video = f"{tmp_dir}/{os.path.basename(source_blob)}"
        audio_wav   = f"/tmp/{base_file}_{lang}.wav"
        caption_vtt = f"/tmp/{base_file}_{lang}.vtt"
        audio_mp4   = f"{tmp_dir}/audio_{lang}.mp4"
        subs_mp4    = f"{tmp_dir}/subs_{lang}.mp4"

        # Téléchargements
        download_blob(storage_client, source_bucket, source_blob, local_video, optional=False)
        download_blob(storage_client, dest_bucket, f"{base_file}/audio/{base_file}_{lang}.wav", audio_wav, optional=True)
        download_blob(storage_client, dest_bucket, f"{base_file}/caption/{base_file}_{lang}.vtt", caption_vtt, optional=True)

        # 1) WAV -> AAC -> MP4 (audio)
        if os.path.exists(audio_wav):
            aac_path = f"/tmp/{base_file}_{lang}.aac"
            subprocess.run([
                "ffmpeg", "-y", "-vn", "-i", audio_wav,
                "-ar", "48000", "-acodec", "aac", "-b:a", "128k",
                aac_path
            ], check=True)
            subprocess.run(["MP4Box", "-add", f"{aac_path}:lang={lang}", "-new", audio_mp4], check=True)
            upload_blob(storage_client, dest_bucket, audio_mp4, f"{base_file}/dash/audio/audio_{lang}.mp4")
            logger.info(f"🎧 Audio '{lang}' encoded & uploaded.")
        else:
            logger.warning(f"⚠️ No audio WAV found for {lang}")

        # 2) VTT -> MP4 (subtitles)
        if os.path.exists(caption_vtt):
            subprocess.run(["MP4Box", "-add", f"{caption_vtt}:hdlr=sbtl:lang={lang}", "-new", subs_mp4], check=True)
            upload_blob(storage_client, dest_bucket, subs_mp4, f"{base_file}/dash/subtitles/subs_{lang}.mp4")
            logger.info(f"💬 Subtitles '{lang}' packaged & uploaded.")
        else:
            logger.warning(f"⚠️ No caption VTT found for {lang}")

        # 3) Marqueur de fin
        status_blob = f"{base_file}/status/lang_{lang}_done.txt"
        upload_string(storage_client, dest_bucket, status_blob, f"done:{lang}")
        logger.info(f"✅ Language '{lang}' processing complete for {base_file}.")
        logger.info("🎯 Pub/Sub message ACK sent.")

        # 🔔 Notifier le packager via Pub/Sub
        topic_path = publisher.topic_path(PROJECT_ID, PACKAGER_TOPIC)
        pack_msg = {
            "base_file_name": base_file,
            "destination_bucket": dest_bucket,
            "language": lang,
            "event": "language_done"
        }
        publisher.publish(topic_path, json.dumps(pack_msg).encode("utf-8"))
        logger.info(f"📤 Published packager task (language_done, {lang}).")
        
        return ("OK", 200)

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ FFmpeg/MP4Box failed: {e}")
        # Erreur technique potentiellement récupérable -> retry Pub/Sub
        return ("Error during media processing", 500)

    except FileNotFoundError as e:
        # Fichier requis manquant (ex: source vidéo) -> non-retry
        logger.warning(f"⚠️ Required file missing: {e}")
        return ("Missing required input (non-retry)", 200)

    except Exception as e:
        logger.exception(f"❌ Unhandled error in language_worker: {e}")
        # Erreur logique/perm -> non-retry
        return ("Error (non-retry)", 200)

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("🧹 Cleaned up temporary folder")

# ---------------- UTILITAIRES ----------------
def lang_already_done(base_file: str, lang: str, bucket_name: str) -> bool:
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(f"{base_file}/status/lang_{lang}_done.txt")
    return blob.exists()

def download_blob(client, bucket_name, blob_name, dest_path, optional=False):
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    if not blob.exists():
        if optional:
            logger.warning(f"Optional file not found: gs://{bucket_name}/{blob_name}")
            return
        logger.warning(f"⚠️ Missing required blob: gs://{bucket_name}/{blob_name}")
        raise FileNotFoundError(f"{bucket_name}/{blob_name}")
    blob.download_to_filename(dest_path)
    logger.info(f"⬇️ Downloaded gs://{bucket_name}/{blob_name}")

def upload_blob(client, bucket_name, source_path, dest_blob):
    client.bucket(bucket_name).blob(dest_blob).upload_from_filename(source_path)
    logger.info(f"⬆️ Uploaded {dest_blob}")

def upload_string(client, bucket_name, dest_blob, content):
    client.bucket(bucket_name).blob(dest_blob).upload_from_string(content)
    logger.info(f"🗂 Uploaded string to {dest_blob}")
