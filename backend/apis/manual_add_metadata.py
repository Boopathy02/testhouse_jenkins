from fastapi import APIRouter
from fastapi.responses import JSONResponse
from utils.chroma_client import get_collection

from pydantic import BaseModel, Field
from typing import Optional

import uuid
from datetime import datetime

router = APIRouter()



class ManualMetadataInput(BaseModel):
    page_name: str
    placeholder: Optional[str] = ""
    text: Optional[str] = ""
    label_text: Optional[str] = ""
    value: Optional[str] = ""
    intent: Optional[str] = ""
    ocr_type: Optional[str] = ""  # e.g., "button", "textbox", etc.
    tag_name: Optional[str] = ""
    dom_id: Optional[str] = ""
    dom_class: Optional[str] = ""

@router.post("/manual-add-metadata")
async def manual_add_metadata(input: ManualMetadataInput):
    # 1. Build full metadata from minimal input
    metadata = build_complete_metadata(input)
    # 2. Insert into ChromaDB
    collection = get_collection("element_metadata")
    collection.add(
        ids=[metadata["id"]],
        documents=[metadata["label_text"]],
        metadatas=[metadata]
    )
    # 3. Return the metadata for verification
    return JSONResponse(content=metadata)

def build_complete_metadata(manual: ManualMetadataInput):
    # You can add logic to auto-calculate bounding box or set dummy values
    bbox = "0,0,100,40"
    now = datetime.utcnow().isoformat()
    uid = str(uuid.uuid4())
    label_text = manual.label_text or manual.placeholder or manual.text or manual.value or ""
    unique_name = f"{manual.page_name}_{manual.intent}_{label_text}_{manual.ocr_type}".replace(" ", "_").lower()
    return {
        "element_id": uid,
        "ocr_id": uid,
        "page_name": manual.page_name,
        "text": manual.text or "",
        "label_text": label_text,
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 40,
        "confidence_score": 1.0,
        "visibility_score": 1.0,
        "locator_stability_score": 1.0,
        "used_in_tests": "[]",
        "last_tested": "",
        "healing_success_rate": 0.0,
        "snapshot_id": "",
        "match_timestamp": now,
        "region_image_path": "",
        "source_url": "",
        "bbox": bbox,
        "position_relation": "{}",
        "tag_name": manual.tag_name,
        "xpath": "",
        "get_by_text": manual.text or "",
        "get_by_role": "",
        "intent": manual.intent or "",
        "html_snippet": "",
        "dom_matched": False,
        "ocr_type": manual.ocr_type,
        "unique_name": unique_name,
        "placeholder": manual.placeholder or "",
        "external": True,
        "dom_class": manual.dom_class,
        "dom_id": manual.dom_id,
    }


# How to Use
# POST to /manual-add-metadata with body:
    page_name: str
    placeholder: Optional[str] = ""
    text: Optional[str] = ""
    label_text: Optional[str] = ""
    value: Optional[str] = ""
    intent: Optional[str] = ""
    ocr_type: Optional[str] = ""  # e.g., "button", "textbox", etc.
    tag_name: Optional[str] = ""
    dom_id: Optional[str] = ""
    dom_class: Optional[str] = ""

{
  "page_name": "saucedemo_inventory",
  "placeholder": "",
  "text": "",
  "label_text": "shopping_cart",
  "value": "",
  "intent": "go_to_cart",
  "ocr_type": "button",
  "dom_id": "",
  "dom_class": "shopping_cart"
}

{
  "page_name": "dashboard",
  "placeholder": "",
  "text": "customers",
  "label_text": "customers",
  "value": "",
  "intent": "",
  "ocr_type": "button",
  "tag_name": "a",
  "dom_id": "",
  "dom_class": ""
}
