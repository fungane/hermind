"""hermind store: SQLite notes + own unigram/bigram postings table, an entity graph, deterministic
consolidation (near-duplicate merge, newer-state supersede) and multi-signal retrieval fused with reciprocal rank
fusion (BM25, keyword/alias field, query entities, one-hop entity expansion, temporal, recency, kind/proof prior).
Pure stdlib + SQLite, no Hermes imports, so tools/ and tests/ can use it directly.

Layout (this file is only the facade; behaviour lives in the mixins):
    store_common.py       constants and pure helpers (no I/O)
    store_schema.py       schema creation, migration, re-indexing
    store_aux.py          usage log, entity summaries, vector storage, access tracking
    store_entities.py     entity dictionary and graph
    store_writes.py       add/update/forget, generated + learned keywords, dedup and write checks
    store_consolidate.py  maintenance, merge, supersede, restore, history
    store_reads.py        get/list/count/stats, related
    store_review.py       review queue (pending / confirm / reject)
    store_core.py         deterministic core summary of pinned notes + trusted preferences
    store_feedback.py     shown/opened counts behind the open-rate ranking prior
    store_search.py       retrieval, fusion, reflect
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

try:
    from . import dbdriver, store_common as _common
    from .store_aux import AuxMixin
    from .store_common import *  # noqa: F401,F403
    from .store_common import Synonyms, _clean_scope, _now, load_synonyms, os, DEFAULT_BOOST, DEFAULT_WEIGHTS
    from .store_consolidate import ConsolidateMixin
    from .store_entities import EntityMixin
    from .store_reads import ReadMixin
    from .store_schema import SchemaMixin
    from .store_core import CoreMixin
    from .store_feedback import FeedbackMixin
    from .store_review import ReviewMixin
    from .store_search import SearchMixin
    from .store_writes import WriteMixin
except ImportError:  # loaded as a top-level module (tools/, import_hindsight.py)
    import dbdriver
    import store_common as _common
    from store_aux import AuxMixin
    from store_common import *  # noqa: F401,F403
    from store_common import Synonyms, _clean_scope, _now, load_synonyms, os, DEFAULT_BOOST, DEFAULT_WEIGHTS
    from store_consolidate import ConsolidateMixin
    from store_entities import EntityMixin
    from store_reads import ReadMixin
    from store_schema import SchemaMixin
    from store_core import CoreMixin
    from store_feedback import FeedbackMixin
    from store_review import ReviewMixin
    from store_search import SearchMixin
    from store_writes import WriteMixin

# Underscore-prefixed helpers are not exported by ``import *``; tools and tests reach them as ``store._name``.
globals().update({k: v for k, v in vars(_common).items() if k.startswith("_") and not k.startswith("__")})

__all__ = ["DecisionStore", "RefusedWrite", "KINDS", "tokenize", "token_weight", "normalize", "jaccard", "load_synonyms",
           "Synonyms", "extract_entities", "DEFAULT_WEIGHTS"]


class DecisionStore(SchemaMixin, AuxMixin, EntityMixin, WriteMixin, ConsolidateMixin, ReadMixin, ReviewMixin,
                    CoreMixin, FeedbackMixin, SearchMixin):

    def __init__(self, db_path: str, synonyms_path: str | None = None, weights: dict | None = None,
                 boost: dict | None = None, scope: str = "default", track_access: bool = False,
                 key: str | None = None):
        self.db_path = str(db_path)
        self._key = key                       # kept for the backup made before a schema upgrade
        if self.db_path != ":memory:":
            folder = Path(self.db_path).expanduser().parent
            fresh = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if fresh:                       # only a folder this call created; never tighten one the user already had
                try:
                    os.chmod(folder, 0o700)
                except OSError:
                    pass
        self._lock = threading.RLock()
        # plain sqlite3 unless a key is set (key= or HERMIND_DB_KEY_FILE / HERMIND_DB_KEY): then SQLCipher, see dbdriver.py
        self.conn = dbdriver.connect(self.db_path, key, check_same_thread=False)
        self.conn.row_factory = dbdriver.row_factory(self.conn)
        self.encrypted = type(self.conn).__module__.split(".")[0] != "sqlite3"
        if self.db_path != ":memory:":
            self._enable_wal()
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-4000")  # ~4 MB page cache
        self.synonyms = Synonyms(load_synonyms(synonyms_path))
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.boost = {**DEFAULT_BOOST, **(boost or {})}
        self.scope = _clean_scope(scope)
        # access tracking (OpenViking lifecycle): returned ids are buffered and written in one batch at most every
        # ACCESS_FLUSH_SECONDS (and on close), so searching does not cause a write per query.
        self.track_access = track_access
        self._access_buf: Counter = Counter()
        self._access_flushed = _now()
        self._ent_cache: dict | None = None
        self._init_schema()
        self._ensure_tokenizer_version()
        self._private_files()

    def _enable_wal(self, attempts: int = 100) -> None:
        """journal_mode=WAL on a file other processes are creating or opening at the same moment fails at once with
        'database is locked' (the busy timeout does not apply to it), so retry for a few seconds."""
        for n in range(attempts):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except dbdriver.operational_errors() as exc:
                if n == attempts - 1 or not ("locked" in str(exc) or "busy" in str(exc)):
                    raise
                time.sleep(0.05)

    def _private_files(self) -> None:
        """Owner-only permissions on the database and its WAL/SHM files (notes are personal). Best effort."""
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.db_path + suffix, 0o600)
            except OSError:
                pass
