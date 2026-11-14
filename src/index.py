import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from report import Report
from store import hash_file, valid_hash

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS blob (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    added_at REAL NOT NULL,
    verified_at REAL
);
CREATE TABLE IF NOT EXISTS manifest (
    name TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    source_url TEXT,
    etag TEXT,
    updated_at REAL NOT NULL,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS entry (
    scope TEXT NOT NULL REFERENCES manifest(name) ON DELETE CASCADE,
    name TEXT NOT NULL,
    hash TEXT NOT NULL REFERENCES blob(hash),
    PRIMARY KEY (scope, name)
);
CREATE INDEX IF NOT EXISTS entry_hash ON entry(hash);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class ManifestInfo:
    name: str
    content_hash: str
    size: int
    source_url: str | None
    etag: str | None
    updated_at: float
    checked_at: float | None


class Index:
    # 连接只在创建它的线程中使用，下载线程不访问数据库
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "Index.db"
        self.connection = None
        self._connect()

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        # Index.db 可重建，不需要每次提交都 fsync
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            self.connection.commit()
        elif row[0] != SCHEMA_VERSION:
            self.close()
            self._delete_files()
            self._connect()

    def _delete_files(self):
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def blob(self, hash):
        return self.connection.execute(
            "SELECT * FROM blob WHERE hash = ?", (hash,)
        ).fetchone()

    def blobs(self):
        return self.connection.execute(
            "SELECT * FROM blob ORDER BY verified_at IS NOT NULL, verified_at, hash"
        ).fetchall()

    def register_blobs(self, rows):
        # rows: (hash, size, added_at, verified_at)
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO blob(hash, size, added_at, verified_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    size = excluded.size,
                    verified_at = COALESCE(excluded.verified_at, blob.verified_at)
                """,
                rows,
            )

    def remove_blobs(self, hashes):
        with self.connection:
            self.connection.executemany(
                "DELETE FROM blob WHERE hash = ?", [(hash,) for hash in hashes]
            )

    def adopt(self, bindings, store, report=None):
        # 登记绑定所需的 blob：文件在盘上就按 mtime 登记；缺失时按声明大小占位，
        # 由 Check 报告 missing。传入 report 时同时记录缺失与大小不符
        rows = []
        seen = set()
        for binding in bindings:
            if binding.hash in seen:
                continue
            seen.add(binding.hash)
            row = self.blob(binding.hash)
            if row is None:
                path = store.path_of(binding.hash)
                exists = path.is_file()
                rows.append((
                    binding.hash, binding.size,
                    path.stat().st_mtime if exists else time.time(), None,
                ))
                if report is not None and not exists:
                    report.missing.append(binding.hash)
            elif report is not None and row["size"] != binding.size:
                rows.append((binding.hash, binding.size, time.time(), None))
                report.corrupt.append(binding.hash)
        if rows:
            self.register_blobs(rows)

    def manifest(self, name):
        row = self.connection.execute(
            "SELECT * FROM manifest WHERE name = ?", (name,)
        ).fetchone()
        return ManifestInfo(**dict(row)) if row else None

    def manifests(self):
        rows = self.connection.execute("SELECT * FROM manifest ORDER BY name").fetchall()
        return [ManifestInfo(**dict(row)) for row in rows]

    def record_manifest(self, name, content_hash, size, bindings, source_url=None,
                        etag=None, updated_at=None):
        updated_at = time.time() if updated_at is None else updated_at
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO manifest(
                    name, content_hash, size, source_url, etag, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    size = excluded.size,
                    source_url = COALESCE(excluded.source_url, manifest.source_url),
                    etag = COALESCE(excluded.etag, manifest.etag),
                    updated_at = excluded.updated_at
                """,
                (name, content_hash, size, source_url, etag, updated_at),
            )
            self.connection.execute("DELETE FROM entry WHERE scope = ?", (name,))
            self.connection.executemany(
                "INSERT INTO entry(scope, name, hash) VALUES (?, ?, ?)",
                [(name, binding.name, binding.hash) for binding in bindings],
            )

    def delete_manifest(self, name):
        with self.connection:
            self.connection.execute("DELETE FROM manifest WHERE name = ?", (name,))

    def binding(self, scope, name):
        row = self.connection.execute(
            "SELECT hash FROM entry WHERE scope = ? AND name = ?", (scope, name)
        ).fetchone()
        return row[0] if row else None

    def entries(self, scope):
        return self.connection.execute(
            "SELECT name, hash FROM entry WHERE scope = ? ORDER BY name", (scope,)
        ).fetchall()

    def scopes(self, hashes):
        hashes = list(hashes)
        found = set()
        for start in range(0, len(hashes), 500):
            chunk = hashes[start:start + 500]
            marks = ",".join("?" * len(chunk))
            found.update(row[0] for row in self.connection.execute(
                f"SELECT DISTINCT scope FROM entry WHERE hash IN ({marks})", chunk
            ))
        return sorted(found)

    def unreferenced(self, cutoff=None):
        sql = """
            SELECT hash, added_at FROM blob b
            WHERE NOT EXISTS (SELECT 1 FROM entry e WHERE e.hash = b.hash)
        """
        params = ()
        if cutoff is not None:
            sql += " AND added_at < ?"
            params = (cutoff,)
        return self.connection.execute(sql + " ORDER BY hash", params).fetchall()

    def mark_verified(self, hashes):
        if hashes:
            now = time.time()
            with self.connection:
                self.connection.executemany(
                    "UPDATE blob SET verified_at = ? WHERE hash = ?",
                    [(now, hash) for hash in hashes],
                )

    def reconcile(self, manifest_dir, store, extract):
        # 修复方向只有文件 → 数据库
        disk = {unquote(path.name): path for path in Path(manifest_dir).iterdir()
                if path.is_file()}
        for info in self.manifests():
            if info.name not in disk:
                self.delete_manifest(info.name)
        for name, path in disk.items():
            content = path.read_bytes()
            content_hash = hashlib.sha1(content).hexdigest()
            info = self.manifest(name)
            if info and info.content_hash == content_hash and info.size == len(content):
                continue
            bindings = extract(name, content).bindings
            self.adopt(bindings, store)
            self.record_manifest(name, content_hash, len(content), bindings,
                                 updated_at=path.stat().st_mtime)

    def rebuild(self, manifest_dir, store, extract, deep=False):
        report = Report()
        self.close()
        self._delete_files()
        self._connect()

        rows = []
        for path in sorted(store.files.glob("*/*")):
            if not path.is_file():
                continue
            if not valid_hash(path.name):
                report.corrupt.append(str(path))
                continue
            good = not deep or hash_file(path) == path.name
            if not good:
                report.corrupt.append(path.name)
            stat = path.stat()
            rows.append((path.name, stat.st_size, stat.st_mtime,
                         time.time() if deep and good else None))
        self.register_blobs(rows)

        for path in sorted(Path(manifest_dir).iterdir()):
            if not path.is_file():
                continue
            content = path.read_bytes()
            bindings = extract(unquote(path.name), content).bindings
            self.adopt(bindings, store, report)
            self.record_manifest(
                unquote(path.name), hashlib.sha1(content).hexdigest(), len(content),
                bindings, updated_at=path.stat().st_mtime,
            )
        return report
