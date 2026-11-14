import errno
import os
import shutil
import time
from pathlib import Path

from manifest import Manifest
from report import Report
from store import BlobMissing, HashMismatch, hash_file, valid_hash, write_lock


class EntryNotFound(LookupError):
    pass


class Resource:
    def __init__(self, root, extractors=()):
        self.root = Path(root)
        self.manifest = Manifest(self.root, extractors)
        self.store = self.manifest.store
        self.index = self.manifest.index

    def Read(self, scope, name, verify=False):
        hash = self.index.binding(scope, name)
        if hash is None:
            raise EntryNotFound(f"{scope}:{name}")
        path = self.store.path_of(hash)
        if not path.is_file():
            raise BlobMissing(hash)
        if verify and not self.store.verify(hash):
            raise HashMismatch(hash)
        return path

    def Export(self, scope, name, dest, link=False):
        # 默认复制；link=True 用硬链接（同分区不占空间），但改动导出文件会污染 blob
        source = self.Read(scope, name)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise FileExistsError(dest)
        if link:
            try:
                os.link(source, dest)
                return dest
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
        shutil.copy2(source, dest)
        return dest

    def Check(self, deep=False, limit=None):
        # 只读检查，不持写锁；仅最后写入 verified_at 时加锁
        report = Report()
        verified = []
        checked = 0
        blobs = self.index.blobs()
        manifests = self.index.manifests()
        for row in blobs:
            hash = row["hash"]
            path = self.store.path_of(hash)
            if not path.is_file():
                report.missing.append(hash)
            elif path.stat().st_size != row["size"]:
                report.corrupt.append(hash)
            elif deep and (limit is None or checked < limit):
                checked += 1
                if hash_file(path) == hash:
                    verified.append(hash)
                else:
                    report.corrupt.append(hash)

        for info in manifests:
            path = self.manifest.File(info.name)
            if (not path.is_file() or path.stat().st_size != info.size
                    or (deep and hash_file(path) != info.content_hash)):
                report.manifest_drift.append(info.name)
        recorded = {info.name for info in manifests}
        report.manifest_drift += [
            name for name in self.manifest.List() if name not in recorded
        ]

        indexed = {row["hash"] for row in blobs}
        report.orphan = sorted(
            path.name if valid_hash(path.name) else str(path)
            for path in self.store.files.glob("*/*")
            if path.is_file() and path.name not in indexed
        )
        report.stale_temp = sorted(
            str(path.relative_to(self.root))
            for path in self.store.temp.rglob("*") if path.is_file()
        )
        report.unreferenced = [row["hash"] for row in self.index.unreferenced()]
        with write_lock(self.root):
            self.index.mark_verified(verified)
        return report

    def GC(self, grace_days=7, dry_run=True):
        cutoff = time.time() - grace_days * 86400
        if dry_run:
            return [row["hash"] for row in self.index.unreferenced(cutoff)]
        with write_lock(self.root):
            hashes = [row["hash"] for row in self.index.unreferenced(cutoff)]
            for hash in hashes:
                self.store.remove(hash)
            self.index.remove_blobs(hashes)
            return hashes

    def RebuildIndex(self, deep=False):
        # Index.db 只是查询缓存，始终可从 File 与 Manifest 重建
        return self.manifest.rebuild(deep)

    def close(self):
        self.manifest.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
