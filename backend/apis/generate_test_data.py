from fastapi import FastAPI
import uvicorn

router=APIRouter()

from fastapi import APIRouter, HTTPException
from pathlib import Path
import json
from services.test_generation_utils import collection  # <-- Your chromadb client
import os

router = APIRouter()

@router.post("/generate-test-data-from-chromadb")
def generate_test_data_from_chromadb():
    # Query all metadata
    all_records = collection.get()
    metadatas = all_records.get("metadatas", [])
    
    # Filter for textbox or select, and build test data dict
    test_data = {}
    for meta in metadatas:
        ocr_type = (meta.get("ocr_type") or "").lower()
        label = meta.get("label_text") or meta.get("intent") or ""
        label = label.strip().replace(" ", "_").lower()
        if ocr_type in {"textbox", "select"} and label:
            test_data[label] = "fakedata"
    
    # Directory and file path (project-scoped)
    proj = os.environ.get("SMARTAI_PROJECT_DIR")
    if not proj:
        raise HTTPException(status_code=400, detail="No active project. Start a project to set SMARTAI_PROJECT_DIR.")
    data_dir = Path(proj) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_data_file = data_dir / "test_data.json"
    
    # Write out the JSON
    with open(test_data_file, "w", encoding="utf-8") as f:
        json.dump(test_data, f, indent=2)
    
    return {"status": "success", "file": str(test_data_file), "test_data": test_data}
