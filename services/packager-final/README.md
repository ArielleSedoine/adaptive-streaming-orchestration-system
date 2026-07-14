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
   ├── Publie un message Pub/Sub : verse-dev-433901-lang-tasks
   │
   ▼
Video Worker (Cloud Run)
   └── Crée /status/video_done.txt --> Publie un message Pub/Sub : verse-dev-433901-packager-tasks
Language Workers (Cloud Run)
   └── Créent /status/lang_xx_done.txt--> Publie un message Pub/Sub : verse-dev-433901-packager-tasks
   │
   ▼
Packager Final (Cloud Run)
   ├── Vérifie que toutes les pistes sont prêtes
   ├── Assemble les vidéos SD/HD/UHD
   ├── Ajoute les pistes audio (EN, FR, ES, JA, ZH…)
   ├── Ajoute les sous-titres (VTT transformés en MP4)
   ├── Génère le fichier manifest.mpd
   └── Crée /status/packaged.txt
