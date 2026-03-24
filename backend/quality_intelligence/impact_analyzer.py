"""
Map detected code changes to impacted tests using ChromaDB similarity search.
"""

from typing import Dict, List

from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from utils.chroma_client import get_collection
from .schemas import ImpactAnalysisResult


class ImpactAnalyzer:
    """Analyzes changed code units to identify impacted tests via embeddings."""

    def analyze(self, changed_code_units: List[Dict]) -> ImpactAnalysisResult:
        impacted_tests: List[str] = []

        embedding_fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        collection = get_collection("codebase_index", embedding_function=embedding_fn)

        for unit in changed_code_units or []:
            if isinstance(unit, (list, tuple)):
                unit = unit[0] if unit else {}
            if not isinstance(unit, dict):
                unit = {}
            code_content = unit.get("code_content") or ""
            if not code_content.strip():
                continue
            try:
                query = collection.query(
                    query_texts=[code_content],
                    n_results=8,
                    where={"unit_type": "TEST_CODE"},
                    include=["metadatas"],
                )
            except Exception:
                continue
            for meta in (query.get("metadatas") or [[]])[0]:
                if not isinstance(meta, dict):
                    continue
                unit_name = meta.get("unit_name") or ""
                file_path = meta.get("file_path") or ""
                if not unit_name and not file_path:
                    continue
                impacted_tests.append(f"{file_path}::{unit_name}".strip(":"))

        impacted_tests = list(dict.fromkeys(impacted_tests))

        return ImpactAnalysisResult(
            impacted_tests=impacted_tests,
            impacted_pages=[],
            reason="Derived from git change detector + ChromaDB codebase index.",
        )
