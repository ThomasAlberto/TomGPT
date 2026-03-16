import hashlib
import logging
from pathlib import Path

import chromadb
import openai

logger = logging.getLogger(__name__)

CHROMA_DIR = Path(__file__).parent / "chroma_db"
_chroma_client = None
_openai_client = None


def init(openai_client: openai.OpenAI):
    global _chroma_client, _openai_client
    _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    _openai_client = openai_client
    logger.info("RAG initialized: chroma_db at %s", CHROMA_DIR)


def _get_client() -> chromadb.PersistentClient:
    if _chroma_client is None:
        raise RuntimeError("RAG not initialized — call rag.init() first")
    return _chroma_client


def get_collection(folder_id: str):
    name = f"folder_{folder_id.replace('-', '_')}"
    return _get_client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def delete_collection(folder_id: str):
    name = f"folder_{folder_id.replace('-', '_')}"
    try:
        _get_client().delete_collection(name=name)
        logger.info("Deleted collection %s", name)
    except Exception:
        pass


# ── Chunking ───────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap
    return chunks


# ── Embeddings ─────────────────────────────────────────────────────────────

EMBED_BATCH_SIZE = 512


def embed(texts: list[str]) -> list[list[float]]:
    if not _openai_client:
        raise RuntimeError("RAG not initialized")
    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        response = _openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=batch,
        )
        all_embeddings.extend(e.embedding for e in response.data)
    return all_embeddings


# ── Indexing ───────────────────────────────────────────────────────────────

def _content_hash(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()[:12]


def index_document(folder_id: str, filename: str, content: str) -> int:
    collection = get_collection(folder_id)
    file_hash = _content_hash(content)

    existing = collection.get(where={"source": filename})
    if existing["ids"]:
        stored_hash = existing["metadatas"][0].get("hash", "")
        if stored_hash == file_hash:
            logger.info("Skipping %s — already indexed (hash match)", filename)
            return 0
        collection.delete(ids=existing["ids"])
        logger.info("Re-indexing %s — content changed", filename)

    chunks = chunk_text(content)
    if not chunks:
        return 0

    embeddings = embed(chunks)
    ids = [f"{folder_id[:8]}_{filename}_{i}" for i in range(len(chunks))]
    metadatas = [{"source": filename, "hash": file_hash, "chunk_index": i} for i in range(len(chunks))]

    collection.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)
    logger.info("Indexed %s: %d chunks into folder %s", filename, len(chunks), folder_id[:8])
    return len(chunks)


def remove_document(folder_id: str, filename: str):
    collection = get_collection(folder_id)
    existing = collection.get(where={"source": filename})
    if existing["ids"]:
        collection.delete(ids=existing["ids"])
        logger.info("Removed %s (%d chunks) from folder %s", filename, len(existing["ids"]), folder_id[:8])


def list_documents(folder_id: str) -> list[dict]:
    collection = get_collection(folder_id)
    all_data = collection.get(include=["metadatas"])
    if not all_data["ids"]:
        return []

    doc_map: dict[str, int] = {}
    for meta in all_data["metadatas"]:
        name = meta["source"]
        doc_map[name] = doc_map.get(name, 0) + 1

    return [{"filename": name, "chunks": count} for name, count in doc_map.items()]


def collection_doc_count(folder_id: str) -> int:
    try:
        collection = get_collection(folder_id)
        return collection.count()
    except Exception:
        return 0


# ── Search ─────────────────────────────────────────────────────────────────

def search_folder_chain(folder_ids: list[str], query: str, top_k: int = 5) -> list[dict]:
    query_embedding = embed([query])[0]
    all_results = []

    for fid in folder_ids:
        try:
            collection = get_collection(fid)
            if collection.count() == 0:
                continue
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, collection.count()),
                include=["documents", "metadatas", "distances"],
            )
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                all_results.append({
                    "content": doc,
                    "source": meta["source"],
                    "score": 1 - dist,
                })
        except Exception as e:
            logger.warning("RAG search failed for folder %s: %s", fid[:8], e)

    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results[:top_k]
