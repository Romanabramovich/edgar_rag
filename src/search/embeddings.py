"""Small Ollama embedding client using only the Python standard library."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, model: str = "nomic-embed-text", *,
                 base_url: str = "http://127.0.0.1:11434", timeout: float = 120,
                 opener=urlopen) -> None:
        if not model.strip():
            raise ValueError("Embedding model cannot be empty")
        self.model = model
        self.url = base_url.rstrip("/") + "/api/embed"
        self.timeout = timeout
        self.opener = opener

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        request = Request(
            self.url,
            data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"Ollama embedding request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise EmbeddingError("Ollama returned an invalid response")
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("Ollama returned an unexpected embedding count")
        try:
            return [[float(value) for value in vector] for vector in vectors]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("Ollama returned an invalid embedding vector") from exc
