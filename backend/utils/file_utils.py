import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from PIL import Image

from services.ocr_type_classifier import classify_ocr_type
from services.yolo_detector import detect_ui_elements_yolo
from utils.match_utils import assign_intent_semantic


def save_region(
    image: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    output_dir: str,
    page_name: str = "page",
    image_path: str = "",
) -> Dict[str, Any]:
    """
    Persist a cropped region to disk, leveraging YOLO to snap to the closest UI element.
    Returns metadata describing the saved region so downstream callers can reuse it.
    """
    detected_type = ""
    detected_confidence = 0.0

    if image_path and os.path.exists(image_path):
        try:
            x, y, w, h, detected_type, detected_confidence = detect_ui_elements_yolo(
                image_path, (x, y, w, h)
            )
        except Exception:
            detected_type = ""
            detected_confidence = 0.0

    # Clamp bounding box to image dimensions
    x = max(0, min(x, image.width - 1))
    y = max(0, min(y, image.height - 1))
    w = max(1, min(w, image.width - x))
    h = max(1, min(h, image.height - y))

    # Generate file name
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    filename = f"{page_name}_{x}_{y}_{w}_{h}_{timestamp}.png"
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    region_path = output_path / filename

    # Crop and save
    cropped = image.crop((x, y, x + w, y + h))
    cropped.save(str(region_path))

    return {
        "path": str(region_path),
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "detected_type": detected_type,
        "detected_confidence": detected_confidence,
    }


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w]+", "_", (text or "").strip().lower())).strip("_")


def _derive_intent(label_text: str, ocr_type: str, existing_intent: str) -> str:
    if existing_intent:
        return existing_intent

    semantic = assign_intent_semantic(label_text or "")
    if semantic:
        return semantic

    base = _slug(label_text)
    if not base:
        return ocr_type or "unknown"

    if ocr_type in ("textbox", "date"):
        return f"{base}_field"
    if ocr_type == "select":
        return f"{base}_select"
    if ocr_type == "checkbox":
        return f"{base}_checkbox"
    if ocr_type in ("button", "link"):
        return f"{base}_action"
    return base


def build_standard_metadata(
    element: dict,
    page_name: str,
    image_path: str = "",
    source_url: str = "",
) -> dict:
    label_text = element.get("label_text") or element.get("text") or ""
    ocr_type = (element.get("ocr_type") or "").strip().lower()
    detected_type = (element.get("detected_type") or "").strip().lower()
    intent = (element.get("intent") or "").strip()

    if not ocr_type or ocr_type == "unknown":
        ocr_type = detected_type or ocr_type

    if (not ocr_type or ocr_type == "unknown") and image_path:
        classified = classify_ocr_type(image_path)
        if classified and classified != "unknown":
            ocr_type = classified

    intent = _derive_intent(label_text, ocr_type, intent)
    unique_name = generate_unique_name(page_name, label_text, ocr_type, intent)

    metadata = {
        "page_name": page_name,
        "label_text": label_text,
        "ocr_type": ocr_type,
        "intent": intent,
        "unique_name": unique_name,
        "external": False,
        "dom_matched": element.get("dom_matched", False),
        "region_image_path": image_path,
        "source_url": source_url,
        "confidence_score": element.get("confidence_score", element.get("detected_confidence", 1.0)),
        "visibility_score": element.get("visibility_score", 1.0),
        "locator_stability_score": element.get("locator_stability_score", 1.0),
        "id": element.get("id") or element.get("ocr_id") or element.get("element_id", ""),
        "ocr_id": element.get("ocr_id") or element.get("id") or element.get("element_id", ""),
        "text": element.get("text") or label_text,
        "x": element.get("x", element.get("boundingBox", {}).get("x", 0)),
        "y": element.get("y", element.get("boundingBox", {}).get("y", 0)),
        "width": element.get("width", element.get("boundingBox", {}).get("width", 0)),
        "height": element.get("height", element.get("boundingBox", {}).get("height", 0)),
        "used_in_tests": element.get("used_in_tests", []),
        "last_tested": element.get("last_tested", ""),
        "healing_success_rate": element.get("healing_success_rate", 0.0),
        "snapshot_id": element.get("snapshot_id", ""),
        "match_timestamp": element.get("match_timestamp", ""),
        "bbox": element.get(
            "bbox",
            f"{element.get('x', 0)},{element.get('y', 0)},{element.get('width', 0)},{element.get('height', 0)}",
        ),
        "position_relation": element.get("position_relation", {}),
        "tag_name": element.get("tag_name", ""),
        "xpath": element.get("xpath", ""),
        "get_by_text": element.get("get_by_text", ""),
        "get_by_role": element.get("get_by_role", ""),
        "html_snippet": element.get("html_snippet", ""),
        "placeholder": element.get("placeholder", ""),
        "detected_type": detected_type,
        "detected_confidence": element.get("detected_confidence", 0.0),
    }

    return sanitize_metadata(metadata)


def generate_unique_name(page_name: str, label_text: str, ocr_type: str, intent: str) -> str:
    slug_label = _slug(label_text)
    slug_intent = _slug(intent)
    slug_type = _slug(ocr_type)

    if slug_label:
        return "_".join(filter(None, (page_name, slug_label, slug_type, slug_intent)))

    unique_str = "_".join(filter(None, (page_name, slug_type, slug_intent)))
    digest = hashlib.sha256(unique_str.encode("utf-8")).hexdigest()[:8]
    return f"{unique_str}_{digest}"


def sanitize_metadata(metadata: dict) -> dict:
    def safe_convert(value):
        if isinstance(value, (str, int, float, bool)):
            return value
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return str(value)
        return str(value)

    return {k: safe_convert(v) for k, v in metadata.items()}


def clean_old_files(directory: str, age_seconds: int = 3600):
    """
    Deletes files older than `age_seconds` from the given directory.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return

    now = time.time()
    for file in dir_path.glob("*"):
        if file.is_file():
            file_age = now - file.stat().st_mtime
            if file_age > age_seconds:
                try:
                    file.unlink()
                except Exception as e:
                    print(f"[CLEANUP ERROR] Failed to delete {file}: {e}")
