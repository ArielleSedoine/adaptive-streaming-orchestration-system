# 📦 Cloud Function: packager_final

**Function name:** packager_final  
**Trigger:** Pub/Sub (push subscription)  
**Platform:** Cloud Run (container)  
**Region:** us-east4  
**GCP Project:** verse-dev-433901  
**Base image:** gcr.io/verse-dev-433901/base-ffmpeg-python:latest  

---

## 🎯 Purpose

**packager_final** is the final stage of the *Video-On-Demand (VOD) & Audio-Only* processing pipeline.

Its responsibility is to **assemble all processed media tracks** (video, audio, subtitles) into a **valid MPEG-DASH presentation** and generate the final:

- `manifest.mpd`
- `segment-*.m4s`
- `status/packager_done.txt`

The function supports **both video-based content (.mp4)** and **audio-only content (.wav)**.

---

## ⚙️ Main Features

| Step | Description |
|--------|--------------|
| **1️⃣ Pub/Sub Trigger** | Triggered by `language_done` events published by language workers. |
| **2️⃣ Idempotence Check** | Skips execution if `packager_done.txt` already exists. |
| **3️⃣ Distributed Lock (TTL)** | Uses a GCS-based lock with TTL to avoid concurrent or stuck executions. |
| **4️⃣ Language Readiness Check** | Waits until all expected languages (from metadata) are completed. |
| **5️⃣ Track Discovery** | Scans `dash/` folders to detect available video, audio, and subtitle tracks. |
| **6️⃣ Audio-Only Support** | Allows DASH generation even when no video track is present. |
| **7️⃣ DASH Packaging** | Uses MP4Box to generate `manifest.mpd` and media segments. |
| **8️⃣ Final Validation** | Verifies uploaded DASH artifacts before marking completion. |

---

## 🎧 Supported Packaging Modes

| User Upload | Packaging Result |
|------------|------------------|
| `.mp4` | DASH **Video + Audio + Subtitles** |
| `.wav` | DASH **Audio-Only + Subtitles** |


---

## 🧱 GCP Architecture

```text
+------------------------------+
| Cloud Run Service:           |
|        packager_final        |
| (trigger: Pub/Sub push)      |
+--------------+---------------+
               |
               | Google Cloud Storage
               v
     +---------------------------+
     | dash/                     |
     |  ├── video/*.mp4          |
     |  ├── audio/*.mp4          |
     |  ├── subtitles/*.mp4      |
     |  └── manifest.mpd         |
     +---------------------------+
               |
               v
     +---------------------------+
     | status/                   |
     |  ├── packager_done.txt    |
     |  └── packager_lock.txt    |
     +---------------------------+
