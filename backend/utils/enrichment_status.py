import json
from pathlib import Path
import os

def _status_file() -> Path | None:
    src = os.environ.get("SMARTAI_SRC_DIR")
    if not src:
        return None
    return Path(src) / "metadata" / "enrichment_status.json"


def load_status():
    sf = _status_file()
    if sf and sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            return {}
    return {}


def save_status(status):
    sf = _status_file()
    if not sf:
        # No active project; skip persisting status
        return
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(status, indent=2))
    except Exception:
        pass


def is_enriched(page_name):
    status = load_status()
    return status.get(page_name, False)


def set_enriched(page_name, value=True):
    status = load_status()
    status[page_name] = value
    save_status(status)


def reset_enriched(page_name):
    status = load_status()
    if page_name in status:
        del status[page_name]
        save_status(status)
