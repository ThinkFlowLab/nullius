"""Point the pinned upstream's Qwen client at the run's search relay."""
import os


def route_qwen(retrieval):
    if retrieval not in ("qwen_embeddings", "qwen_embeddings_grep", "qwen_embeddings_reranker",
                         "qwen_embeddings_reranker_grep", "alltools-qwen"):
        return
    base, key = os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY")
    if not base or not key:
        raise ValueError("Qwen search needs the run's relay URL and placeholder key")
    from openai import OpenAI
    from tau2.knowledge.embedders import openrouter_embedder

    def client(**kwargs):
        return OpenAI(**{**kwargs, "base_url": base, "api_key": key})

    # Only this upstream module's constructor: its embedder, instructions and cache stay upstream's.
    openrouter_embedder.OpenAI = client
