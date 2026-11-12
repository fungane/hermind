"""DecisionStore mixin: schema creation, migration, re-indexing."""

from __future__ import annotations

from pathlib import Path

try:
    from . import dbdriver
    from .db_backup import BackupError, backup_database
    from .store_common import (
        FIELD_GEN, FIELD_LEARNED, SCHEMA_VERSION, TOKENIZER_VERSION, TRUST_DEFAULT, core_text,
        parse_dates, tokenize
    )
except ImportError:  # loaded as a top-level module (tools/)
    import dbdriver
    from db_backup import BackupError, backup_database
    from store_common import (
        FIELD_GEN, FIELD_LEARNED, SCHEMA_VERSION, TOKENIZER_VERSION, TRUST_DEFAULT, core_text,
        parse_dates, tokenize
    )


class SchemaMixin:
    # -- schema / migration ------------------------------------------------------------------------------------

    KEEP_UPGRADE_BACKUPS = 3
    _key: str | None = None             # set by DecisionStore.__init__; the backup before an upgrade needs it

    def _backup_before_upgrade(self) -> None:
        """An existing database written by an older schema is copied to `<db>.bak-v<N>-<time>-<n>` (owner-only) before
        anything is altered, with SQLite's online backup (db_backup.py): the copy holds every committed note, also while
        another connection is in the middle of a read. If the copy cannot be made the upgrade is refused: a failed
        backup must not lead to a risky upgrade. The newest KEEP_UPGRADE_BACKUPS copies are kept."""
        if self.db_path == ":memory:":
            return
        old = self.conn.execute("PRAGMA user_version").fetchone()[0]
        has_notes = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone()
        if not has_notes or not (0 < old < SCHEMA_VERSION):
            return
        src = Path(self.db_path).expanduser()
        try:
            made = backup_database(src, prefix=f"bak-v{old}", keep=self.KEEP_UPGRADE_BACKUPS, key=self._key,
                                   prune_glob=src.name + ".bak-v*")
        except (BackupError, OSError) as e:
            raise RuntimeError(f"refusing to upgrade {src.name} (schema {old} -> {SCHEMA_VERSION}): could not back it up first ({e})") from e
        if made is None:                    # no readable file at that path: still no backup, so still no upgrade
            raise RuntimeError(f"refusing to upgrade {src.name} (schema {old} -> {SCHEMA_VERSION}): could not back it up first (no readable database file at that path)")

    def _init_schema(self) -> None:
        self._backup_before_upgrade()
        with self._lock, self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'chat',
                keywords TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL,
                source_session TEXT NOT NULL DEFAULT '',
                pinned INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                doc_len_content INTEGER NOT NULL DEFAULT 0,
                doc_len_keywords INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS notes_kind ON notes(kind, created_at);
            CREATE INDEX IF NOT EXISTS notes_hash ON notes(content_hash);
            CREATE TABLE IF NOT EXISTS postings (
                term TEXT NOT NULL,
                note_id INTEGER NOT NULL,
                field INTEGER NOT NULL,
                tf INTEGER NOT NULL,
                PRIMARY KEY (term, note_id, field)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS postings_note ON postings(note_id);
            """)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(notes)")}
            for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'active'"), ("canonical_id", "INTEGER"),
                             ("proof_count", "INTEGER NOT NULL DEFAULT 1"), ("event_at", "REAL"),
                             ("consolidated_at", "REAL"), ("locked", "INTEGER NOT NULL DEFAULT 0"),
                             ("trust", f"REAL NOT NULL DEFAULT {TRUST_DEFAULT}"), ("valid_to", "REAL"),
                             ("candidate", "INTEGER NOT NULL DEFAULT 0"), ("scope", "TEXT NOT NULL DEFAULT 'default'"),
                             ("tags", "TEXT NOT NULL DEFAULT ''"), ("access_count", "INTEGER NOT NULL DEFAULT 0"),
                             ("last_access", "REAL"), ("gen_keywords", "TEXT NOT NULL DEFAULT ''"),
                             ("gen_at", "REAL"), ("gen_model", "TEXT NOT NULL DEFAULT ''"),
                             ("learned_terms", "TEXT NOT NULL DEFAULT ''"),
                             ("review", "TEXT NOT NULL DEFAULT 'active'"), ("valid_from", "REAL"),
                             ("shown_count", "REAL NOT NULL DEFAULT 0"), ("opened_count", "REAL NOT NULL DEFAULT 0"),
                             ("fb_at", "REAL")):
                if col not in cols:
                    try:
                        self.conn.execute(f"ALTER TABLE notes ADD COLUMN {col} {ddl}")
                    except dbdriver.operational_errors() as exc:   # another process added it between our check and now
                        if "duplicate column name" not in str(exc):
                            raise
            self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS notes_status ON notes(status);
            CREATE INDEX IF NOT EXISTS notes_review ON notes(review) WHERE review != 'active';
            CREATE INDEX IF NOT EXISTS notes_scope ON notes(scope);
            CREATE INDEX IF NOT EXISTS notes_canonical ON notes(canonical_id);
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                df INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS note_entities (
                note_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL,
                PRIMARY KEY (note_id, entity_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS note_entities_ent ON note_entities(entity_id, note_id);
            CREATE TABLE IF NOT EXISTS entity_links (
                a INTEGER NOT NULL,
                b INTEGER NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (a, b)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS note_links (
                src INTEGER NOT NULL,
                dst INTEGER NOT NULL,
                rel TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (src, dst, rel)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS note_links_dst ON note_links(dst);
            CREATE INDEX IF NOT EXISTS entity_links_b ON entity_links(b);
            -- narrow covering index for the BM25 join (the notes table rows carry the full text)
            CREATE INDEX IF NOT EXISTS notes_bm25 ON notes(id, doc_len_content, doc_len_keywords, expires_at, pinned,
                                                           scope, event_at, created_at);
            CREATE INDEX IF NOT EXISTS notes_gen_at ON notes(gen_at);
            CREATE TABLE IF NOT EXISTS learn_pending (
                note_id INTEGER NOT NULL,
                term TEXT NOT NULL,
                source TEXT NOT NULL,
                query_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (note_id, term, source)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vectors (note_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS tombstones (
                digest TEXT PRIMARY KEY,
                note_id INTEGER,
                created_at REAL NOT NULL
            ) WITHOUT ROWID;
            """)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            self._migrate(version)

    def _migrate(self, version: int) -> None:
        """v0.1 -> v0.2: re-tokenise (normalisation/stemming changed), fill event dates, build the entity graph.
        v0.2 -> v0.3: only new columns/tables (trust, valid_to, tombstones), added in _init_schema.
        v0.3 -> v0.4: only new columns/tables (gen_keywords, learned_terms, learn_pending, meta) and indexes.
        v4 -> v5: only new nullable/defaulted columns (review, valid_from, shown_count, opened_count, fb_at)."""
        if version >= 2:
            with self._lock, self.conn:
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        with self._lock, self.conn:
            rows = self.conn.execute("SELECT id, content, keywords FROM notes").fetchall()
            self.conn.execute("DELETE FROM postings")
            for r in rows:
                ctoks, ktoks = tokenize(r["content"]), tokenize(r["keywords"])
                ev = parse_dates(core_text(r["content"])) or parse_dates(r["content"])
                self.conn.execute("UPDATE notes SET doc_len_content=?, doc_len_keywords=?, event_at=? WHERE id=?",
                                  (len(ctoks), len(ktoks), ev[0] if ev else None, r["id"]))
                self._index(r["id"], ctoks, ktoks)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if rows:
            self.rebuild_graph()

    def reindex(self) -> int:
        """Rebuild every posting (content, keywords, generated aliases, learned terms) with the current tokenizer.
        Needed after textnorm.tokenize/stem changes; idempotent. Returns the number of notes re-indexed."""
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id, content, keywords, gen_keywords, learned_terms FROM notes").fetchall()
            self.conn.execute("DELETE FROM postings")
            for r in rows:
                ctoks, ktoks = tokenize(r["content"]), tokenize(r["keywords"])
                self.conn.execute("UPDATE notes SET doc_len_content=?, doc_len_keywords=? WHERE id=?",
                                  (len(ctoks), len(ktoks), r["id"]))
                self._index(r["id"], ctoks, ktoks)
                if r["gen_keywords"]:
                    self._index_field(r["id"], FIELD_GEN, r["gen_keywords"])
                learned = list(dict.fromkeys(t for w in (r["learned_terms"] or "").split() for t in tokenize(w)))
                if learned:
                    self.conn.executemany("INSERT OR REPLACE INTO postings(term, note_id, field, tf) VALUES (?,?,?,1)",
                                          [(t, r["id"], FIELD_LEARNED) for t in learned])
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('tokenizer_version', ?)",
                              (TOKENIZER_VERSION,))
        return len(rows)

    def _ensure_tokenizer_version(self) -> None:
        cur = self.get_meta("tokenizer_version")
        if cur == TOKENIZER_VERSION:
            return
        has_notes = self.conn.execute("SELECT 1 FROM notes LIMIT 1").fetchone()
        if has_notes:
            self.reindex()
        else:
            self.set_meta("tokenizer_version", TOKENIZER_VERSION)
