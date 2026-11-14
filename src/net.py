import hashlib
import os
import threading
import time

import httpx

import log
from store import HashMismatch

# 首次失败后依次等待这些间隔（秒）重试
RETRY_DELAYS = (15, 30, 60, 300)
CHUNK = 1 << 16


class RetryExhausted(RuntimeError):
    pass


class Aborted(RuntimeError):
    pass


# 把 Mojang 元数据 API 摊到每分钟 per_minute 次
class RateLimiter:
    def __init__(self, per_minute):
        if per_minute <= 0:
            raise ValueError("per_minute 必须大于 0")
        self.interval = 60 / per_minute
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)
        return delay


# 429/5xx 与网络错误、哈希不符都值得重试，其余直接抛给调用方
def retryable(error):
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        return code == 429 or code >= 500
    return isinstance(error, (httpx.TransportError, HashMismatch))


def retry_after(error):
    response = getattr(error, "response", None)
    value = response.headers.get("Retry-After", "") if response is not None else ""
    return int(value) if value.isdigit() else 0


def describe(error):
    return f"{type(error).__name__}: {error}"


class Network:
    def __init__(self, client=None, api_per_minute=None, delays=RETRY_DELAYS):
        self.client = client or httpx.Client(follow_redirects=True, timeout=60)
        self.limiter = RateLimiter(api_per_minute) if api_per_minute else None
        self.delays = delays
        self.stopped = threading.Event()

    def stop(self):
        # 通知所有线程放弃重试和下载，以便程序尽快退出
        self.stopped.set()

    def retry(self, action, what):
        # 首次失败后依次等待 delays 中的间隔重试，全部失败则抛出 RetryExhausted
        for attempt, delay in enumerate((*self.delays, None)):
            if self.stopped.is_set():
                raise Aborted(what)
            try:
                return action()
            except Exception as error:
                if not retryable(error):
                    raise
                if delay is None:
                    raise RetryExhausted(
                        f"{what} 连续失败 {len(self.delays) + 1} 次: {describe(error)}"
                    ) from error
                wait = max(delay, retry_after(error))
                log.warn(
                    f"{what} 失败 ({describe(error)})，{wait}s 后重试 "
                    f"({attempt + 1}/{len(self.delays)})"
                )
                if self.stopped.wait(wait):
                    raise Aborted(what) from error

    def get(self, url, etag=None, sha1=None):
        # 只限制 Mojang 元数据 API，叶子资源下载走 download
        def request():
            delay = self.limiter.wait() if self.limiter else 0
            if delay:
                log.debug(f"API 限流等待 {delay:.2f}s")
            log.debug(f"API 请求: {url}")
            headers = {"If-None-Match": etag} if etag else {}
            response = self.client.get(url, headers=headers)
            log.debug(f"API 响应 {response.status_code}: {url}")
            if response.status_code != 304:
                response.raise_for_status()
                if sha1 and hashlib.sha1(response.content).hexdigest() != sha1.lower():
                    raise HashMismatch(f"SHA-1 不符: {url}")
            return response

        return self.retry(request, url)

    def download(self, url, part, size):
        # 流式写入 part，已有内容则从断点续传；完整性由调用方的 put 校验
        if part.is_file() and part.stat().st_size > size:
            part.unlink()
        if part.is_file() and part.stat().st_size == size:
            log.debug(f"续传文件已完整，跳过下载: {url}")
            return
        offset = part.stat().st_size if part.is_file() else 0
        for _ in range(2):
            log.debug(f"下载请求: {url}，偏移 {log.human_size(offset)}")
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with self.client.stream("GET", url, headers=headers) as response:
                log.debug(f"下载响应 {response.status_code}: {url}")
                if response.status_code == 416 and offset:
                    log.warn(f"续传已失效，重新下载: {url}")
                    part.unlink(missing_ok=True)
                    offset = 0
                    continue
                response.raise_for_status()
                mode = "ab" if offset and response.status_code == 206 else "wb"
                with part.open(mode) as file:
                    for chunk in response.iter_bytes(CHUNK):
                        if self.stopped.is_set():
                            raise Aborted(url)
                        file.write(chunk)
                    file.flush()
                    os.fsync(file.fileno())
                return

    def close(self):
        self.client.close()
