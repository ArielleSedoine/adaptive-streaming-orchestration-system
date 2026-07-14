from cloudevents.http import CloudEvent
import json
import tempfile
import os
import logging
from google.cloud import storage
from langdetect import detect_langs  # pip install langdetect

# --- CONFIGURATION ---
DEST_BUCKET_NAME = "vodprocessedgcp"

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def timestamp_to_vtt(ts):
    # Supporte '00:00:41,580' ou '00:00:41.580'
    if not ts:
        return ""
    return ts.replace(',', '.').strip()


def convert_custom_json_to_vtt(json_array, text_field="translated_text"):
    vtt_lines = ["WEBVTT\n"]
    for item in json_array:
        start = timestamp_to_vtt(item.get("start_timestamp", ""))
        end = timestamp_to_vtt(item.get("end_timestamp", ""))
        if not start or not end:
            continue
        text = (item.get(text_field) or "").strip()
        if not text:
            continue
        vtt_lines.append(f"{start} --> {end}")
        vtt_lines.append(text)
        vtt_lines.append("")  # ligne vide VTT
    return "\n".join(vtt_lines)


def build_sample_text(json_data, field="original_text", min_chars=80, max_items=20):
    """Construit un échantillon pour améliorer la détection de langue."""
    pieces = []
    for item in json_data:
        t = item.get(field, "")
        if t and isinstance(t, str):
            pieces.append(t.strip())
            if sum(len(p) for p in pieces) >= min_chars or len(pieces) >= max_items:
                break
    return " ".join(pieces)


def detect_language_from_text(sample_text):
    """Détecte la langue la plus probable avec langdetect."""
    try:
        if not sample_text or len(sample_text.strip()) < 3:
            return None
        probs = detect_langs(sample_text)
        if not probs:
            return None
        return probs[0].lang  # ex: "en", "fr", "es"
    except Exception as e:
        logger.warning(f"Échec détection langue (langdetect) : {e}")
        return None


def processtranscript(event):
    try:
        # 1) Parsing event
        data = event.data if hasattr(event, "data") else event
        if isinstance(data, bytes):
            data = json.loads(data.decode())
        logger.info(f"Event reçu : {data}")

        bucket_name = data.get('bucket')
        blob_name = data.get('name')
        if not bucket_name or not blob_name:
            logger.info("Event ignoré : champs 'bucket' ou 'name' manquants.")
            return '', 204

        if not blob_name.endswith('.json') or '/transcript/' not in blob_name:
            logger.info("Event ignoré : pas un transcript JSON.")
            return '', 204

        logger.info(f"Déclenchement Cloud Function pour : gs://{bucket_name}/{blob_name}")

        parts = blob_name.split('/')
        if len(parts) < 3:
            logger.error(f"Chemin inattendu : {blob_name}")
            return '', 204

        base_file_name = parts[0]  # ex: test301
        transcript_file = os.path.basename(blob_name)

        # 2) Télécharger le .json
        storage_client = storage.Client()
        src_bucket = storage_client.bucket(bucket_name)
        blob = src_bucket.blob(blob_name)
        if not blob.exists():
            logger.error(f"Le fichier {blob_name} n'existe pas dans {bucket_name}.")
            return '', 204

        local_json_path = os.path.join(tempfile.gettempdir(), transcript_file)
        blob.download_to_filename(local_json_path)
        logger.info(f"Transcript JSON téléchargé localement : {local_json_path}")

        # 3) Charger le JSON
        with open(local_json_path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)
        if not isinstance(json_data, list) or len(json_data) == 0:
            logger.error("Le JSON attendu est un tableau non vide d'éléments.")
            os.remove(local_json_path)
            return '', 204

        dest_bucket = storage_client.bucket(DEST_BUCKET_NAME)
        local_vtt_paths = []

        # Détection langue originale
        orig_lang = detect_language_from_text(build_sample_text(json_data, field="original_text"))

        # Langue du translated_text (champ "language" = langue traduite)
        trans_lang = None
        if any(item.get("translated_text") for item in json_data):
            trans_lang = json_data[0].get("language")
            if not trans_lang:
                trans_lang = detect_language_from_text(build_sample_text(json_data, field="translated_text"))

        # Construire la liste des sorties
        langs = []
        if orig_lang and any(item.get("original_text") for item in json_data):
            langs.append(("original_text", orig_lang))
        else:
            logger.warning("Langue originale indétectable → pas de VTT généré pour original_text.")

        if trans_lang and any(item.get("translated_text") for item in json_data):
            langs.append(("translated_text", trans_lang))

        # Générer et uploader VTTs
        for text_field, lang_code in langs:
            try:
                vtt_content = convert_custom_json_to_vtt(json_data, text_field=text_field)
                vtt_filename = f"{base_file_name}_{lang_code}.vtt"
                local_vtt_path = os.path.join(tempfile.gettempdir(), vtt_filename)
                with open(local_vtt_path, 'w', encoding='utf-8') as f:
                    f.write(vtt_content)
                caption_blob_path = f"{base_file_name}/caption/{vtt_filename}"
                caption_blob = dest_bucket.blob(caption_blob_path)
                caption_blob.upload_from_filename(local_vtt_path, content_type='text/vtt')
                logger.info(f"Fichier VTT ({text_field}, {lang_code}) uploadé : gs://{DEST_BUCKET_NAME}/{caption_blob_path}")
                local_vtt_paths.append(local_vtt_path)
            except Exception as e:
                logger.error(f"Erreur lors de la génération du VTT pour '{text_field}': {e}")

        # Nettoyage
        try:
            os.remove(local_json_path)
            for path in local_vtt_paths:
                os.remove(path)
        except Exception:
            pass

        logger.info("Traitement transcript > VTT terminé avec succès.")

    except Exception as exc:
        import traceback
        logger.error(f"Erreur inattendue : {exc}\n{traceback.format_exc()}")

    return '', 204
