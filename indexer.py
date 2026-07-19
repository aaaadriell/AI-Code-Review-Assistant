from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core import StorageContext
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.node_parser import CodeSplitter
import chromadb


def build_index(repo_path: str):
    # Load all Python files from the repo
    documents = SimpleDirectoryReader(
        repo_path,
        required_exts=[".py"],
        recursive=True,
        exclude=["venv", ".venv", "__pycache__", ".git", "node_modules", ".pytest_cache"]
    ).load_data()

    # Use HuggingFace embeddings (free, no API key needed)
    embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")

    # Using Tree-sitter to chunk by AST (functions & classes) instead of LlamaIndex's default chunking by character count
    splitter = CodeSplitter(
        language="python",
        chunk_lines=40,
        chunk_lines_overlap=5,
        max_chars=1500,
    )

    # Set up ChromaDB
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    chroma_collection = chroma_client.get_or_create_collection("codebase")
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # Build and persist the index
    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        embed_model=embed_model,
        transformations=[splitter]
    )
    print(f"Indexed {len(documents)} files.")
    return index

if __name__ == "__main__":
    build_index("C:\\Users\\adrie\\Documents\\AI-Code-Review-Sandbox")
