"""Finds the document chunks most relevant to a question (Chroma vector search).

Chunks farther than MAX_RELEVANT_DISTANCE are dropped, so an unrelated question
returns nothing and ends up as 'information not available' instead of a guess.
"""

import chromadb
from chromadb.utils import embedding_functions

CHROMA_PERSIST_DIR = "rag/chroma_store"
COLLECTION_NAME = "knowledge_base"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

DEFAULT_TOP_K = 3

MAX_RELEVANT_DISTANCE = 1.2


def _get_collection():
    """Open the knowledge-base collection built by rag/ingest.py."""
    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )
    try:
        return client.get_collection(name=COLLECTION_NAME, embedding_function=embedding_fn)
    except Exception as e:
        raise RuntimeError(
            "Knowledge base collection not found. Run `python rag/ingest.py` "
            "first to build it."
        ) from e


def retrieve(query: str, top_k: int = DEFAULT_TOP_K, verbose: bool = True):
    """Search the knowledge base. Returns the query, the relevant chunks and a text block for the prompt.
    """
    collection = _get_collection()

    results = collection.query(query_texts=[query], n_results=top_k)

    raw_chunks = results["documents"][0]
    raw_metadatas = results["metadatas"][0]
    raw_distances = results["distances"][0]

    chunks = []
    for text, metadata, distance in zip(raw_chunks, raw_metadatas, raw_distances):
        if distance <= MAX_RELEVANT_DISTANCE:
            chunks.append({
                "text": text,
                "source": metadata["source"],
                "section": metadata.get("section", metadata["source"]),
                "distance": round(distance, 4),
            })

    context = "\n\n".join(
        f"[Chunk {i+1} | source: {c['source']}]\n{c['text']}"
        for i, c in enumerate(chunks)
    )

    if verbose:
        print(f"\n[RAG] Query: {query!r}")
        if not chunks:
            print("[RAG] No chunks within relevance threshold "
                  f"(MAX_RELEVANT_DISTANCE={MAX_RELEVANT_DISTANCE}). "
                  "Nothing relevant found — this should surface as "
                  "'information not available', not a guess.")
        for i, c in enumerate(chunks):
            print(f"[RAG] Chunk {i+1} (source: {c['source']}, "
                  f"distance: {c['distance']} — lower is more relevant)")
            preview = c["text"][:150].replace("\n", " ")
            print(f"        {preview}...")

    return {"query": query, "chunks": chunks, "context": context}


if __name__ == "__main__":
    test_queries = [
        "Can I return a product after opening it?",
        "What is your shipping policy?",
        "What is your policy on returning shoes?",
    ]
    for q in test_queries:
        result = retrieve(q)
        print(f"[RAG] --> {len(result['chunks'])} relevant chunk(s) returned.\n")
