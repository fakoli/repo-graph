"""Incremental SQLite keyword index and optional CPU semantic search."""
from __future__ import annotations

from contextlib import closing
import hashlib
import heapq
import json
from pathlib import Path
import re
import sqlite3
import time

MODEL = "BAAI/bge-small-en-v1.5"
READ_LIMIT = 64 * 1024
TEXT_EXTENSIONS = {".md", ".rst", ".txt", ".go", ".py", ".js", ".jsx", ".ts", ".tsx",
                   ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".sh", ".tf", ".sql", ".vue", ".svelte"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})")


def words(text: str) -> str:
    return re.sub(r"[^\w]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)).replace("_", " ")


def synopsis(path: str, source: str) -> str:
    """Index names and declarations/comments, not every implementation token."""
    if "PRIVATE KEY-----" in source:
        return ""
    title = words(path)
    lines = []
    for number, line in enumerate(source.splitlines(), 1):
        line = line.strip()
        if not line or re.search(r"copyright|spdx|licensed under|permission is hereby", line, re.I):
            continue
        if (Path(path).suffix in {".md", ".rst", ".txt"} or
            re.match(r"(?://|#|/\*|\*|\"\"\"|'''|func |def |class |pub |fn |export |interface |type |resource |data )", line)):
            # Evidence retains original line references; identifier terms aid exact retrieval.
            lines.append(f"L{number}: {SECRET.sub('[redacted]', line[:180])}")
        if sum(map(len, lines)) >= 1400:
            break
    return title + "\n" + "\n".join(lines)


def connect(output: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = output / "search.db"
    db = sqlite3.connect(path.as_uri() + "?mode=ro" if readonly else str(path), uri=readonly, timeout=30)
    db.row_factory = sqlite3.Row
    if not readonly:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
          stamp TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, terms TEXT NOT NULL, vector BLOB);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(terms, content='docs', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
          INSERT INTO fts(rowid,terms) VALUES(new.id,new.terms); END;
        CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
          INSERT INTO fts(fts,rowid,terms) VALUES('delete',old.id,old.terms); END;
        CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF terms ON docs BEGIN
          INSERT INTO fts(fts,rowid,terms) VALUES('delete',old.id,old.terms);
          INSERT INTO fts(rowid,terms) VALUES(new.id,new.terms); END;
        """)
    return db


def catalog(root: Path, files: list[str], output: Path) -> dict:
    started = time.monotonic()
    scanned = reused = truncated = 0
    with closing(connect(output)) as db, db:
        db.execute("CREATE TEMP TABLE seen(path TEXT PRIMARY KEY)")
        for path in files:
            if Path(path).suffix.lower() not in TEXT_EXTENSIONS:
                continue
            resolved = (root / path).resolve()
            if root not in resolved.parents or not resolved.is_file():
                continue
            stat = resolved.stat()
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
            db.execute("INSERT INTO seen VALUES(?)", (path,))
            old = db.execute("SELECT stamp,digest FROM docs WHERE path=?", (path,)).fetchone()
            truncated += stat.st_size > READ_LIMIT
            if old and old['stamp'] == stamp:
                reused += 1
                continue
            with resolved.open('rb') as stream:
                source = stream.read(READ_LIMIT).decode('utf-8', errors='replace')
            body = synopsis(path, source)
            if not body:
                db.execute("DELETE FROM docs WHERE path=?", (path,))
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()
            if old and old['digest'] == digest:
                db.execute("UPDATE docs SET stamp=? WHERE path=?", (stamp, path))
                reused += 1
            else:
                db.execute("""INSERT INTO docs(path,stamp,digest,body,terms) VALUES(?,?,?,?,?)
                  ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp,digest=excluded.digest,
                    body=excluded.body,terms=excluded.terms,vector=NULL""",
                           (path, stamp, digest, body, words(body)))
                scanned += 1
        deleted = db.execute("DELETE FROM docs WHERE path NOT IN (SELECT path FROM seen)").rowcount
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema','1')")
        count = db.execute("SELECT count(*) FROM docs").fetchone()[0]
    return dict(documents=count, scanned=scanned, reused=reused, deleted=deleted, truncated=truncated,
                seconds=round(time.monotonic() - started, 3))


class Embeddings:
    def __init__(self, model: str = MODEL, *, offline: bool = False):
        try:
            from fastembed import TextEmbedding
            import numpy as np
        except ImportError as error:
            raise RuntimeError("Semantic search needs the semantic extra: uv sync --extra semantic, or install repo-graph-agent[semantic].") from error
        self.np = np
        self.name = model
        self.model = TextEmbedding(model_name=model, threads=4, providers=['CPUExecutionProvider'],
                                   cache_dir=str(Path.home() / '.cache/repo-graph/models'), local_files_only=offline)

    def passages(self, texts):
        return self.model.passage_embed(texts, batch_size=32)

    def query(self, text):
        return next(self.model.query_embed(text))

    def packed(self, vector):
        vector = self.np.asarray(vector, dtype='<f4')
        norm = self.np.linalg.norm(vector)
        if vector.ndim != 1 or not self.np.isfinite(vector).all() or norm <= 0:
            raise ValueError('Embedding returned an invalid vector')
        return (vector / norm).astype('<f4').tobytes()


def embed_index(output: Path, embedder: Embeddings) -> dict:
    started = time.monotonic()
    with closing(connect(output)) as db:
        old = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        if old and old[0] != embedder.name:
            raise ValueError(f"Index uses {old[0]}; rebuild in a different output directory to change model.")
        db.execute("INSERT OR REPLACE INTO meta VALUES('model',?)", (embedder.name,)); db.commit()
        reused = db.execute("SELECT count(*) FROM docs WHERE vector IS NOT NULL").fetchone()[0]
        embedded = 0
        while rows := db.execute("SELECT id,body FROM docs WHERE vector IS NULL ORDER BY id LIMIT 256").fetchall():
            vectors = list(embedder.passages([row['body'] for row in rows]))
            if len(vectors) != len(rows):
                raise ValueError('Embedding batch has missing results')
            with db:
                for row, vector in zip(rows, vectors):
                    db.execute("UPDATE docs SET vector=? WHERE id=?", (embedder.packed(vector), row['id']))
            embedded += len(rows)
        return dict(embedded=embedded, reused=reused, model=embedder.name, seconds=round(time.monotonic() - started, 3))


class Search:
    def __init__(self, output: Path, embedder=None):
        self.output, self.embedder = output, embedder

    def run(self, query: str, *, mode='hybrid', limit=10, prefix='') -> dict:
        if mode not in {'keyword', 'semantic', 'hybrid'}:
            raise ValueError('Unknown search mode')
        if not query.strip() or len(query) > 1000 or not 1 <= limit <= 50:
            raise ValueError('Query must have 1–1000 characters; limit must be 1–50')
        started = time.monotonic()
        ranks, cosine = [], {}
        with closing(connect(self.output, readonly=True)) as db:
            prefix = prefix.strip('/')
            where = "(?='' OR path=? OR substr(path,1,length(?)+1)=?||'/')"
            params = (prefix, prefix, prefix, prefix)
            count = db.execute(f"SELECT count(*) FROM docs WHERE {where}", params).fetchone()[0]
            candidate_count = max(50, limit * 5)
            if mode != 'semantic':
                terms = re.findall(r'\w+', words(query))[:32]
                expression = ' OR '.join('"' + term + '"' for term in terms)
                hits = db.execute(f"""SELECT docs.id FROM fts JOIN docs ON docs.id=fts.rowid
                    WHERE fts MATCH ? AND {where} ORDER BY bm25(fts),docs.path LIMIT ?""",
                                  (expression or '""', *params, candidate_count)).fetchall()
                ranks.append([row['id'] for row in hits])
            if mode != 'keyword':
                if self.embedder is None:
                    raise RuntimeError('Build a semantic index with repo-graph index OUTPUT --semantic first, or select keyword mode.')
                model = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
                ready = db.execute(f"SELECT count(*) FROM docs WHERE vector IS NOT NULL AND {where}", params).fetchone()[0]
                if not model or model[0] != self.embedder.name or ready != count:
                    raise RuntimeError('Semantic index is missing, stale, or uses a different model. Run repo-graph index OUTPUT --semantic.')
                np = self.embedder.np
                q = np.frombuffer(self.embedder.packed(self.embedder.query(query)), dtype='<f4')
                best = []
                cursor = db.execute(f"SELECT id,vector FROM docs WHERE {where} ORDER BY id", params)
                # ponytail: exact search uses bounded 512-vector blocks; add an ANN shard when measured latency exceeds the budget.
                while rows := cursor.fetchmany(512):
                    matrix = np.frombuffer(b''.join(row['vector'] for row in rows), dtype='<f4').reshape(len(rows), -1)
                    scores = matrix @ q
                    for row, score in zip(rows, scores):
                        item = (float(score), row['id'])
                        if len(best) < candidate_count: heapq.heappush(best, item)
                        elif item > best[0]: heapq.heapreplace(best, item)
                best.sort(reverse=True)
                ranks.append([id for score, id in best]); cosine = {id:score for score,id in best}
            fused = {}
            for rank in ranks:
                for pos, id in enumerate(rank, 1):
                    fused[id] = fused.get(id, 0) + 1 / (60 + pos)
            selected = sorted(fused, key=lambda id: (-fused[id], id))[:limit]
            results = []
            for id in selected:
                row = db.execute('SELECT path,body FROM docs WHERE id=?', (id,)).fetchone()
                results.append(dict(path=row['path'], evidence=row['body'][:1400], score=round(fused[id], 6),
                                    similarity=round(cosine[id], 4) if id in cosine else None))
        return dict(query=query, mode=mode, documents=count, results=results,
                    seconds=round(time.monotonic() - started, 4))
