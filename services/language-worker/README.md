# 🧠 Language Worker — Vosyn Verse Video Pipeline

## 🧩 Description

Le **language-worker** est un service **Cloud Run** déclenché automatiquement via **Pub/Sub (Push)**.  
Il reçoit des messages du **service orchestrator**, contenant la langue à traiter pour une vidéo donnée,  
puis exécute le **packaging audio et sous-titres multilingue** à l’aide de **FFmpeg** et **MP4Box**.

Chaque instance du service gère une seule langue à la fois,  
convertissant et téléversant les fichiers traités dans le bucket cible (`vodprocessedgcp`).

---

## ⚙️ Fonctionnalités principales

- 📦 Réception automatique des tâches via **Pub/Sub Push**
- 🎧 Conversion **audio WAV → AAC → MP4**
- 💬 Conversion **sous-titres VTT → MP4 (track sbtl)**  
- ☁️ Téléversement dans le bucket cible `vodprocessedgcp`
- 📄 Création d’un marqueur `status/lang_<lang>_done.txt`
- 🧹 Nettoyage automatique du répertoire temporaire `/tmp`
- 🪵 Journalisation complète dans **Cloud Logging**

---

## 🧱 Architecture

```text
Orchestrator
   │
   ├── Publie un message Pub/Sub : verse-dev-433901-lang-tasks
   │
   ▼
Language Worker (Cloud Run)
   ├── Télécharge la vidéo depuis vodunprocessedgcp
   ├── Télécharge les fichiers audio (.wav) et sous-titres (.vtt)
   ├── Convertit les fichiers audio/sous-titres selon la langue
   ├── Téléverse les fichiers traités vers vodprocessedgcp
   └── Crée un marqueur /status/lang_<lang>_done.txt
