# 🎬 Video Worker — Vosyn Verse Video Pipeline

## 🧩 Description

Le **video-worker** est un service **Cloud Run** déclenché automatiquement par **Pub/Sub**  
(`projects/verse-dev-433901/topics/verse-dev-433901-video-task`).

Il reçoit les métadonnées envoyées par le **service orchestrator**,  
télécharge la vidéo originale depuis Cloud Storage,  
et effectue un **transcodage automatique multi-résolution** :

- 🎞 **SD (480p)**  
- 🎞 **HD (720p)**  
- 🎞 **UHD (1080p)**  

Les fichiers encodés sont téléversés dans le bucket cible  
(`gs://vodprocessedgcp`), et un marqueur `status/video_done.txt`  
est créé pour signaler la fin du traitement.

---

## ⚙️ Fonctionnalités principales

- 📥 Téléchargement automatique depuis **Cloud Storage**
- 🎬 Transcodage vidéo avec **FFmpeg**
- ☁️ Téléversement des fichiers encodés vers `vodprocessedgcp`
- 📄 Création d’un marqueur `video_done.txt` pour suivi de pipeline
- 🧹 Nettoyage automatique du répertoire `/tmp`
- 🪵 Journalisation complète dans **Cloud Logging**
- 🧠 Protection contre les **retries infinis Pub/Sub**

---

## 🧱 Architecture

```text
Orchestrator
   │
   ├── Publie un message Pub/Sub : verse-dev-433901-video-task
   │
   ▼
Video Worker (Cloud Run)
   ├── Télécharge la vidéo depuis vodunprocessedgcp
   ├── Transcode SD/HD/UHD via FFmpeg
   ├── Téléverse les fichiers dans vodprocessedgcp
   └── Crée /status/video_done.txt
