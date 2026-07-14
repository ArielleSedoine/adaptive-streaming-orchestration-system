import json
import os
import logging
import subprocess
from flask import jsonify, Request   # ✅ Ajout important
from google.cloud import storage, pubsub_v1

# -------- CONFIGURATION GÉNÉRALE --------
PROJECT_ID = "verse-dev-433901"
TOPIC_LANG = "verse-dev-433901-lang-tasks"
TOPIC_VIDEO = "verse-dev-433901-video-task"
SRC_BUCKET = "vodunprocessedgcp"
DST_BUCKET = "vodprocessedgcp"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --------- CLOUD FUNCTION ENTRYPOINT ---------
def orchestrator(request: Request):
    try:
        req = request.get_json(silent=True) or {}
        source_bucket = req.get("source_bucket", SRC_BUCKET)
        dest_bucket   = req.get("destination_bucket", DST_BUCKET)

        # 1️⃣ Trouver la dernière vidéo .mp4
        storage_client = storage.Client()
        sb = storage_client.bucket(source_bucket)
        mp4s = [b for b in sb.list_blobs() if b.name.lower().endswith(".mp4")]
        if not mp4s:
            logger.warning("⚠️ No .mp4 file found in source bucket.")
            return jsonify({"status": "error", "message": "No .mp4 video found"}), 200  # ✅ renvoie toujours 200

        last_blob = max(mp4s, key=lambda b: b.updated)
        source_blob_name = last_blob.name
        base_name = os.path.splitext(os.path.basename(source_blob_name))[0]
        logger.info(f"🎞 Latest video detected: {source_blob_name}")

        # 2️⃣ Copier l’original
        copy_original_file(storage_client, last_blob, base_name, dest_bucket)

        # 3️⃣ Générer le thumbnail
        local_video = f"/tmp/{os.path.basename(source_blob_name)}"
        download_blob(source_bucket, source_blob_name, local_video)
        generate_thumbnail(base_name, local_video, dest_bucket)

        if os.path.exists(local_video):
            os.remove(local_video)
            logger.info("🧹 Local video cleaned up from /tmp")

        # 4️⃣ Détecter les langues
        audio_langs, caption_langs = detect_available_languages(base_name, dest_bucket)
        all_langs = sorted(set(audio_langs + caption_langs))
        if not all_langs:
            logger.warning("⚠️ No language assets found.")
            return jsonify({"status": "error", "message": "No language assets found"}), 200

        save_langs_json(base_name, dest_bucket, audio_langs, caption_langs)

        # 5️⃣ Publier la tâche video-worker
        publisher = pubsub_v1.PublisherClient()
        topic_video = publisher.topic_path(PROJECT_ID, TOPIC_VIDEO)
        publisher.publish(topic_video, json.dumps({
            "base_file_name": base_name,
            "source_blob_name": source_blob_name,
            "destination_bucket": dest_bucket
        }).encode("utf-8"))
        logger.info("📤 Published video-worker task")

        # 6️⃣ Publier une tâche par langue
        topic_lang = publisher.topic_path(PROJECT_ID, TOPIC_LANG)
        for lang in all_langs:
            msg = {
                "base_file_name": base_name,
                "source_blob_name": source_blob_name,
                "language": lang,
                "destination_bucket": dest_bucket
            }
            publisher.publish(topic_lang, json.dumps(msg).encode("utf-8"))
            logger.info(f"📤 Published language-worker task for {lang}")

        logger.info("🎯 Pub/Sub message ACK sent.")
        return jsonify({
            "status": "ok",
            "message": f"✅ Published {len(all_langs)} language tasks + 1 video task for '{base_name}'."
        }), 200

    except Exception as e:
        logger.exception(f"❌ Error in orchestrator: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200  # ✅ évite retry

    finally:
        # Nettoyage global /tmp
        for path in os.listdir("/tmp"):
            try:
                full_path = os.path.join("/tmp", path)
                if os.path.isfile(full_path):
                    os.remove(full_path)
            except Exception:
                pass
        logger.info("🧹 Global cleanup done in /tmp.")


# --------- UTILS ORCHESTRATOR ---------
def copy_original_file(storage_client, source_blob, base_file_name, dest_bucket):
    db = storage_client.bucket(dest_bucket)
    new_name = f"{base_file_name}/original/{os.path.basename(source_blob.name)}"
    source_blob.bucket.copy_blob(source_blob, db, new_name)
    logger.info(f"✅ Original copied to gs://{dest_bucket}/{new_name}")


def download_blob(bucket_name, blob_name, dst):
    storage.Client().bucket(bucket_name).blob(blob_name).download_to_filename(dst)
    logger.info(f"⬇️ Downloaded gs://{bucket_name}/{blob_name} -> {dst}")


def upload_blob(bucket, src, dst):
    storage.Client().bucket(bucket).blob(dst).upload_from_filename(src)
    logger.info(f"⬆️ Uploaded {src} -> gs://{bucket}/{dst}")


def generate_thumbnail(base_file_name, local_video_path, dest_bucket):
    thumb = f"/tmp/{base_file_name}.jpg"
    try:
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-i", local_video_path,
            "-ss", "00:00:10", "-vframes", "1", "-q:v", "2", thumb
        ], check=True)
        upload_blob(dest_bucket, thumb, f"{base_file_name}/thumbnail/{base_file_name}.jpg")
        logger.info("🖼 Thumbnail generated & uploaded")
    finally:
        if os.path.exists(thumb):
            os.remove(thumb)
            logger.info("🧹 Thumbnail cleaned from /tmp")


def detect_available_languages(base_file_name, dest_bucket):
    client = storage.Client()
    bucket = client.bucket(dest_bucket)
    aud, caps = set(), set()

    for blob in bucket.list_blobs(prefix=f"{base_file_name}/audio/"):
        fn = os.path.basename(blob.name)
        if "_" in fn and "." in fn:
            aud.add(fn.split("_")[-1].split(".")[0])

    for blob in bucket.list_blobs(prefix=f"{base_file_name}/caption/"):
        fn = os.path.basename(blob.name)
        if "_" in fn and "." in fn:
            caps.add(fn.split("_")[-1].split(".")[0])

    logger.info(f"🔊 Audio languages: {sorted(aud)} | 💬 Captions: {sorted(caps)}")
    return sorted(aud), sorted(caps)


def save_langs_json(base_file_name, dest_bucket, audio_langs, caption_langs):
    tmp_path = f"/tmp/{base_file_name}_langs.json"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({
            "audio": audio_langs,
            "caption": caption_langs
        }, f, indent=2, ensure_ascii=False)
    upload_blob(dest_bucket, tmp_path, f"{base_file_name}/metadata/langs.json")
    logger.info(f"🗂 langs.json saved to gs://{dest_bucket}/{base_file_name}/metadata/langs.json")
    os.remove(tmp_path)
