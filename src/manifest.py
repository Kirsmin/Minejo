import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, unquote

from index import Index
from store import BlobMissing, BlobStore, atomic_write, hash_file, write_lock


class ManifestNotFound(FileNotFoundError):
    pass


class DependencyMissing(FileNotFoundError):
    pass


class ManifestInUse(RuntimeError):
    pass


class Binding(NamedTuple):
    name: str
    hash: str
    size: int
    url: str


@dataclass
class Extracted:
    bindings: list[Binding] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)


class Manifest:
    def __init__(self, root, extractors=()):
        # extractors: [(名称正则, 函数)]，函数把原始字节解析为 Extracted
        self.root = Path(root)
        self.files = self.root / "Manifest"
        self.files.mkdir(parents=True, exist_ok=True)
        self.store = BlobStore(self.root)
        self.extractors = [(re.compile(pattern), func) for pattern, func in extractors]
        with write_lock(self.root):
            self.index = Index(self.root)
            self.index.reconcile(self.files, self.store, self._extract)

    def _extract(self, name, content):
        for pattern, extractor in self.extractors:
            if pattern.fullmatch(name):
                return extractor(content)
        return Extracted()

    def _read(self, name):
        path = self.File(name)
        if not path.is_file():
            raise ManifestNotFound(name)
        return path.read_bytes()

    def _check_case(self, name):
        for path in self.files.iterdir():
            other = unquote(path.name)
            if other != name and other.casefold() == name.casefold():
                raise FileExistsError(f"Manifest 名称仅大小写不同: {other}")

    def File(self, name):
        return self.files / quote(name, safe="")

    def Read(self, name):
        return self._read(name).decode("utf-8")

    def Update(self, name, content, source_url=None, etag=None):
        content = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        extracted = self._extract(name, content)
        with write_lock(self.root):
            missing_deps = [dep for dep in extracted.deps if not self.Exists(dep)]
            if missing_deps:
                raise DependencyMissing(", ".join(missing_deps))
            missing_blobs = sorted({b.hash for b in extracted.bindings
                                    if not self.store.has(b.hash)})
            if missing_blobs:
                raise BlobMissing(", ".join(missing_blobs))
            self._check_case(name)
            path = self.File(name)
            content_hash = hashlib.sha1(content).hexdigest()
            changed = not path.is_file() or hash_file(path) != content_hash
            if changed:
                # 原子替换是提交点；若随后崩溃，启动对账会修复数据库
                atomic_write(path, content, self.store.temp)
            self.index.adopt(extracted.bindings, self.store)
            self.index.record_manifest(
                name, content_hash, len(content), extracted.bindings, source_url, etag
            )
            return changed

    def Delete(self, name):
        with write_lock(self.root):
            path = self.File(name)
            if not path.is_file():
                raise ManifestNotFound(name)
            dependents = []
            for other in self.files.iterdir():
                other_name = unquote(other.name)
                if other_name != name and name in self._extract(
                    other_name, other.read_bytes()
                ).deps:
                    dependents.append(other_name)
            if dependents:
                raise ManifestInUse(f"仍被依赖: {', '.join(sorted(dependents))}")
            path.unlink()
            self.index.delete_manifest(name)

    def Exists(self, name):
        return self.File(name).is_file()

    def List(self, prefix=""):
        return sorted(
            unquote(path.name) for path in self.files.iterdir()
            if path.is_file() and unquote(path.name).startswith(prefix)
        )

    def Info(self, name):
        if not self.Exists(name):
            raise ManifestNotFound(name)
        info = self.index.manifest(name)
        if info is None:
            self.reconcile()
            info = self.index.manifest(name)
        return info

    def Extract(self, name):
        return self._extract(name, self._read(name))

    def Locate(self, hashes):
        # 按 hash 找回来源绑定（含 url），用于修复时重新下载
        wanted, found = set(hashes), {}
        for scope in self.index.scopes(wanted):
            for binding in self.Extract(scope).bindings:
                if binding.hash in wanted:
                    found.setdefault(binding.hash, binding)
        return found

    def reconcile(self):
        with write_lock(self.root):
            self.index.reconcile(self.files, self.store, self._extract)

    def rebuild(self, deep=False):
        with write_lock(self.root):
            return self.index.rebuild(self.files, self.store, self._extract, deep)

    def close(self):
        self.index.close()
