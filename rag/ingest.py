"""Builds the knowledge base: reads the Markdown documents under data/, splits them into
chunks, embeds them locally and stores them in Chroma. Run it again after editing any document.

    python rag/ingest.py
"""

import os
import re
import glob
import chromadb
from chromadb.utils import embedding_functions


DOC_FOLDERS = [
    "data/policies",
    "data/faq",
    "data/docs",
]

CHROMA_PERSIST_DIR = "rag/chroma_store"
COLLECTION_NAME = "knowledge_base"

EMBEDDING_MODEL = "all-MiniLM-L6-v2"

CHUNK_SIZE = 800


def load_documents():
    """Read every .md file in the document folders."""
    documents = []
    for folder in DOC_FOLDERS:
        for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            documents.append({"source": os.path.basename(path), "text": text})
    return documents


def chunk_text(text: str, source: str = "", chunk_size: int = CHUNK_SIZE):
    """Split one document into chunks that each start with 'Document > Section'.

    FAQ entries become one chunk per question; other sections are split on
    paragraph boundaries at about `chunk_size` characters.
    """
    lines = text.strip().splitlines()
    doc_title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else source
    doc_title = re.sub(r"\s*\(FICTIONAL DEMO DOCUMENT\)", "", doc_title)

    sections = []
    heading, body = "Overview", []
    for line in lines[1:] if lines and lines[0].startswith("# ") else lines:
        if line.startswith("## "):
            if "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading, body = line[3:].strip(), []
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))

    chunks = []
    for heading, body in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]

        if any(p.startswith("**Q:") for p in paragraphs):
            for p in paragraphs:
                question = re.match(r"\*\*Q:\s*(.*?)\*\*", p)
                label = question.group(1) if question else heading
                chunks.append(f"{doc_title} > {label}\n{p}")
            continue

        current = ""
        for p in paragraphs:
            if current and len(current) + len(p) + 2 > chunk_size:
                chunks.append(f"{doc_title} > {heading}\n{current}")
                current = p
            else:
                current = f"{current}\n\n{p}" if current else p
        if current:
            chunks.append(f"{doc_title} > {heading}\n{current}")
    return chunks


def build_knowledge_base():
    """Rebuild the collection from scratch."""
    print(f"Loading documents from: {DOC_FOLDERS}")
    documents = load_documents()
    print(f"Found {len(documents)} documents.")

    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)

    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )

    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"Deleted existing collection '{COLLECTION_NAME}' to rebuild fresh.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
    )

    all_chunks = []
    all_ids = []
    all_metadatas = []

    for doc in documents:
        chunks = chunk_text(doc["text"], source=doc["source"])
        print(f"  {doc['source']}: {len(chunks)} chunks")
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{doc['source']}-{i}")
            all_metadatas.append({
                "source": doc["source"],
                "chunk_index": i,
                "section": chunk.splitlines()[0],
            })

    collection.add(documents=all_chunks, ids=all_ids, metadatas=all_metadatas)

    print(f"\nDone. {len(all_chunks)} chunks embedded and stored in "
          f"'{CHROMA_PERSIST_DIR}' under collection '{COLLECTION_NAME}'.")
    return collection


if __name__ == "__main__":
    build_knowledge_base()
