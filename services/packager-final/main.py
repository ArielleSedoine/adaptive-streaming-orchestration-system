import os
import json
import base64
import logging
import subprocess
import shutil
from flask import Request, jsonify
from google.cloud import storage

# ------------- CONFIG -------------
PROJECT_ID = "verse-dev-433901"
DST_BUCKET = "vodprocessedgcp"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ------------- ENTRYPOINT -------------
def packager_final(request: Request):

    tmp_dir = None

    try:
        # ------------------ DECODE PUBSUB ------------------
        envelope = request.get_json(silent=True)
        if not envelope or "message" not in envelope:
            logger.warning("⚠️ Invalid Pub/Sub envelope.")
            return jsonify({"status": "error"}), 200  # ack, pas de retry

        raw = envelope["message"]
        data = {}

        if "data" in raw:
            try:
                data = json.loads(base64.b64decode(raw["data"]).decode("utf-8"))
            except Exception:
                logger.warning("⚠️ Invalid base64 in Pub/Sub.")
                return jsonify({"status": "error"}), 200

        base_file = data.get("base_file_name")
        dest_bucket_name = data.get("destination_bucket", DST_BUCKET)
        event_type = data.get("event")

        if not base_file:
            logger.warning("⚠️ Missing base_file in Pub/Sub message.")
            return jsonify({"status": "error", "msg": "missing base_file"}), 200

        logger.info(f"📦 packager_final triggered for {base_file} (event={event_type})")

        storage_client = storage.Client()
        bucket = storage_client.bucket(dest_bucket_name)

        # ------------------ IDEMPOTENCE ------------------
        if packager_already_done(base_file, dest_bucket_name):
            logger.info("✔ Already packaged — skipping.")
            return ("OK", 200)

        # ------------------ LOCK CHECK ------------------
        lock_blob_path = f"{base_file}/status/packager_lock.txt"
        lock_blob = bucket.blob(lock_blob_path)

        if lock_blob.exists():
            logger.info("🔒 Lock active — another instance is processing. Skipping.")
            return ("Locked", 200)

        # CREATE LOCK
        lock_blob.upload_from_string("1")
        logger.info("🔐 Lock acquired.")


        # ------------------ STATUS FILES ------------------
        statuses = list(bucket.list_blobs(prefix=f"{base_file}/status/"))

        video_done = any("video_done" in b.name for b in statuses)
        langs_done = {
            b.name.split("_")[1].split(".")[0]
            for b in statuses if "lang_" in b.name
        }

        if not video_done:
            logger.info("⏳ Video not ready (video_done.txt missing).")
            release_lock(bucket, lock_blob_path)
            return ("Not ready", 200)

        # ------------------ EXPECTED LANGS ------------------
        expected_langs = read_langs_from_metadata(bucket, base_file)
        if expected_langs:
            logger.info(f"🌐 Expected languages: {sorted(expected_langs)}")
            if not expected_langs.issubset(langs_done):
                logger.info(f"⏳ Not all languages ready. done={langs_done}")
                release_lock(bucket, lock_blob_path)
                return ("Not ready", 200)


        # ------------------ LIST MEDIA ------------------
        media = list_all_media(bucket, base_file)
        videos = media["videos"]
        audios = media["audios"]
        subs   = media["subs"]

        logger.info(f"📊 Tracks → videos={len(videos)}, audios={len(audios)}, subs={len(subs)}")

        if not videos or not audios:
            logger.info("⏳ Missing essential tracks.")
            release_lock(bucket, lock_blob_path)
            return ("Not ready", 200)


        # ------------------ DOWNLOAD ALL ------------------
        tmp_dir = f"/tmp/{base_file}_dash"
        os.makedirs(tmp_dir, exist_ok=True)

        all_tracks = videos + audios + subs
        for path in all_tracks:
            local = os.path.join(tmp_dir, os.path.basename(path))
            bucket.blob(path).download_to_filename(local)
            logger.info(f"⬇️ Downloaded {path} -> {local}")


        # ------------------ MP4BOX COMMAND ------------------
        output_mpd = os.path.join(tmp_dir, "manifest.mpd")
        mp4box_cmd = build_mp4box_command(tmp_dir, output_mpd)

        logger.info("⚙️ Running MP4Box:")
        logger.info(" ".join(mp4box_cmd))
        subprocess.run(mp4box_cmd, check=True)


        # ------------------ UPLOAD OUTPUT ------------------
        uploaded_objects = upload_manifest_and_segments(bucket, tmp_dir, base_file)


        # ------------------ VERIFICATION ------------------
        missing = [obj for obj in uploaded_objects if not bucket.blob(obj).exists()]
        if missing:
            logger.error(f"❌ Missing DASH objects: {missing}")
            release_lock(bucket, lock_blob_path)
            return ("Error: DASH incomplete", 500)


        # ------------------ DONE ------------------
        bucket.blob(f"{base_file}/status/packager_done.txt").upload_from_string("done")
        release_lock(bucket, lock_blob_path)

        logger.info("🎉 Packaging complete!")
        return ("OK", 200)


    except subprocess.CalledProcessError as e:
        logger.error(f"❌ MP4Box failed: {e}")
        release_lock(bucket, lock_blob_path)
        return ("Error during packaging", 500)

    except Exception as e:
        logger.exception(f"❌ Unexpected error: {e}")
        release_lock(bucket, lock_blob_path)
        return ("Error (non-retry)", 200)

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("🧹 Cleaned /tmp")


# ---------------------- UTILITIES ----------------------
def release_lock(bucket, path):
    try:
        blob = bucket.blob(path)
        if blob.exists():
            blob.delete()
            logger.info("🔓 Lock released.")
    except Exception as e:
        logger.error(f"⚠️ Failed to release lock: {e}")


def packager_already_done(base_file, bucket_name):
    return storage.Client().bucket(bucket_name).blob(
        f"{base_file}/status/packager_done.txt"
    ).exists()


def read_langs_from_metadata(bucket, base_file):
    meta_blob = bucket.blob(f"{base_file}/metadata/langs.json")
    if not meta_blob.exists():
        return set()
    try:
        data = json.loads(meta_blob.download_as_text())
        return set(data.get("audio", [])) | set(data.get("caption", []))
    except:
        return set()


def list_all_media(bucket, base_file):
    prefix = f"{base_file}/dash/"
    videos, audios, subs = [], [], []

    for blob in bucket.list_blobs(prefix=prefix):
        name = blob.name
        if not name.endswith(".mp4"):
            continue

        base = os.path.basename(name)

        if base.startswith("audio_"):
            audios.append(name)
        elif base.startswith("subs_"):
            subs.append(name)
        elif ("_sd" in base) or ("_hd" in base) or ("_uhd" in base) or base.startswith("video_"):
            videos.append(name)

    return {"videos": videos, "audios": audios, "subs": subs}


def build_mp4box_command(tmp_dir, output_mpd):
    files = os.listdir(tmp_dir)
    cmd = [
        "MP4Box","-dash","2000","-frag","2000","-rap",
        "-bs-switching","no","-url-template",
        "-segment-name","segment-$RepresentationID$-",
        "-out",output_mpd
    ]

    # VIDEO
    video_files = sorted(
        f for f in files
        if f.endswith(".mp4") and ("_sd" in f or "_hd" in f or "_uhd" in f or f.startswith("video_"))
    )

    for idx, v in enumerate(video_files, start=1):
        cmd.append(f"{os.path.join(tmp_dir,v)}#video:id={idx}")

    # AUDIO
    audio_files = sorted([f for f in files if f.startswith("audio_") and f.endswith(".mp4")])
    start_audio = len(video_files) + 1
    for off, a in enumerate(audio_files):
        lang = a.split("_")[1].split(".")[0]
        cmd.append(f"{os.path.join(tmp_dir,a)}#audio:lang={lang}:id={start_audio+off}")

    # SUBS
    subs_files = sorted([f for f in files if f.startswith("subs_") and f.endswith(".mp4")])
    start_subs = len(video_files) + len(audio_files) + 1
    for off, s in enumerate(subs_files):
        lang = s.split("_")[1].split(".")[0]
        cmd.append(f"{os.path.join(tmp_dir,s)}#text:lang={lang}:id={start_subs+off}")

    return cmd


def upload_manifest_and_segments(bucket, tmp_dir, base_file):
    uploaded = []

    for root, _, files in os.walk(tmp_dir):
        for f in files:
            if (
                f.endswith(".mpd")
                or f.endswith(".m4s")
                or (f.endswith(".mp4") and f.startswith("segment-") and f.endswith("-.mp4"))
            ):
                local = os.path.join(root, f)
                dest = f"{base_file}/dash/{f}"
                bucket.blob(dest).upload_from_filename(local)
                uploaded.append(dest)

    return uploaded
