from collections import deque
import json
import os
from typing import List
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def build_dependency_graph(ordered_images: List[str], output_path: str = "./data/navigation_graph.json") -> None:
    """
    Builds a simple directed graph based on ordered image names.
    Stores edges in JSON format: [ {"from": "image1.png", "to": "image2.png"}, ... ]
    """
    edges = []
    for i in range(len(ordered_images) - 1):
        edges.append({"from": ordered_images[i], "to": ordered_images[i + 1]})

    # Ensure the folder exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        with open(output_path, "w") as f:
            json.dump(edges, f, indent=2)
        logger.debug(f"[GRAPH] ✅ Dependency graph written to: {os.path.abspath(output_path)}")
        # logger.debug(f"[GRAPH] Contents:\n{json.dumps(edges, indent=2)}")
    except Exception as e:
        logger.error(f"[GRAPH] ❌ Failed to write dependency graph: {e}")


def read_dependency_graph(input_path: str = "./data/navigation_graph.json") -> List[dict]:
    """
    Reads the stored navigation graph.
    Returns list of edges.
    """
    try:
        with open(input_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[GRAPH] ❌ Error reading graph: {e}")
        return []


def find_path(graph, start, end):
    queue = deque([[start]])
    visited = set()

    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == end:
            return path
        if node not in visited:
            visited.add(node)
            for neighbor in graph.get(node, []):
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
    return []

def get_adjacency_list(edges: List[dict]) -> dict:
    graph = {}
    for edge in edges:
        graph.setdefault(edge["from"], []).append(edge["to"])
    return graph
