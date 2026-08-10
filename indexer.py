import os
import base64
from dotenv import load_dotenv
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, Document
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.core import StorageContext
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.node_parser import CodeSplitter
from pinecone import Pinecone, ServerlessSpec

load_dotenv()

# Same embedding model used in main.py's retrieve_context() - must match,
# otherwise indexing and retrieval compare vectors from different embedding spaces.
EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMENSION = 384
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "codebase-index")


def _get_pinecone_index():
    pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
    existing = [idx["name"] for idx in pc.list_indexes()]
    if PINECONE_INDEX_NAME not in existing:
        pc.create_index(
            name=PINECONE_INDEX_NAME,
            dimension=EMBED_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
    return pc.Index(PINECONE_INDEX_NAME)


EXCLUDED_DIRS = {
    "venv", ".venv", "__pycache__", ".git", "node_modules", ".pytest_cache",
    "dist", "build", ".next"
}

# Extension -> tree-sitter grammar name (CodeSplitter needs one language per splitter)
EXT_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
}


def _find_source_files(repo_path: str) -> dict[str, list[str]]:
    """Walk the repo manually, pruning excluded directories before descending
    into them, grouping matches by language. SimpleDirectoryReader's `exclude`
    param does not reliably skip nested directories (e.g. backend/venv), so
    this is the safe alternative."""
    matches: dict[str, list[str]] = {lang: [] for lang in set(EXT_TO_LANGUAGE.values())}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for f in files:
            ext = os.path.splitext(f)[1]
            language = EXT_TO_LANGUAGE.get(ext)
            if language:
                matches[language].append(os.path.join(root, f))
    return matches


def _load_documents_by_language_from_disk(repo_path: str) -> dict[str, list[Document]]:
    files_by_language = _find_source_files(repo_path)
    docs_by_language: dict[str, list[Document]] = {}
    for language, files in files_by_language.items():
        print(f"Found {len(files)} {language} files to index.")
        docs_by_language[language] = SimpleDirectoryReader(input_files=files).load_data() if files else []
    return docs_by_language


def _load_documents_by_language_from_github(repo_name: str, github_client) -> dict[str, list[Document]]:
    """Fetch source files directly via the GitHub API instead of local disk.
    This is what makes /reindex work in production (Railway has no local
    clone of the sandbox repo, but it does have GitHub API access)."""
    repo = github_client.get_repo(repo_name)
    tree = repo.get_git_tree(sha=repo.default_branch, recursive=True)

    docs_by_language: dict[str, list[Document]] = {lang: [] for lang in set(EXT_TO_LANGUAGE.values())}
    for entry in tree.tree:
        if entry.type != "blob":
            continue
        if any(part in EXCLUDED_DIRS for part in entry.path.split("/")):
            continue
        language = EXT_TO_LANGUAGE.get(os.path.splitext(entry.path)[1])
        if not language:
            continue

        blob = repo.get_git_blob(entry.sha)
        if blob.encoding != "base64":
            continue  # skip binary/non-text blobs
        content = base64.b64decode(blob.content).decode("utf-8", errors="ignore")
        docs_by_language[language].append(Document(text=content, metadata={"file_name": entry.path}))

    for language, docs in docs_by_language.items():
        print(f"Found {len(docs)} {language} files to index (from GitHub).")
    return docs_by_language


def _build_and_persist_index(docs_by_language: dict[str, list[Document]]):
    embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)

    # Each language needs its own CodeSplitter (tree-sitter grammar differs per language),
    # so we split per-language and combine the resulting nodes before building the index.
    all_nodes = []
    total_files = 0
    for language, documents in docs_by_language.items():
        if not documents:
            continue
        splitter = CodeSplitter(
            language=language,
            chunk_lines=40,
            chunk_lines_overlap=5,
            max_chars=1500,
        )
        all_nodes.extend(splitter.get_nodes_from_documents(documents))
        total_files += len(documents)

    # Set up Pinecone (cloud-hosted, persists regardless of where indexing runs)
    pinecone_index = _get_pinecone_index()

    # Wipe existing vectors before every rebuild. Documents don't have stable IDs,
    # so re-indexing without this just piles new vectors on top of old ones forever
    # (this is always a full repo re-index, never incremental, so a clean slate is correct).
    try:
        pinecone_index.delete(delete_all=True)
    except Exception as e:
        print(f"  Skipping clear (index likely already empty): {e}")

    vector_store = PineconeVectorStore(pinecone_index=pinecone_index)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # Build and persist the index from the combined, pre-split nodes
    index = VectorStoreIndex(
        nodes=all_nodes,
        storage_context=storage_context,
        embed_model=embed_model,
    )
    print(f"Indexed {total_files} files ({len(all_nodes)} chunks) across {len(EXT_TO_LANGUAGE)} languages.")
    return index


def build_index(repo_path: str):
    """Index from a local clone (for manual/local runs)."""
    docs_by_language = _load_documents_by_language_from_disk(repo_path)
    return _build_and_persist_index(docs_by_language)


def build_index_from_github(repo_name: str, github_client):
    """Index directly via the GitHub API (for the /reindex endpoint, works
    identically locally and on Railway since no local clone is needed)."""
    docs_by_language = _load_documents_by_language_from_github(repo_name, github_client)
    return _build_and_persist_index(docs_by_language)


if __name__ == "__main__":
    build_index("C:\\Users\\adrie\\Documents\\AI-Code-Review-Sandbox")
