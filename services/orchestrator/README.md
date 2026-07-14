# 🎬 Cloud Function: orchestrator

**Nom de la fonction :** `orchestrator`  
**Région :** `us-east4`  
**Projet GCP :** `verse-dev-433901`  
**Image de base :** `gcr.io/verse-dev-433901/base-ffmpeg-python:latest`  

---

## 🎯 Objectif

La fonction **`orchestrator`** sert de point d’entrée principal du pipeline **VOD (Video on Demand)** sur GCP.  
Elle détecte automatiquement la dernière vidéo uploadée dans le bucket source, exécute plusieurs tâches automatisées et publie des messages Pub/Sub pour orchestrer le traitement multimédia.

---

## ⚙️ Fonctionnalités principales

| Étape | Description |
|--------|--------------|
| **1️⃣ Détection vidéo** | Recherche la dernière vidéo `.mp4` uploadée dans `vodunprocessedgcp`. |
| **2️⃣ Copie originale** | Copie la vidéo d’origine vers `vodprocessedgcp` (`/original/`). |
| **3️⃣ Génération du thumbnail** | Extrait une image statique (à 10 secondes) à l’aide de `ffmpeg`. |
| **4️⃣ Détection des langues** | Identifie les pistes audio et sous-titres disponibles dans `/audio/` et `/caption/`. |
| **5️⃣ Sauvegarde des métadonnées** | Crée un fichier `langs.json` contenant les langues détectées. |
| **6️⃣ Publication des tâches** | Publie :<br>• une tâche Pub/Sub “video-worker” pour le transcodage SD/HD/UHD<br>• une tâche Pub/Sub “language-worker” par langue détectée |

---

## 🧱 Architecture GCP

```text
+--------------------------+
| Cloud Function:          |
|        orchestrator      |
+-----------+--------------+
            |
            | Pub/Sub
            v
  +------------------------+
  | Topic 1: verse-dev-433901-video-task  |
  |  → Cloud Function video-worker        |
  +--------------------------------------+
            |
            | Pub/Sub
            v
  +------------------------+
  | Topic 2: verse-dev-433901-lang-tasks  |
  |  → Cloud Function language-worker     |
  +--------------------------------------+
