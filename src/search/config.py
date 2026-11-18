"""Search configuration with candidate depth derived from RRF settings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchConfig:
    rrf_k: int = 10
    candidate_multiplier: int = 5
    result_limit: int = 10
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768

    def __post_init__(self) -> None:
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        if self.candidate_multiplier < 1:
            raise ValueError("candidate_multiplier must be positive")
        if self.result_limit < 1:
            raise ValueError("result_limit must be positive")
        if self.embedding_dimensions < 1:
            raise ValueError("embedding_dimensions must be positive")
        if not self.embedding_model.strip():
            raise ValueError("embedding_model cannot be empty")

    @property
    def candidate_depth(self) -> int:
        return self.rrf_k * self.candidate_multiplier
