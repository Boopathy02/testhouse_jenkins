# utils/match_utils.py
from sentence_transformers import SentenceTransformer, util
import difflib
import re
import os
from urllib.parse import urlparse


def find_best_match(target: str, ocr_entries: dict, threshold=0.8):
    best_score = 0
    best_id = None
    for entry_id, text in ocr_entries.items():
        score = difflib.SequenceMatcher(
            None, target.lower(), text.lower()).ratio()
        if score > threshold and score > best_score:
            best_score = score
            best_id = entry_id
    return best_id


def normalize_text(text: str) -> str:
    """
    Normalize text for embedding comparison.
    """
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', '', text)  # remove punctuation
    text = re.sub(r'\s+', ' ', text)     # collapse whitespace
    return text


def normalize_page_name(input_string: str) -> str:
    input_string = input_string.strip().lower()

    # Handle URLs
    if input_string.startswith("http"):
        parsed = urlparse(input_string)
        path = parsed.path.strip("/")

        # Just use the last non-empty path segment, or "login" as fallback
        if path:
            segments = [seg for seg in path.split("/") if seg]
            page = segments[-1] if segments else "login"
            page = re.sub(r'\.html?$', '', page)
            page = re.sub(r'_\d+$', '', page)
        else:
            page = "login"
        return page

    # Handle images: strip extension and trailing _<digits>
    if input_string.endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")):
        base = re.sub(r'\.(png|jpg|jpeg|bmp|gif|webp)$', '', input_string)
        base = re.sub(r'_\d+$', '', base)
        return base.lower()

    # Fallback: just remove trailing _<digits> for anything else
    return re.sub(r'_\d+$', '', input_string)


def generalize_label(label: str) -> str:
    """Map raw field names to semantic equivalents like username/password."""
    label = normalize_text(label)
    if "user" in label or "email" in label or "login" in label:
        return "username"
    if "pass" in label or "pwd" in label:
        return "password"
    return label


intent_model = SentenceTransformer("all-MiniLM-L6-v2")

# Define common test intents and their typical label meanings
INTENT_TEMPLATES = {
    "fill_username": ["username", "user name", "email", "login id"],
    "fill_password": ["password", "passcode"],
    "click_login": ["login", "sign in", "submit", "continue"],
    "click_cart": ["cart", "basket"],
    "click_checkout": ["checkout", "place order"],
    "click_continue": ["continue", "next"],
    "click_finish": ["finish", "complete", "done"],
    "click_logout": ["logout", "sign out"],
}

intent_embeddings = {
    intent: intent_model.encode(
        labels, convert_to_tensor=True, show_progress_bar=False)
    for intent, labels in INTENT_TEMPLATES.items()
}


def assign_intent_semantic(label_text: str) -> str:
    label_embedding = intent_model.encode(
        label_text, convert_to_tensor=True, show_progress_bar=False)

    best_intent = None
    best_score = -1

    for intent, embeddings in intent_embeddings.items():
        score = util.pytorch_cos_sim(label_embedding, embeddings).max().item()
        if score > best_score:
            best_score = score
            best_intent = intent

    return best_intent if best_score > 0.6 else None
