import json
from pathlib import Path
from chromadb import PersistentClient

# Path to your existing ChromaDB directory
chroma_path = Path(__file__).resolve().parents[0] / "data" / "chroma_db"

# Connect to ChromaDB
client = PersistentClient(path=str(chroma_path))
collection = client.get_or_create_collection("element_metadata")

# Fetch all records
records = collection.get()

# Extract relevant data
output = []
for i in range(len(records["ids"])):
    entry = {
        "id": records["ids"][i],
        "document": records["documents"][i],
        "metadata": records["metadatas"][i]
    }
    output.append(entry)

# Write to JSON
output_path = Path("chromadb_export.json")
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"utils/export_chromaDB.py ✅ ChromaDB exported to {output_path}")
