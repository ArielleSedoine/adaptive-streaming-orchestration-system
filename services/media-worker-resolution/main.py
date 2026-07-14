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
def video_worker(request: Request):
    """
    HTTP entrypoint (Pub/Sub Push)
    Déclenché automatiquement par le topic Pub/Sub verse-dev-433901-video-task.
    """
    tmp_dir = None
    try:
        envelope = request.get_json(silent=True)
        if not envelope or "message" not in envelope:
            logger.warning("⚠️ Invalid Pub/Sub message format.")
            return jsonify({"status": "error", "message": "Invalid Pub/Sub message"}), 200  # éviter retry

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
        dest_bucket = data.get("destination_bucket", DST_BUCKET)
        source_bucket = data.get("source_bucket", SRC_BUCKET)

        if not base_file or not source_blob:
            logger.warning("⚠️ Missing base_file_name or source_blob_name in message.")
            return jsonify({"status": "error", "message": "Incomplete payload"}), 200

        logger.info(f"🎞 Starting video transcoding for {base_file}")

        # Vérifier si déjà traité (idempotence)
        if already_done(base_file, dest_bucket):
            logger.info(f"✅ Video '{base_file}' already processed, skipping.")
            return ("OK", 200)  # ACK immédiat → pas de retry inutile

        # ---------------- Processing ----------------
        storage_client = storage.Client()
        tmp_dir = f"/tmp/{base_file}_video"
        os.makedirs(tmp_dir, exist_ok=True)

        local_video = f"{tmp_dir}/{os.path.basename(source_blob)}"
        download_blob(storage_client, source_bucket, source_blob, local_video)

        dash_sd  = f"{tmp_dir}/{base_file}_sd.mp4"
        dash_hd  = f"{tmp_dir}/{base_file}_hd.mp4"
        dash_uhd = f"{tmp_dir}/{base_file}_uhd.mp4"

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", local_video,
            "-filter_complex",
            "[0:v]split=3[vsd][vhd][vuhd];"
            "[vsd]scale=854:480[voutsd];"
            "[vhd]scale=1280:720[vouthd];"
            "[vuhd]scale=1920:1080[voutuhd]",
            "-threads", "0",
            # SD
            "-map", "[voutsd]", "-b:v:0", "2M", "-c:v:0", "libx264",
            "-g", "120", "-keyint_min", "120", "-preset", "fast",
            "-profile:v:0", "main", "-an", "-f", "mp4", dash_sd,
            # HD
            "-map", "[vouthd]", "-b:v:1", "6M", "-c:v:1", "libx264",
            "-g", "120", "-keyint_min", "120", "-preset", "fast",
            "-profile:v:1", "main", "-an", "-f", "mp4", dash_hd,
            # UHD
            "-map", "[voutuhd]", "-b:v:2", "10M", "-c:v:2", "libx264",
            "-g", "120", "-keyint_min", "120", "-preset", "fast",
            "-profile:v:2", "high", "-an", "-f", "mp4", dash_uhd
        ]

        logger.info("🚀 Running FFmpeg transcoding command…")
        subprocess.run(ffmpeg_cmd, check=True)

        # Upload des résultats
        for path in [dash_sd, dash_hd, dash_uhd]:
            if not os.path.exists(path):
                logger.warning(f"⚠️ Missing expected output file {path}")
                continue
            upload_blob(storage_client, dest_bucket, path,
                        f"{base_file}/dash/video/{os.path.basename(path)}")

        # Créer le marqueur de fin
        upload_string(storage_client, dest_bucket,
                      f"{base_file}/status/video_done.txt", "done")

        logger.info(f"✅ Video worker completed successfully for {base_file}")

        # 🔔 Notifier le packager via Pub/Sub
        topic_path = publisher.topic_path(PROJECT_ID, PACKAGER_TOPIC)
        pack_msg = {
            "base_file_name": base_file,
            "destination_bucket": dest_bucket,
            "event": "video_done"
        }
        publisher.publish(topic_path, json.dumps(pack_msg).encode("utf-8"))
        logger.info("📤 Published packager task (video_done).")
        logger.info("🎯 Pub/Sub message ACK sent.")
        return ("OK", 200)  # ✅ ACK message → supprime du topic

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ FFmpeg failed: {e}")
        # ⚠️ Retry Pub/Sub en cas d'erreur technique (CPU, quota, timeout)
        return ("Error during transcoding", 500)

    except Exception as e:
        logger.exception(f"❌ Unhandled error in video_worker: {e}")
        # ✅ Pas de retry pour erreur logique (payload, permissions, etc.)
        return ("Error (non-retry)", 200)

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("🧹 Cleaned up temporary folder")


# ---------------- UTILITAIRES ----------------
def already_done(base_file: str, bucket_name: str) -> bool:
    """Vérifie si la vidéo est déjà traitée (idempotence)."""
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(f"{base_file}/status/video_done.txt")
    return blob.exists()


def download_blob(client, bucket, blob_name, dest_path):
    b = client.bucket(bucket).blob(blob_name)
    if not b.exists():
        logger.warning(f"⚠️ Blob not found: {bucket}/{blob_name}")
        raise FileNotFoundError(f"Missing blob: {bucket}/{blob_name}")
    b.download_to_filename(dest_path)
    logger.info(f"⬇️ Downloaded gs://{bucket}/{blob_name}")


def upload_blob(client, bucket, src_path, dest_blob):
    client.bucket(bucket).blob(dest_blob).upload_from_filename(src_path)
    logger.info(f"⬆️ Uploaded {dest_blob}")


def upload_string(client, bucket, dest_blob, content):
    client.bucket(bucket).blob(dest_blob).upload_from_string(content)
    logger.info(f"🗂 Uploaded string to {dest_blob}")
