import os
import json
import base64
import logging
import subprocess
import shutil
import time
from flask import Flask, Request, jsonify
from google.cloud import storage

# =====================================================
# CONFIG
# =====================================================
PROJECT_ID = "verse-dev-433901"
DST_BUCKET = "vodprocessedgcp"

LOCK_TTL_SECONDS = 15 * 60

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# =====================================================
# ENTRYPOINT
# =====================================================
def packager_final(request: Request):

    tmp_dir = None
    bucket = None
    lock_blob_path = None

    try:

        # ------------------ DECODE PUBSUB ------------------
        envelope = request.get_json(silent=True)

        if not envelope or "message" not in envelope:
            logger.warning("⚠️ Invalid Pub/Sub envelope.")
            return jsonify({"status": "error"}), 200

        raw = envelope["message"]
        data = {}

        if "data" in raw:
            try:
                data = json.loads(
                    base64.b64decode(raw["data"]).decode("utf-8")
                )
            except Exception:
                logger.warning("⚠️ Invalid base64 in Pub/Sub.")
                return jsonify({"status": "error"}), 200

        base_file = data.get("base_file_name")
        dest_bucket_name = data.get("destination_bucket", DST_BUCKET)
        event_type = data.get("event")

        if not base_file:
            logger.warning("⚠️ Missing base_file.")
            return jsonify({"status": "error"}), 200

        logger.info(f"📦 packager_final triggered for {base_file} ({event_type})")

        storage_client = storage.Client()
        bucket = storage_client.bucket(dest_bucket_name)

        # ------------------ IDEMPOTENCE ------------------
        if packager_already_done(base_file, dest_bucket_name):
            logger.info("✔ Already packaged — skipping.")
            return ("OK", 200)

        # ------------------ CONTENT TYPE ------------------
        expect_video = bucket.blob(f"{base_file}/status/expect_video.txt").exists()
        audio_only = bucket.blob(f"{base_file}/status/audio_only.txt").exists()

        if expect_video and audio_only:
            logger.warning("⚠️ Both markers exist. Forcing expect_video.")
            audio_only = False

        logger.info(f"🧭 Markers: expect_video={expect_video}, audio_only={audio_only}")

        # ------------------ LOCK ------------------
        lock_blob_path = f"{base_file}/status/packager_lock.txt"
        lock_blob = bucket.blob(lock_blob_path)

        if lock_blob.exists():

            if lock_is_expired(lock_blob):

                logger.warning("⏱️ Lock expired — deleting.")
                try:
                    lock_blob.delete()
                except Exception:
                    return ("Locked", 200)

            else:

                logger.info("🔒 Lock active.")
                return ("Locked", 200)

        lock_blob.upload_from_string(str(time.time()))
        logger.info("🔐 Lock acquired")

        # ------------------ EXPECTED LANGUAGES ------------------

        expected_langs = read_langs_from_metadata(bucket, base_file)

        if not expected_langs:

            logger.info("⏳ Waiting for langs.json metadata")
            release_lock(bucket, lock_blob_path)
            return ("Waiting metadata", 200)

        done_langs = set()

        for blob in bucket.list_blobs(prefix=f"{base_file}/status/"):

            name = os.path.basename(blob.name)

            if name.startswith("lang_") and name.endswith("_done.txt"):

                lang = name.replace("lang_", "").replace("_done.txt", "")
                done_langs.add(lang)

        logger.info(f"🌐 Expected languages: {expected_langs}")
        logger.info(f"✅ Languages done: {done_langs}")

        if not expected_langs.issubset(done_langs):

            missing = expected_langs - done_langs
            logger.info(f"⏳ Missing languages: {missing}")

            release_lock(bucket, lock_blob_path)
            return ("Waiting languages", 200)

        logger.info("🚀 All languages ready. Starting packaging")

        # ------------------ LIST MEDIA ------------------
        media = list_all_media(bucket, base_file)

        videos = media["videos"]
        audios = media["audios"]
        subs = media["subs"]

        logger.info(
            f"📊 Tracks → videos={len(videos)}, audios={len(audios)}, subs={len(subs)}"
        )

        # ------------------ TRACK VALIDATION ------------------

        if expect_video:

            if not videos:

                logger.info("⏳ Waiting for video tracks")
                release_lock(bucket, lock_blob_path)
                return ("Not ready", 200)

            if not audios:

                logger.info("⏳ Waiting for audio tracks")
                release_lock(bucket, lock_blob_path)
                return ("Not ready", 200)

            logger.info("🎬 Video+Audio mode")

        elif audio_only:

            if not audios:

                logger.info("⏳ Waiting for audio")
                release_lock(bucket, lock_blob_path)
                return ("Not ready", 200)

            logger.info("🎧 Audio-only mode")

        else:

            logger.warning("⚠️ No marker — conservative mode")

            if not audios:

                release_lock(bucket, lock_blob_path)
                return ("Not ready", 200)

        # ------------------ DOWNLOAD ------------------

        tmp_dir = f"/tmp/{base_file}_dash"
        os.makedirs(tmp_dir, exist_ok=True)

        for path in videos + audios + subs:

            local = os.path.join(tmp_dir, os.path.basename(path))
            bucket.blob(path).download_to_filename(local)

            logger.info(f"⬇️ Downloaded {path}")

        # ------------------ MP4BOX ------------------

        file_hash, video_name = parse_base_file(base_file)

        mpd_filename = f"{file_hash}{video_name}.mpd"
        output_mpd = os.path.join(tmp_dir, mpd_filename)

        cmd = build_mp4box_command(tmp_dir, output_mpd)

        logger.info("⚙️ Running MP4Box")
        logger.info(" ".join(cmd))

        subprocess.run(cmd, check=True)

        # ------------------ UPLOAD ------------------

        uploaded = upload_manifest_and_segments(bucket, tmp_dir, base_file)

        # ------------------ VERIFY ------------------

        missing = [o for o in uploaded if not bucket.blob(o).exists()]

        if missing:

            logger.error(f"❌ Missing DASH objects: {missing}")

            release_lock(bucket, lock_blob_path)
            return ("DASH incomplete", 500)

        # ------------------ DONE ------------------

        bucket.blob(f"{base_file}/status/packager_done.txt").upload_from_string("done")

        release_lock(bucket, lock_blob_path)

        logger.info("🎉 Packaging complete")

        return ("OK", 200)

    except subprocess.CalledProcessError as e:

        logger.error(f"❌ MP4Box failed: {e}")

        if bucket and lock_blob_path:
            release_lock(bucket, lock_blob_path)

        return ("Packaging error", 500)

    except Exception:

        logger.exception("❌ Unexpected error")

        if bucket and lock_blob_path:
            release_lock(bucket, lock_blob_path)

        return ("Error", 200)

    finally:

        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("🧹 Cleaned /tmp")


# =====================================================
# LOCK HELPERS
# =====================================================
def lock_is_expired(blob):

    try:

        blob.reload()

        age = time.time() - blob.time_created.timestamp()

        return age > LOCK_TTL_SECONDS

    except Exception:

        return True


def release_lock(bucket, path):

    try:

        blob = bucket.blob(path)

        if blob.exists():

            blob.delete()

            logger.info("🔓 Lock released")

    except Exception as e:

        logger.error(f"⚠️ Lock release error: {e}")


# =====================================================
# UTILITIES
# =====================================================
def packager_already_done(base_file, bucket_name):

    return storage.Client().bucket(bucket_name).blob(
        f"{base_file}/status/packager_done.txt"
    ).exists()


def read_langs_from_metadata(bucket, base_file):

    blob = bucket.blob(f"{base_file}/metadata/langs.json")

    if not blob.exists():
        return set()

    try:

        data = json.loads(blob.download_as_text())

        return set(data.get("audio", [])) | set(data.get("caption", []))

    except Exception:

        return set()


def list_all_media(bucket, base_file):

    prefix = f"{base_file}/dash/"

    videos = []
    audios = []
    subs = []

    for blob in bucket.list_blobs(prefix=prefix):

        name = blob.name

        if not name.endswith(".mp4"):
            continue

        base = os.path.basename(name)

        if base.startswith("audio_"):
            audios.append(name)

        elif base.startswith("subs_"):
            subs.append(name)

        elif base.startswith("video_") or any(
            x in base for x in ["_sd", "_hd", "_uhd"]
        ):
            videos.append(name)

    return {

        "videos": videos,
        "audios": audios,
        "subs": subs,
    }


def build_mp4box_command(tmp_dir, output_mpd):

    files = os.listdir(tmp_dir)

    cmd = [

        "MP4Box",
        "-dash",
        "2000",
        "-frag",
        "2000",
        "-rap",
        "-bs-switching",
        "no",
        "-url-template",
        "-segment-name",
        "segment-$RepresentationID$-",
        "-out",
        output_mpd,
    ]

    video_files = sorted(

        f
        for f in files
        if f.endswith(".mp4")
        and (f.startswith("video_") or any(x in f for x in ["_sd", "_hd", "_uhd"]))
    )

    for idx, v in enumerate(video_files, start=1):

        cmd.append(f"{os.path.join(tmp_dir, v)}#video:id={idx}")

    audio_files = sorted(

        f for f in files if f.startswith("audio_") and f.endswith(".mp4")
    )

    start_audio = len(video_files) + 1

    for off, a in enumerate(audio_files):

        lang = a.split("_")[1].split(".")[0]

        cmd.append(

            f"{os.path.join(tmp_dir, a)}#audio:lang={lang}:id={start_audio+off}"
        )

    subs_files = sorted(

        f for f in files if f.startswith("subs_") and f.endswith(".mp4")
    )

    start_subs = len(video_files) + len(audio_files) + 1

    for off, s in enumerate(subs_files):

        lang = s.split("_")[1].split(".")[0]

        cmd.append(

            f"{os.path.join(tmp_dir, s)}#text:lang={lang}:id={start_subs+off}"
        )

    return cmd


def upload_manifest_and_segments(bucket, tmp_dir, base_file):

    uploaded = []

    for root, _, files in os.walk(tmp_dir):

        for f in files:

            if (

                f.endswith(".mpd")
                or f.endswith(".m4s")
                or (f.endswith(".mp4") and f.startswith("segment-"))
            ):

                local = os.path.join(root, f)

                dest = f"{base_file}/dash/{f}"

                bucket.blob(dest).upload_from_filename(local)

                uploaded.append(dest)

    return uploaded


def parse_base_file(base_file):

    file_hash = base_file[:64]
    video_name = base_file[64:]

    return file_hash, video_name


# =====================================================
# HEALTH
# =====================================================
@app.get("/health")
def health():

    return "OK", 200
