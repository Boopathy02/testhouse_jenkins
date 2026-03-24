import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_PATH = os.path.abspath(os.path.join(BASE_DIR, ".."))

# Lazy path helpers; do not create folders at import-time
DATA_PATH = os.path.join(ROOT_PATH, "data")
REGION_PATH = os.path.join(DATA_PATH, "regions")
CHROMA_PATH = os.path.join(DATA_PATH, "chroma_db")

def get_data_path() -> str:
    proj = os.environ.get("SMARTAI_PROJECT_DIR")
    if proj:
        return os.path.join(proj, "data")
    return DATA_PATH

def get_region_path() -> str:
    proj = os.environ.get("SMARTAI_PROJECT_DIR")
    if proj:
        return os.path.join(proj, "data", "regions")
    return REGION_PATH

def get_chroma_path() -> str:
    # Prefer explicit SMARTAI_CHROMA_PATH, then project-scoped, then repo-level
    explicit = os.environ.get("SMARTAI_CHROMA_PATH")
    if explicit:
        return explicit
    proj = os.environ.get("SMARTAI_PROJECT_DIR")
    if proj:
        return os.path.join(proj, "data", "chroma_db")
    return CHROMA_PATH
