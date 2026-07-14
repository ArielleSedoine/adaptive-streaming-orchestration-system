# 🎬 Cloud Function: orchestrator

**Function name:** orchestrator
**Trigger:** Eventarc (GCS → object.finalize)
**Platform:** Cloud Run (container)
**Region:** us-east4
**GCP Project:** verse-dev-433901
**Base image:** gcr.io/verse-dev-433901/base-ffmpeg-python:latest

---

## 🎯 Purpose

**orchestrator** is the main entry point of the *Video-On-Demand (VOD)* processing pipeline.
Every newly uploaded .mp4 file in the source bucket automatically triggers:

1- original video copy

2- thumbnail generation using ffmpeg

3- dynamic language detection (based on existing audio/subtitle files)

4- metadata generation

5- Pub/Sub fan-out for transcoding and per-language processing

---

## ⚙️ Main Features

| Step | Description |
|--------|--------------|
| **1️⃣ Automatic Trigger** | Uploading a .mp4 to vodunprocessedgcp triggers Eventarc. |
| **2️⃣ Original Copy** | The file is copied into vodprocessedgcp/original/. |
| **3️⃣ Local Thumbnail** | ffmpeg generates a frame (at 10 seconds) using a local temporary download. |
| **4️⃣ Language Detection** | Dynamic scanning of /audio/ and /caption/ folders to detect existing languages. |
| **5️⃣ Metadata Creation** | A metadata/langs.json file is generated automatically. |
| **6️⃣ Task Publication** | Publishes: • one “video-task” Pub/Sub message • one “lang-task” message per detected language |

---

## 🧱 GCP Architecture

```text
+--------------------------+
| Cloud Run Service:       |
|        orchestrator      |
| (trigger: Eventarc GCS)  |
+-----------+--------------+
            |
            | Pub/Sub
            v
  +------------------------+
  | Topic: verse-dev-433901-video-task    |
  |   → transcoding worker                |
  +--------------------------------------+
            |
            | Pub/Sub (per language)
            v
  +------------------------+
  | Topic: verse-dev-433901-lang-tasks    |
  |   → language worker                   |
  +--------------------------------------+
