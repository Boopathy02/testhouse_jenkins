from typing import Optional, Tuple
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from config.settings import get_chroma_path

_CLIENT: Optional[chromadb.PersistentClient] = None
_CLIENT_PATH: Optional[str] = None
_COLLECTIONS = {}


def get_chroma_client(chromaPath : str) -> chromadb.PersistentClient:
    """Return a PersistentClient for the current chroma path.

    If the path changed since last call, re-create the client and clear cached collections.
    """
    global _CLIENT, _CLIENT_PATH, _COLLECTIONS
    path = chromaPath
    if _CLIENT is None or _CLIENT_PATH != path:
        _CLIENT = chromadb.PersistentClient(path=path)
        _CLIENT_PATH = path
        _COLLECTIONS = {}
    return _CLIENT


def get_collection(chromaPath : str , name: str, embedding_function=None):
    global _COLLECTIONS
    key = (chromaPath , name, id(embedding_function))
    if key in _COLLECTIONS:
        return _COLLECTIONS[key]
    client = get_chroma_client(chromaPath)
    coll = client.get_or_create_collection(name=name, embedding_function=embedding_function)
    _COLLECTIONS[key] = coll
    return coll


def reset_chroma_client() -> None:
    """Clear cached chromadb client/collections so a new path can be re-initialized."""
    global _CLIENT, _CLIENT_PATH, _COLLECTIONS
    _CLIENT = None
    _CLIENT_PATH = None
    _COLLECTIONS = {}

