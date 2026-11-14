import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import log
from log import human_size
from manifest import Binding, Extracted, ManifestNotFound
from net import RETRY_DELAYS, Network
from store import HashMismatch, valid_hash
from url import urlVersionMainfestV2, asset_url


def _binding(name, data):
    return Binding(name, data["sha1"].lower(), data["size"], data["url"])


def extract_version(content):
    data = json.loads(content)
    bindings = {}
    downloads = data.get("downloads", {})
    for key, name in (
        ("client", "client.jar"),
        ("server", "server.jar"),
        ("client_mappings", "client_mappings.txt"),
        ("server_mappings", "server_mappings.txt"),
    ):
        if key in downloads:
            bindings[name] = _binding(name, downloads[key])

    # 不过滤 rules，镜像保留全平台 libraries
    for library in data.get("libraries", []):
        downloads = library.get("downloads", {})
        if "artifact" in downloads:
            artifact = downloads["artifact"]
            name = f"libraries/{artifact['path']}"
            bindings[name] = _binding(name, artifact)
        for classifier in downloads.get("classifiers", {}).values():
            name = f"libraries/{classifier['path']}"
            bindings[name] = _binding(name, classifier)

    logging = data.get("logging", {}).get("client", {}).get("file")
    if logging:
        name = f"logging/{logging['id']}"
        bindings[name] = _binding(name, logging)

    deps = []
    if "assetIndex" in data:
        deps.append(f"assetindex/{data['assetIndex']['id']}")
    return Extracted(list(bindings.values()), deps)


def extract_assetindex(content):
    objects = json.loads(content).get("objects", {})
    return Extracted([
        Binding(name, data["hash"].lower(), data["size"],
                asset_url(data["hash"].lower()))
        for name, data in objects.items()
    ])


# 构造 Resource 时传入，对账、重建与同步共用同一套解析
EXTRACTORS = [
    (r"version/[^/]+", extract_version),
    (r"assetindex/[^/]+", extract_assetindex),
]


class Mojang:
    def __init__(self, resource, client=None, max_workers=4, api_per_minute=None,
                 delays=RETRY_DELAYS):
        if not 1 <= max_workers <= 8:
            raise ValueError("max_workers 应在 1 到 8 之间")
        self.resource = resource
        self.manifest = resource.manifest
        self.store = resource.store
        self.net = Network(client, api_per_minute, delays)
        self.max_workers = max_workers

    def _info(self, name):
        try:
            return self.manifest.Info(name)
        except ManifestNotFound:
            return None

    def _current(self, version):
        name = f"version/{version['id']}"
        info = self._info(name)
        if info is None or info.content_hash != version["sha1"].lower():
            return False
        return all(self.manifest.Exists(dep) for dep in self.manifest.Extract(name).deps)

    def sync(self, version_ids=None):
        info = self._info("version_manifest_v2")
        response = self.net.get(urlVersionMainfestV2, info.etag if info else None)
        if response.status_code == 304:
            # 全量任务可能只完成了一部分，304 时仍用本地清单续跑
            log.info("版本清单未变化，使用本地清单检查未完成版本")
            content = self.manifest.Read("version_manifest_v2")
        else:
            content = response.content
            self.manifest.Update(
                "version_manifest_v2", content, urlVersionMainfestV2,
                response.headers.get("ETag")
            )
            log.debug("版本清单已提交")

        versions = {version["id"]: version for version in json.loads(content)["versions"]}
        if version_ids is None:
            version_ids = list(versions)
            log.info(f"全量目标共 {len(version_ids)} 个版本")
        unknown = [version_id for version_id in version_ids if version_id not in versions]
        if unknown:
            raise ValueError(f"未知版本: {', '.join(unknown)}")
        total = len(version_ids)

        updated = []
        for position, version_id in enumerate(version_ids, 1):
            if self._current(versions[version_id]):
                log.debug(f"[{position}/{total}] {version_id} 已是最新，跳过")
                continue
            log.info(f"[{position}/{total}] 开始同步 {version_id}")
            try:
                self._sync_version(versions[version_id])
            except Exception as error:
                log.error(f"{version_id} 同步失败: {type(error).__name__}: {error}")
                raise
            updated.append(version_id)
            log.info(f"[{position}/{total}] {version_id} 同步完成")
        return updated

    def _sync_version(self, version):
        version_id = version["id"]
        response = self.net.get(version["url"], sha1=version["sha1"])
        content = response.content
        bindings = list(extract_version(content).bindings)

        pending = None
        asset = json.loads(content).get("assetIndex")
        if asset:
            info = self._info(f"assetindex/{asset['id']}")
            if info and info.content_hash == asset["sha1"].lower():
                log.debug(f"{version_id} 复用本地资源索引 {asset['id']}")
            else:
                asset_response = self.net.get(asset["url"], sha1=asset["sha1"])
                bindings += extract_assetindex(asset_response.content).bindings
                pending = (f"assetindex/{asset['id']}", asset, asset_response)
                log.debug(f"{version_id} 使用资源索引 {asset['id']}")

        leaves = {binding.hash: binding for binding in bindings}
        missing = [b for b in leaves.values() if not self.store.has(b.hash)]
        log.info(
            f"{version_id} 资源 {len(leaves)} 个，缓存命中 "
            f"{len(leaves) - len(missing)} 个，待下载 {len(missing)} 个"
            f"（{human_size(sum(b.size for b in missing))}）"
        )
        self._fetch(missing, version_id)

        # 被依赖者先提交，保证 Manifest 不变量
        if pending:
            name, asset, asset_response = pending
            log.debug(f"提交 Manifest: {name}")
            self.manifest.Update(
                name, asset_response.content, asset["url"],
                asset_response.headers.get("ETag"),
            )
        log.debug(f"提交 Manifest: version/{version_id}")
        self.manifest.Update(
            f"version/{version_id}", content, version["url"], response.headers.get("ETag")
        )

    def _fetch(self, bindings, label):
        total = len(bindings)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(self._download, binding) for binding in bindings]
            try:
                for done, future in enumerate(as_completed(futures), 1):
                    binding = future.result()
                    log.debug(
                        f"{label} 下载入库 {done}/{total}: {binding.hash}，"
                        f"{human_size(binding.size)}"
                    )
            except BaseException:
                # 任一资源最终失败（或被中断）时，让其余线程尽快放弃
                self.net.stop()
                raise

    def _download(self, binding):
        part = self.store.temp / f"{binding.hash}.part"

        def attempt():
            self.net.download(binding.url, part, binding.size)
            try:
                self.store.put(part, binding.hash, binding.size)
            except HashMismatch:
                part.unlink(missing_ok=True)
                raise

        self.net.retry(attempt, binding.url)
        return binding

    def repair(self, deep=False, limit=None):
        # 对账 → 清理残留 → 检查 → 对缺失与损坏的 blob 按来源重新下载
        self.manifest.reconcile()
        self.store.clean_temp()
        report = self.resource.Check(deep, limit)
        bad = sorted({hash for hash in report.missing + report.corrupt if valid_hash(hash)})
        found = self.manifest.Locate(bad)
        for hash in bad:
            if hash not in found:
                log.warn(f"找不到资源来源，跳过: {hash}")
        todo = [found[hash] for hash in bad if hash in found]
        for binding in todo:
            self.store.remove(binding.hash)
        if todo:
            log.info(f"修复 {len(todo)} 个缺失或损坏的资源")
            self._fetch(todo, "修复")
        return report, len(todo)

    def close(self):
        self.net.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
