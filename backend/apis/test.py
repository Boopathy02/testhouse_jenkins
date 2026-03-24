
import numpy as np
import sys
from pathlib import Path

from posthog import page
sys.path.append(str(Path(__file__).resolve().parents[1]))
import json
import orjson
from utils.match_utils import normalize_page_name
from utils.chroma_client import get_collection


from pathlib import Path

# This works regardless of where the script is run from and honors active project
collection = get_collection("element_metadata")
records = collection.get()
page_names = list({meta.get("page_name", "unknown") for meta in records.get("metadatas", [])})

print(page_names)




# Helper function
def convert_np(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.float32, np.float64)):
        return float(obj)
    if isinstance(obj, (np.int32, np.int64)):
        return int(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

