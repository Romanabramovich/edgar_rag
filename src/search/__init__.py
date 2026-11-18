"""Local hybrid search over indexed filing passages."""

from .config import SearchConfig
from .indexer import index_pending, vector_search
from .retrieval import (
    keyword_search, reciprocal_rank_fusion, retrieve, search,
    search_coverage, semantic_search,
)

__all__ = [
    "SearchConfig", "index_pending", "vector_search", "keyword_search",
    "semantic_search", "reciprocal_rank_fusion", "retrieve", "search",
    "search_coverage",
]
