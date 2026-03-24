import os
import re
from pathlib import Path
from dotenv import load_dotenv
from utils.chroma_client import get_collection
from openai import OpenAI
from utils.match_utils import normalize_page_name
from utils.project_context import filter_metadata_by_project

load_dotenv()

def runtime_collection(chromaPath : str):
    return get_collection(chromaPath , "element_metadata")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
openai_client = client  # <-- Add this line

def get_class_name(page_name: str) -> str:
    return f"Saucedemo_{page_name}Page"

def filter_all_pages(chromaPath : str):
    records = runtime_collection(chromaPath).get()
    metas = filter_metadata_by_project(records.get("metadatas", []))
    return list(set(normalize_page_name(meta.get("page_name", "unknown")) for meta in metas))

__all__ = ["openai_client", "runtime_collection", "filter_all_pages", "get_class_name"]
