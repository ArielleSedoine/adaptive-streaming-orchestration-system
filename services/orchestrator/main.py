import os
import json
import logging
import subprocess
from flask import Flask, request, jsonify
from google.cloud import storage, pubsub_v1

# -------- CONFIG --------
PROJECT_ID   = "verse-dev-433901"
TOPIC_LANG   = "verse-dev-433901-lang-tasks"
TOPIC_VIDEO  = "verse-dev-433901-video-task"
SRC_BUCKET   = "vodunprocessedgcp"
DST_BUCKET   = "vodprocessedgcp"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

publisher = pubsub_v1.PublisherClient()

# =====================================================================
# 🔥 EVENTARC ENTRYPOINT
# =====================================================================
def entrypoint(request):
    event = request.get_json(silent=True)
    if not event:
        logger.error("❌ No JSON event received.")
        return jsonify({"error": "No event"}), 200
    return handle_new_object(event)

# =====================================================================
# 🟢 Parse event + routing logic
# =====================================================================
def handle_new_object(event):

    logger.info(f"📩 Incoming Eventarc event: {event}")

    bucket_name = event.get("bucket")
    object_name = event.get("name")

    if not bucket_name or not object_name:
        logger.warning("⚠️ Missing bucket or object")
        return ("Missing event data", 200)

    base_name = os.path.splitext(os.path.basename(object_name))[0]
    ext = os.path.splitext(object_name)[1].lower()

    logger.info(f"🎯 Uploaded file detected: {object_name} (ext={ext})")

    try:
        if ext == ".mp4":
            return orchestrate_video(bucket_name, object_name, base_name)
        elif ext == ".wav":
            return orchestrate_audio(bucket_name, object_name, base_name)
        else:
            logger.info("ℹ️ Unsupported file type.")
            return ("Unsupported file type", 200)

    except Exception:
        logger.exception("❌ Error during orchestration")
        return ("Error", 200)

# =====================================================================
# 🎬 VIDEO WORKFLOW (.mp4)
# =====================================================================
def orchestrate_video(bucket_name, source_blob_name, base_name):

    storage_client = storage.Client()
    bucket = storage_client.bucket(DST_BUCKET)

    # ---- STATUS MARKER (idempotent) ----
    write_status_once(bucket, f"{base_name}/status/expect_video.txt")

    # 1) Copy original video
    copy_original_file(storage_client, bucket_name, source_blob_name, base_name)

    # 2) Thumbnail
    blob_obj = storage_client.bucket(bucket_name).blob(source_blob_name)
    generate_thumbnail_local(base_name, blob_obj, DST_BUCKET)

    # 3) Detect languages
    audio_langs, caption_langs = detect_available_languages(base_name, DST_BUCKET)
    all_langs = sorted(set(audio_langs + caption_langs))

    # 4) Save metadata
    save_langs_json(base_name, DST_BUCKET, audio_langs, caption_langs)

    # 5) Publish video task
    publish_task(TOPIC_VIDEO, {
        "base_file_name": base_name,
        "source_blob_name": source_blob_name,
        "destination_bucket": DST_BUCKET
    })

    # 6) Publish language tasks
    for lang in all_langs:
        publish_task(TOPIC_LANG, {
            "base_file_name": base_name,
            "source_blob_name": source_blob_name,
            "language": lang,
            "destination_bucket": DST_BUCKET
        })

    logger.info("✨ Video orchestration DONE")
    return ("OK", 200)

# =====================================================================
# 🎧 AUDIO-ONLY WORKFLOW (.wav)
# =====================================================================
def orchestrate_audio(bucket_name, source_blob_name, base_name):

    storage_client = storage.Client()
    bucket = storage_client.bucket(DST_BUCKET)

    # ---- STATUS MARKER (idempotent) ----
    write_status_once(bucket, f"{base_name}/status/audio_only.txt")

    # 1) Copy original audio
    copy_original_file(storage_client, bucket_name, source_blob_name, base_name)

    # 2) Detect languages
    audio_langs, caption_langs = detect_available_languages(base_name, DST_BUCKET)
    all_langs = sorted(set(audio_langs + caption_langs))

    # 3) Save metadata
    save_langs_json(base_name, DST_BUCKET, audio_langs, caption_langs)

    # 4) Publish language tasks
    for lang in all_langs:
        publish_task(TOPIC_LANG, {
            "base_file_name": base_name,
            "source_blob_name": source_blob_name,
            "language": lang,
            "destination_bucket": DST_BUCKET
        })

    logger.info("🎧 Audio-only orchestration DONE")
    return ("OK", 200)

# =====================================================================
# 🔁 Pub/Sub helper
# =====================================================================
def publish_task(topic_name, payload):
    topic = publisher.topic_path(PROJECT_ID, topic_name)
    publisher.publish(topic, json.dumps(payload).encode("utf-8"))
    logger.info(f"📤 Published {payload} to {topic_name}")

# =====================================================================
# 📁 Copy original file
# =====================================================================
def copy_original_file(storage_client, bucket_name, blob_name, base_file):
    sb = storage_client.bucket(bucket_name).blob(blob_name)
    db = storage_client.bucket(DST_BUCKET)
    new_name = f"{base_file}/original/{os.path.basename(blob_name)}"
    sb.bucket.copy_blob(sb, db, new_name)
    logger.info(f"📁 Original copied → gs://{DST_BUCKET}/{new_name}")

# =====================================================================
# 🖼 Thumbnail generation
# =====================================================================
def generate_thumbnail_local(base_file_name, blob_obj, dest_bucket):

    local_video = f"/tmp/{base_file_name}.mp4"
    local_thumb = f"/tmp/{base_file_name}.jpg"

    try:
        blob_obj.download_to_filename(local_video)

        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-i", local_video,
            "-ss", "00:00:10",
            "-vframes", "1",
            "-q:v", "2",
            local_thumb
        ], check=True)

        upload_file(dest_bucket, local_thumb,
            f"{base_file_name}/thumbnail/{base_file_name}.jpg")

    finally:
        for p in [local_video, local_thumb]:
            if os.path.exists(p):
                os.remove(p)

def upload_file(bucket, src, dst):
    storage.Client().bucket(bucket).blob(dst).upload_from_filename(src)

# =====================================================================
# 🌐 Detect languages dynamically
# =====================================================================
def detect_available_languages(base_file_name, dest_bucket):
    client = storage.Client()
    bucket = client.bucket(dest_bucket)
    aud, caps = set(), set()

    for blob in bucket.list_blobs(prefix=f"{base_file_name}/audio/"):
        fn = os.path.basename(blob.name)
        if "_" in fn:
            aud.add(fn.split("_")[-1].split(".")[0])

    for blob in bucket.list_blobs(prefix=f"{base_file_name}/caption/"):
        fn = os.path.basename(blob.name)
        if "_" in fn:
            caps.add(fn.split("_")[-1].split(".")[0])

    return sorted(aud), sorted(caps)

def save_langs_json(base, bucket, aud, caps):
    tmp = f"/tmp/{base}_langs.json"
    with open(tmp, "w") as f:
        json.dump({"audio": aud, "caption": caps}, f, indent=2)
    upload_file(bucket, tmp, f"{base}/metadata/langs.json")
    os.remove(tmp)

# =====================================================================
# 🛡 Status helper (RECOMMANDATION 1)
# =====================================================================
def write_status_once(bucket, path, content="1"):
    blob = bucket.blob(path)
    if not blob.exists():
        blob.upload_from_string(content)
        logger.info(f"📝 Status created: {path}")
    else:
        logger.info(f"ℹ️ Status already exists: {path}")

# =====================================================================
# 🟢 Health
# =====================================================================
@app.get("/health")
def health():
    return "OK", 200

# LOCAL DEV MODE
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
