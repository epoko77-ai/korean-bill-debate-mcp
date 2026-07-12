"""Replaceable local indexing providers."""

from .embeddings import EmbeddingProvider, HashEmbeddingProvider

__all__ = ["EmbeddingProvider", "HashEmbeddingProvider"]
