# chroma_services.py

from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from utils.chroma_client import get_collection
from utils.project_context import current_project_id
from fastapi.concurrency import run_in_threadpool
from services.ocr_type_classifier import classify_ocr_type
import logging
import json

# Setup embedding function; collection resolved at call-time for active project
embedding_function = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

# Logger
error_logger = logging.getLogger("chroma_upsert_errors")
error_logger.setLevel(logging.WARNING)
handler = logging.FileHandler("chroma_upsert_errors.log")
handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
error_logger.addHandler(handler)

def _sanitize_metadata_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value

def upsert_text_record(chromaPath , record: dict):
    # print(f"[DEBUG] Upserting OCR record: {record}")
    bbox_values = record.get('bbox') or [0, 0, 0, 0]
    bbox_str = ",".join(map(str, bbox_values))

    # Only persist application-facing fields in OCR metadata
    metadata = {
        "page_name": _sanitize_metadata_value(record.get("page_name")),
        "label_text": _sanitize_metadata_value(record.get("label_text")),
        "ocr_type": _sanitize_metadata_value(record.get("ocr_type", "")),
        "intent": _sanitize_metadata_value(record.get("intent")),
        "external": _sanitize_metadata_value(record.get("external")),
        "dom_matched": _sanitize_metadata_value(record.get("dom_matched")),
        "placeholder": _sanitize_metadata_value(record.get("placeholder") or record.get("label_text")),
        "get_by_text": _sanitize_metadata_value(record.get("get_by_text") or record.get("label_text")),
        "type": "ocr",
        # Keep stable identifiers available to consumers
        "unique_name": _sanitize_metadata_value(record.get("unique_name")),
        "element_id": _sanitize_metadata_value(record.get("element_id") or record.get("id")),
    }
    pid = current_project_id()
    if pid is not None:
        metadata["project_id"] = pid
    
    # ---- DUPLICATE CHECK START ----
    # 1. Query by the broadest field (the one with the most candidates)
    collection = get_collection(chromaPath , "element_metadata", embedding_function)
    possible_matches = collection.get(
        where={"label_text": metadata.get("label_text")},
        include=["metadatas"]
    )

    # 2. Loop through matches and check all fields
    for idx, meta in enumerate(possible_matches.get("metadatas", [])):
        if (
            meta.get("page_name") == metadata.get("page_name") and
            meta.get("ocr_type") == metadata.get("ocr_type") and
            meta.get("intent") == metadata.get("intent")
        ):
            existing_id = possible_matches["ids"][idx]
            existing_record = collection.get(ids=[existing_id], include=["documents", "metadatas", "embeddings"])
            return {
                "id": existing_record["ids"][0],
                "document": existing_record["documents"][0],
                "metadata": existing_record["metadatas"][0],
                # "embedding": existing_record["embeddings"][0]
            }
    # ---- DUPLICATE CHECK END ----

    try:
        text_to_embed = build_embedding_text(record)
        embedding_value = embedding_function([text_to_embed])[0]
        collection.upsert(
            ids=[record["id"]],
            documents=[metadata["label_text"]],
            metadatas=[metadata],
            embeddings=[embedding_value],
        )
        
        # Fetch what was actually stored
        stored_data = collection.get(ids=[record["id"]], include=["documents", "metadatas", "embeddings"]
        )

        # Return it as a structured object
        return {
            "id": stored_data["ids"][0],
            "document": stored_data["documents"][0],
            "metadata": stored_data["metadatas"][0],
            # "embedding": stored_data["embeddings"][0]
        }

    except Exception as e:
        error_logger.warning(f"upsert_text_record failed: {str(e)} | Record: {record}")

def build_embedding_text(record: dict) -> str:
    parts = [
        record.get("label_text", "").strip(),
        record.get("ocr_type", "").strip(),
        record.get("intent", "").strip(),
        record.get("page_name", "").strip()
    ]
    # Remove empty fields and join with a space
    return " | ".join([p for p in parts if p])

def upsert_element_record(record: dict):
    document_content = record.get("html_snippet") or record.get("label_text") or record.get("intent")

    metadata = {
        "element_id": _sanitize_metadata_value(record.get("element_id")),
        "page_name": _sanitize_metadata_value(record.get("page_name")),
        "intent": _sanitize_metadata_value(record.get("intent")),
        "tag": _sanitize_metadata_value(record.get("tag")),
        "label_text": _sanitize_metadata_value(record.get("label_text")),
        "css_selector": _sanitize_metadata_value(record.get("css_selector")),
        "get_by_text": _sanitize_metadata_value(record.get("get_by_text")),
        "get_by_role": _sanitize_metadata_value(record.get("get_by_role")),
        "xpath": _sanitize_metadata_value(record.get("xpath")),
        "x": record.get("x") or 0,
        "y": record.get("y") or 0,
        "width": record.get("width") or 0,
        "height": record.get("height") or 0,
        "position_relation": _sanitize_metadata_value(record.get("position_relation")),
        "html_snippet": _sanitize_metadata_value(record.get("html_snippet")),
        "confidence_score": record.get("confidence_score") or 0.0,
        "visibility_score": record.get("visibility_score") or 0.0,
        "locator_stability_score": record.get("locator_stability_score") or 0.0,
        "snapshot_id": _sanitize_metadata_value(record.get("snapshot_id")),
        "timestamp": _sanitize_metadata_value(record.get("timestamp")),
        "source_url": _sanitize_metadata_value(record.get("source_url")),
        "used_in_tests": _sanitize_metadata_value(record.get("used_in_tests")),
        "last_tested": _sanitize_metadata_value(record.get("last_tested")),
        "healing_success_rate": record.get("healing_success_rate") or 0.0,
        "type": "locator"
    }
    pid = current_project_id()
    if pid is not None:
        metadata["project_id"] = pid

    try:
        collection = get_collection("element_metadata", embedding_function)
        embedding_value = record.get("combined_embedding") or record.get("text_embedding")
        if not embedding_value:
            embedding_value = embedding_function([document_content])[0]

        collection.upsert(
            documents=[document_content],
            metadatas=[metadata],
            embeddings=[embedding_value],
            ids=[record["element_id"]]
        )
    except Exception as e:
        error_logger.warning(f"upsert_element_record failed: {str(e)} | Record: {record}")

def fetch_ocr_entries():
    try:
        collection = get_collection("element_metadata", embedding_function)
        results = collection.get(where={"type": "ocr"})
        ocr_entries = []
        for id_, doc, meta in zip(results["ids"], results["documents"], results["metadatas"]):
            ocr_entries.append({
                "id": id_,
                "text": doc,
                "page": meta.get("page_name", "")
            })

        # print(f"[FETCH OCR] Found {len(ocr_entries)} OCR entries")
        for entry in ocr_entries:
            # print(f"  ID: {entry['id']} | Text: {entry['text']} | Page: {entry['page']}")
            pass
        return ocr_entries
    except Exception as e:
        error_logger.warning(f"fetch_ocr_entries failed: {str(e)}")
        return []


def _update_locator_by_text_sync(entry_id: str, locator: str):
    """Synchronously update the locator field for a given record."""
    try:
        collection = get_collection("element_metadata", embedding_function)
        item = collection.get(ids=[entry_id])
        doc = item["documents"][0]
        meta = item["metadatas"][0]
        meta["locator"] = locator
        meta["source_type"] = "url"
        meta_sanitized = {k: _sanitize_metadata_value(v) for k, v in meta.items()}

        collection.upsert(
            documents=[doc],
            metadatas=[meta_sanitized],
            ids=[entry_id]
        )
    except Exception as e:
        error_logger.warning(f"_update_locator_by_text_sync failed: {str(e)} | ID: {entry_id}")


async def update_locator_by_text(entry_id: str, locator: str):
    await run_in_threadpool(_update_locator_by_text_sync, entry_id, locator)
