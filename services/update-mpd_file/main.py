import os
import json
import logging
import base64
from flask import Flask, request, jsonify
from google.cloud import storage
from lxml import etree

# ------------------------------------
# CONFIG
# ------------------------------------
DESTINATION_BUCKET = "vodprocessedgcp"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# ------------------------------------
# HELPERS
# ------------------------------------

def get_signed_urls(bucket_name, folder_name):
    """Collecte tous les .txt (signed URLs) dans un dossier GCS."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    signed = {}

    for blob in bucket.list_blobs(prefix=folder_name):
        if not blob.name.endswith(".txt"):
            continue
        key = os.path.basename(blob.name)
        url = blob.download_as_text().strip()
        if url:
            signed[key] = url

    logger.info(f"🔐 {len(signed)} signed URLs trouvées sous {folder_name}")
    return signed


def get_manifest_blob(client, bucket_name, base_file):
    """Retourne le blob manifest.mpd pour un fichier donné."""
    bucket = client.bucket(bucket_name)
    path = f"{base_file}/dash/manifest.mpd"
    blob = bucket.blob(path)

    if not blob.exists():
        raise FileNotFoundError(f"❌ manifest.mpd absent : gs://{bucket_name}/{path}")

    return blob, path


# ------------------------------------
# CORE PROCESSING
# ------------------------------------

def process_manifest_for_basefile(base_file):
    """Met à jour manifest.mpd → manifest_updated.mpd avec des Signed URLs."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(DESTINATION_BUCKET)

    # 1) charger manifest.mpd
    dash_blob, dash_path = get_manifest_blob(storage_client, DESTINATION_BUCKET, base_file)
    dash_folder = f"{base_file}/dash/"
    logger.info(f"📄 Chargement : gs://{DESTINATION_BUCKET}/{dash_path}")

    # 2) charger signed URLs (.txt)
    signed_urls = get_signed_urls(DESTINATION_BUCKET, dash_folder)
    if not signed_urls:
        raise RuntimeError("❌ Aucun .txt trouvé — impossible de mettre à jour le MPD.")

    # 3) parser manifest.mpd
    xml = dash_blob.download_as_bytes()
    parser = etree.XMLParser(remove_blank_text=True, recover=True)
    root = etree.XML(xml, parser)
    ns = {"ns": "urn:mpeg:dash:schema:mpd:2011"}

    # 4) remplacement SegmentTemplate → SegmentList
    for adaptation in root.xpath(".//ns:AdaptationSet", namespaces=ns):
        st = adaptation.find("./ns:SegmentTemplate", namespaces=ns)
        if st is None:
            continue

        initialization = st.get("initialization")
        media = st.get("media")
        timescale = st.get("timescale", "1")
        start_number = int(st.get("startNumber", "1"))
        duration = st.get("duration", None)

        # Pour chaque Representation
        for rep in adaptation.xpath("./ns:Representation", namespaces=ns):
            rep_id = rep.get("id")

            seglist = etree.Element("SegmentList")
            seglist.set("timescale", timescale)
            seglist.set("startNumber", str(start_number))
            if duration and duration != "0":
                seglist.set("duration", duration)

            # INIT segment
            if initialization:
                init_filename = initialization.replace("$RepresentationID$", rep_id)
                init_txt = os.path.splitext(os.path.basename(init_filename))[0] + ".txt"
                signed_init = signed_urls.get(init_txt)

                if signed_init:
                    init_elem = etree.Element("Initialization")
                    init_elem.set("sourceURL", signed_init)
                    seglist.append(init_elem)
                else:
                    logger.warning(f"⚠️ Pas d’URL signée pour {init_txt}")

            # MEDIA segments
            if media and "$Number$" in media:
                num = 1
                while True:
                    seg_filename = (
                        media.replace("$RepresentationID$", rep_id)
                        .replace("$Number$", str(num))
                    )
                    seg_txt = os.path.splitext(os.path.basename(seg_filename))[0] + ".txt"
                    signed_seg = signed_urls.get(seg_txt)

                    if not signed_seg:
                        break

                    seg_elem = etree.Element("SegmentURL")
                    seg_elem.set("media", signed_seg)
                    seglist.append(seg_elem)

                    num += 1

            rep.append(seglist)

        # supprimer SegmentTemplate
        adaptation.remove(st)

    # 5) sauvegarder manifest_updated.mpd
    updated_blob = bucket.blob(f"{dash_folder}manifest_updated.mpd")
    updated_xml = etree.tostring(
        root,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8"
    )
    updated_blob.upload_from_string(updated_xml, content_type="application/xml")

    logger.info(f"✅ manifest_updated.mpd écrit dans gs://{DESTINATION_BUCKET}/{dash_folder}")


# ------------------------------------
# ENTRYPOINT (Eventarc → Cloud Run)
# ------------------------------------

@app.post("/")
def entrypoint():
    event = request.get_json(silent=True)
    if not event:
        logger.error("❌ No JSON event received")
        return jsonify({"error": "no event"}), 400

    logger.info(f"📩 Event reçu : {json.dumps(event)[:400]}")

    bucket = None
    name = None

    # CloudEvent (Eventarc → Cloud Run)
    if isinstance(event, dict) and "data" in event:
        data = event["data"]
        bucket = data.get("bucket")
        name = data.get("name")

    # Fallback possible
    if not bucket and "bucket" in event:
        bucket = event["bucket"]
    if not name and "name" in event:
        name = event["name"]

    if not bucket or not name:
        logger.error("❌ Impossible d'extraire bucket/name")
        return ("OK", 200)

    # Filtrer uniquement manifest.mpd
    if bucket != DESTINATION_BUCKET or not name.endswith("manifest.mpd"):
        logger.info("⏭ Pas un manifest.mpd → on ignore.")
        return ("OK", 200)

    # extraire base_file
    parts = name.split("/")
    if len(parts) < 3 or parts[-2] != "dash":
        logger.error(f"❌ structure inattendue : {name}")
        return ("OK", 200)

    base_file = parts[0]
    logger.info(f"🎯 update MPD pour base_file={base_file}")

    try:
        process_manifest_for_basefile(base_file)
        return ("OK", 200)
    except Exception as e:
        logger.exception(f"❌ Erreur mise à jour MPD : {e}")
        return ("Error (non-retry)", 200)


# LOCAL DEV
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
