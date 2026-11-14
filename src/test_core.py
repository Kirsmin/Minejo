import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from log import human_size
from manifest import Binding, DependencyMissing, Extracted, ManifestInUse
from mojang import EXTRACTORS, Mojang
from net import Network, RetryExhausted
from resource import Resource
from store import BlobMissing, HashMismatch, write_lock
from url import urlVersionMainfestV2, asset_url


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def extract(content):
    data = json.loads(content)
    return Extracted(
        [Binding(name, hash, size, "") for name, (hash, size) in data["files"].items()],
        data["deps"],
    )


def content(files, deps=()):
    return json.dumps({
        "files": {name: [sha1(data), len(data)] for name, data in files.items()},
        "deps": list(deps),
    })


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Resource"
        self.extractors = [(r"pack/[^/]+", extract)]
        self.resource = Resource(self.root, self.extractors)
        self.manifest = self.resource.manifest
        self.store = self.resource.store

    def tearDown(self):
        self.resource.close()
        self.tmp.cleanup()

    def blob(self, data):
        part = self.store.temp / f"{sha1(data)}.part"
        part.write_bytes(data)
        return self.store.put(part, sha1(data), len(data))

    def snapshot(self):
        index = self.resource.index
        return (
            {row["hash"]: row["size"] for row in index.blobs()},
            {info.name: (info.content_hash, info.size) for info in index.manifests()},
            {info.name: [tuple(row) for row in index.entries(info.name)]
             for info in index.manifests()},
        )

    def test_put_is_idempotent(self):
        first, second = self.blob(b"hello"), self.blob(b"hello")
        self.assertEqual(first, second)
        self.assertEqual(self.store.path_of(first).read_bytes(), b"hello")
        self.assertEqual(list(self.store.temp.iterdir()), [])

    def test_put_rejects_wrong_content(self):
        part = self.store.temp / "x.part"
        part.write_bytes(b"abc")
        with self.assertRaises(HashMismatch):
            self.store.put(part, sha1(b"other"), 3)
        self.assertFalse(self.store.has(sha1(b"other")))

    def test_update_requires_blobs_and_deps(self):
        files = {"a": b"1"}
        with self.assertRaises(BlobMissing):
            self.manifest.Update("pack/x", content(files))
        self.blob(b"1")
        with self.assertRaises(DependencyMissing):
            self.manifest.Update("pack/x", content(files, ["pack/base"]))
        self.assertFalse(self.manifest.Exists("pack/x"))
        self.manifest.Update("pack/base", content({}))
        self.assertTrue(self.manifest.Update("pack/x", content(files, ["pack/base"])))
        self.assertFalse(self.manifest.Update("pack/x", content(files, ["pack/base"])))
        self.assertEqual(self.resource.Read("pack/x", "a").read_bytes(), b"1")
        with self.assertRaises(ManifestInUse):
            self.manifest.Delete("pack/base")

    def test_reconcile_after_crash(self):
        self.blob(b"1")
        # 模拟提交点之后、数据库更新之前崩溃：文件已替换，数据库未更新
        self.manifest.File("pack/x").write_bytes(content({"a": b"1"}).encode())
        self.resource.close()
        self.resource = Resource(self.root, self.extractors)
        self.assertEqual(self.resource.Read("pack/x", "a").read_bytes(), b"1")

    def test_rebuild_matches_original(self):
        for data in (b"s", b"a", b"b"):
            self.blob(data)
        self.manifest.Update("pack/a", content({"s": b"s", "a": b"a"}))
        self.manifest.Update("pack/b", content({"s": b"s", "b": b"b"}))
        before = self.snapshot()
        report = self.resource.RebuildIndex()
        self.assertFalse(report.missing or report.corrupt)
        self.assertEqual(self.snapshot(), before)

    def test_gc_keeps_shared_blobs(self):
        for data in (b"s", b"a", b"b"):
            self.blob(data)
        self.manifest.Update("pack/a", content({"s": b"s", "a": b"a"}))
        self.manifest.Update("pack/b", content({"s": b"s", "b": b"b"}))
        self.manifest.Delete("pack/a")
        self.assertEqual(self.resource.GC(-1), [sha1(b"a")])
        self.assertEqual(self.resource.GC(-1, dry_run=False), [sha1(b"a")])
        self.assertTrue(self.store.has(sha1(b"s")))
        self.assertTrue(self.store.has(sha1(b"b")))
        self.assertFalse(self.store.has(sha1(b"a")))

    def test_check_finds_problems(self):
        for data in (b"1", b"22", b"333"):
            self.blob(data)
        self.manifest.Update("pack/x", content({"a": b"1", "b": b"22", "c": b"333"}))
        self.assertTrue(self.resource.Check().ok)
        self.store.path_of(sha1(b"1")).unlink()
        self.store.path_of(sha1(b"22")).write_bytes(b"2")
        self.store.path_of(sha1(b"333")).write_bytes(b"xxx")
        stray = self.store.files / "ab" / ("a" * 40)
        stray.parent.mkdir(exist_ok=True)
        stray.write_bytes(b"?")
        shallow = self.resource.Check()
        self.assertEqual(shallow.missing, [sha1(b"1")])
        self.assertEqual(shallow.corrupt, [sha1(b"22")])
        self.assertEqual(shallow.orphan, ["a" * 40])
        deep = self.resource.Check(deep=True)
        self.assertEqual(sorted(deep.corrupt), sorted([sha1(b"22"), sha1(b"333")]))

    def test_write_lock_is_reentrant(self):
        with write_lock(self.root), write_lock(self.root):
            pass

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1536), "1.50 KB")
        self.assertEqual(human_size(31060443), "29.62 MB")
        self.assertEqual(human_size(5 * 1024 ** 3), "5.00 GB")


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class NetworkTest(unittest.TestCase):
    def test_gives_up_after_all_retries(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500)

        with self.assertRaises(RetryExhausted):
            Network(client(handler), delays=(0, 0, 0, 0)).get("https://x/y")
        self.assertEqual(len(calls), 5)

    def test_recovers_after_failures(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, content=b"ok")

        self.assertEqual(Network(client(handler), delays=(0, 0, 0, 0)).get("https://x/y").content, b"ok")
        self.assertEqual(len(calls), 3)

    def test_client_errors_are_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(404)

        with self.assertRaises(httpx.HTTPStatusError):
            Network(client(handler), delays=(0, 0, 0, 0)).get("https://x/y")
        self.assertEqual(len(calls), 1)

    def test_download_resumes(self):
        data = bytes(range(256)) * 10
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "p.part"
            part.write_bytes(data[:1000])
            seen = []

            def handler(request):
                seen.append(request.headers.get("Range"))
                return httpx.Response(206, content=data[1000:])

            Network(client(handler)).download("https://x/y", part, len(data))
            self.assertEqual(seen, ["bytes=1000-"])
            self.assertEqual(part.read_bytes(), data)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "Resource"
        self.jar, self.lib, self.asset = b"jar" * 100, b"lib" * 50, b"asset" * 20
        self.requests = []
        index = json.dumps({"objects": {"minecraft/a.txt": {"hash": sha1(self.asset), "size": len(self.asset)}}}).encode()
        version = json.dumps({
            "id": "1.0",
            "downloads": {"client": {"sha1": sha1(self.jar), "size": len(self.jar), "url": "https://f.test/client.jar"}},
            "libraries": [{"downloads": {"artifact": {
                "path": "a/b/c.jar", "sha1": sha1(self.lib), "size": len(self.lib), "url": "https://f.test/c.jar"}}}],
            "assetIndex": {"id": "9", "sha1": sha1(index), "size": len(index), "url": "https://m.test/9.json"},
        }).encode()
        listing = json.dumps({"versions": [{"id": "1.0", "url": "https://m.test/1.0.json", "sha1": sha1(version)}]}).encode()
        self.routes = {
            urlVersionMainfestV2: listing, "https://m.test/1.0.json": version, "https://m.test/9.json": index,
            "https://f.test/client.jar": self.jar, "https://f.test/c.jar": self.lib,
            asset_url(sha1(self.asset)): self.asset,
        }
        self.resource = Resource(root, EXTRACTORS)
        self.mojang = Mojang(self.resource, client(self.handler), delays=(0,))

    def handler(self, request):
        self.requests.append(request.url)
        if request.url == urlVersionMainfestV2 and request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=self.routes[request.url], headers={"ETag": '"v1"'})

    def tearDown(self):
        self.mojang.close()
        self.resource.close()
        self.tmp.cleanup()

    def test_sync_is_incremental_and_repairable(self):
        self.assertEqual(self.mojang.sync(), ["1.0"])
        self.assertEqual(self.resource.Read("version/1.0", "client.jar").read_bytes(), self.jar)
        self.assertEqual(self.resource.Read("assetindex/9", "minecraft/a.txt").read_bytes(), self.asset)
        self.assertTrue(self.resource.Check(deep=True).ok)

        self.requests.clear()
        self.assertEqual(self.mojang.sync(), [])
        self.assertEqual(len(self.requests), 1)

        path = self.resource.Read("version/1.0", "libraries/a/b/c.jar")
        path.write_bytes(b"broken")
        self.assertFalse(self.resource.Check().ok)
        self.assertEqual(self.mojang.repair()[1], 1)
        self.assertEqual(path.read_bytes(), self.lib)
        self.assertTrue(self.resource.Check(deep=True).ok)

    def test_shared_asset_index_is_reused(self):
        self.mojang.sync()
        self.requests.clear()
        self.resource.manifest.Delete("version/1.0")
        self.mojang.sync()
        self.assertNotIn("https://m.test/9.json", self.requests)


if __name__ == "__main__":
    unittest.main()
