# image_text_extractor.py
# ############################ Open AI Logic for Image API ############################

import os
import base64
import io
import uuid
from pathlib import Path
from dotenv import load_dotenv
import json
from datetime import datetime
import re
from PIL import Image
from openai import OpenAI

from config.settings import get_region_path
from utils.file_utils import save_region, build_standard_metadata
from utils.match_utils import normalize_page_name
from services.chroma_service import upsert_text_record

# ------------------------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------------------------
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT_FILE_PATH = os.path.join(os.path.dirname(__file__), '..', 'prompts', 'image_text_extraction.txt')
with open(PROMPT_FILE_PATH, 'r') as f:
    PROMPT = f.read()

# ------------------------------------------------------------------------------------
# Helper normalization functions
# ------------------------------------------------------------------------------------

def _normalize_separators(s: str) -> str:
    """Normalize separators like ':' and '-', tidy spaces, remove bullets."""
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"^[\s>*•\-]+\s*", "", s)
    s = re.sub(r"\s*:\s*", " - ", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def _clean_line(line: str) -> str:
    """Remove numbering/markdown and normalize separators."""
    line = re.sub(r"^\s*(\(?\d+\)?[.)]\s*)", "", line)
    line = re.sub(r"(\*\*|\*|`|__|_)", "", line)
    return _normalize_separators(line)


def _snake(s: str) -> str:
    """Convert arbitrary label text to lowercase_snake_case."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^A-Za-z0-9 ]+", "", s)
    s = s.lower().strip()
    return re.sub(r"\s+", "_", s)


def build_intent(label_text: str, ocr_type: str) -> str:
    """Deterministic, taxonomy-free intent built from label_text + ocr_type."""
    t = (ocr_type or "").strip().lower()
    base = _snake(label_text)
    if t == "textbox":
        return f"{base}_field" if base else "field"
    if t == "select":
        return f"{base}_select" if base else "select"
    if t == "checkbox":
        return f"{base}_checkbox" if base else "checkbox"
    if t in ("button", "link"):
        return f"{base}_action" if base else "action"
    if t == "label":
        return f"{base}_info" if base else "info"
    return base or "unknown"


def detect_likely_select(orig_line: str, label_text: str, ocr_type: str) -> str:
    """Heuristic to detect dropdowns/selects that the LLM labelled as textbox/label.

    Looks for common keywords (select, dropdown, choose, option) and visual
    arrow characters often used in UI dropdowns. Returns a possibly-updated
    ocr_type (usually 'select' or the original).
    """
    try:
        s = (orig_line or "") + " " + (label_text or "")
        s_l = s.lower()
        # keywords indicating a select/dropdown
        kws = ("select", "dropdown", "choose", "choose an", "choose a", "pick", "option", "options")
        if any(k in s_l for k in kws):
            return "select"

        # common arrow glyphs used in dropdown UI elements
        arrows = set(["▾", "▿", "▼", "˅", "˄", "▸", "▶", "⌄", "˅", "ˇ"])
        if any(ch in (orig_line or "") for ch in arrows):
            return "select"

        # if LLM guessed 'label' or 'textbox' but the label contains 'option: ' patterns
        if ocr_type and ocr_type.lower() in ("textbox", "label") and "option" in s_l:
            return "select"

    except Exception:
        pass
    return ocr_type

# ------------------------------------------------------------------------------------
# Main image processor
# ------------------------------------------------------------------------------------

async def process_image_gpt(
    image: Image.Image,
    filename: str,
    image_path: str = "",
    regionsPath: str = "",
    projectChromaPath :str="",
    debug_log_path: str = None
) -> list:

    page_name = normalize_page_name(filename)

    def _call_vision(img: Image.Image) -> tuple[list[str], list[str]]:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                ],
            }],
            max_tokens=4000,
            temperature=0
        )

        raw = (response.choices[0].message.content or "").strip()
        raw_lines = raw.splitlines() if raw else []

        clean_lines: list[str] = []
        for line in raw_lines:
            if not line.strip():
                continue
            line = _clean_line(line)
            if line:
                clean_lines.append(line)
        return raw_lines, clean_lines

    # Use tiling for large images to avoid missing text on dense screens.
    tile_size = 1200
    overlap = 80
    w, h = image.size
    tiles: list[Image.Image] = []
    if w > tile_size or h > tile_size:
        step = max(1, tile_size - overlap)
        for y in range(0, h, step):
            for x in range(0, w, step):
                box = (x, y, min(x + tile_size, w), min(y + tile_size, h))
                tiles.append(image.crop(box))
    else:
        tiles.append(image)

    raw_lines: list[str] = []
    clean_lines: list[str] = []
    seen = set()
    for tile in tiles:
        raw_part, clean_part = _call_vision(tile)
        raw_lines.extend(raw_part)
        for line in clean_part:
            key = line.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            clean_lines.append(line)

    # Save audit logs
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = os.path.splitext(os.path.basename(filename))[0]
        file_name = f"{timestamp}_{base_name}.txt"
        folder = "data/openai_response"
        os.makedirs(folder, exist_ok=True)
        out_file = os.path.join(folder, file_name)
        with open(out_file, "w", encoding="utf-8") as f:
            f.write("---- RAW ----\n")
            for l in raw_lines:
                f.write((l or "") + "\n")
            f.write("\n---- CLEANED ----\n")
            for l in clean_lines:
                f.write((l or "") + "\n")
    except Exception:
        pass

    results = []
    known_types = {"textbox", "button", "label", "checkbox", "select", "dropdown", "combobox", "link"}
    regions_dir = Path(regionsPath)
    regions_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------
    # Robust parsing with type detection (handles hyphens and colons)
    # ---------------------------------------------------------------
    for orig in clean_lines:
        line = orig.strip()
        if not line:
            continue

        tokens = [t.strip() for t in line.split(" - ") if t and t.strip()]
        if not tokens:
            continue

        label_text, ocr_type, intent = "", "", ""

        # Detect type position dynamically
        ocr_index = next((i for i, t in enumerate(tokens) if t.lower() in known_types), -1)

        if ocr_index != -1:
            label_text = " - ".join(tokens[:ocr_index]).strip()
            ocr_type = tokens[ocr_index].lower()
            intent_part = tokens[ocr_index + 1:] if ocr_index + 1 < len(tokens) else []
            intent = " ".join(intent_part).strip()
        else:
            # Fallback: always keep the line; treat it as a label when type is unknown.
            if len(tokens) == 2:
                label_text, ocr_type = tokens
            elif len(tokens) == 1:
                label_text = tokens[0]
                ocr_type = "label"
            else:
                label_text = " - ".join(tokens).strip()
                ocr_type = "label"

        # Clean fake labels like 'textbox' or 'button'
        if label_text.lower() in known_types:
            label_text = ""

        ocr_type = ocr_type.lower().strip()
        label_text = label_text.strip()

        # Heuristic: some dropdowns are mislabelled by the LLM. Detect and fix.
        new_type = detect_likely_select(orig, label_text, ocr_type)
        if new_type and new_type != ocr_type:
            ocr_type = new_type

        # Deterministic intent
        intent = build_intent(label_text, ocr_type)

        if intent in ("unknown", "action", "field", "select", "checkbox", "info"):
            print(f"[INTENT-NOTE] Generic intent → label='{label_text}' type='{ocr_type}' line='{orig}'")

        # Dummy bounding box (placeholder until detector)
        unique_id = str(uuid.uuid4())
        x, y, w, h = 10, 10, 100, 40

        region_path = save_region(
            image, x, y, w, h,
            str(regions_dir),
            page_name,
            image_path=image_path
        )

        element = {
            "label_text": label_text,
            "ocr_type": ocr_type,
            "intent": intent,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "bbox": f"{x},{y},{w},{h}",
            "confidence_score": 1.0,
            "data_id" : "",
        }

        metadata = build_standard_metadata(
            element,
            page_name,
            image_path=region_path
        )
        metadata["id"] = unique_id
        metadata["ocr_id"] = unique_id
        metadata["get_by_text"] = label_text

        # Store metadata
        try:
            stored_metadata = upsert_text_record(projectChromaPath , metadata)
            results.append(stored_metadata)
        except Exception as e:
            print(f"[ERROR] Failed to upsert to ChromaDB for label='{label_text}': {e}")

        # Optional debug log
        if debug_log_path:
            try:
                with open(debug_log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(metadata, ensure_ascii=False) + "\n")
            except Exception:
                pass

    return results    
