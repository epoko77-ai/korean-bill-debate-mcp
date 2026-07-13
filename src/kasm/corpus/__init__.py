"""Durable, revisioned full-text corpus for official Assembly documents."""

from .lexical import (
    CorpusSearchCandidate,
    CorpusSearchPage,
    LexicalMatchMode,
    lexical_term_frequencies,
    lexical_terms,
)
from .models import (
    CORPUS_SCHEMA_VERSION,
    LEXICAL_INDEX_VERSION,
    CorpusDocument,
    CorpusDocumentIdentity,
    CorpusDocumentRef,
    CorpusEvidenceKind,
    CorpusIngestionFailure,
    CorpusLexicalIndexManifest,
    CorpusRevisionManifest,
    CorpusScopeCoverage,
    LexicalShardRef,
)
from .repository import (
    CorpusRepository,
    CorpusRepositoryIntegrityError,
    CorpusRevisionBuilder,
    FullTextCorpusReader,
    IncompleteCorpusRevisionError,
)
from .storage import (
    CorpusBlobClient,
    CorpusObjectConflictError,
    CorpusObjectIntegrityError,
    CorpusObjectStore,
    CorpusStorageError,
    FilesystemCorpusObjectStore,
    VercelBlobCorpusObjectStore,
)

__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "LEXICAL_INDEX_VERSION",
    "CorpusDocument",
    "CorpusDocumentIdentity",
    "CorpusDocumentRef",
    "CorpusEvidenceKind",
    "CorpusIngestionFailure",
    "CorpusBlobClient",
    "CorpusLexicalIndexManifest",
    "CorpusObjectConflictError",
    "CorpusObjectIntegrityError",
    "CorpusObjectStore",
    "CorpusRepository",
    "CorpusRepositoryIntegrityError",
    "CorpusRevisionBuilder",
    "CorpusRevisionManifest",
    "CorpusScopeCoverage",
    "CorpusSearchCandidate",
    "CorpusSearchPage",
    "CorpusStorageError",
    "FilesystemCorpusObjectStore",
    "FullTextCorpusReader",
    "IncompleteCorpusRevisionError",
    "LexicalMatchMode",
    "LexicalShardRef",
    "VercelBlobCorpusObjectStore",
    "lexical_term_frequencies",
    "lexical_terms",
]
