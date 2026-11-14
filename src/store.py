import hashlib
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_HASH = re.compile(r"^[0-9a-f]{40}$")
_local = threading.local()


class HashMismatch(ValueError):
    pass


class BlobMissing(FileNotFoundError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def valid_hash(value):
    return bool(_HASH.fullmatch(value))


def hash_file(path):
    digest = hashlib.sha1()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace(source, destination):
    # Windows 文件被占用时短暂重试；Temp 与目标始终位于同一分区
    for attempt in range(3):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if os.name != "nt" or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write(destination, content, temp):
    fd, temporary = tempfile.mkstemp(dir=temp)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        atomic_replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _acquire(file, block):
    if os.name == "nt":
        import msvcrt

        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        mode = msvcrt.LK_LOCK if block else msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(file.fileno(), mode, 1)
                return True
            except OSError:
                if not block:
                    return False
    import fcntl

    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def _release(file):
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _flock(path, block):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if not _acquire(file, block):
            raise AlreadyRunning(str(path))
        try:
            yield
        finally:
            _release(file)


@contextmanager
def write_lock(root):
    # 单写者锁：同一线程可重入，不同线程或进程互斥
    root = Path(root)
    key = str(root.resolve())
    held = _local.__dict__.setdefault("held", set())
    if key in held:
        yield
        return
    with _flock(root / ".lock", True):
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def run_lock(root):
    # 任务级非阻塞锁：已有任务在运行时直接抛出 AlreadyRunning，防止定时任务重叠
    return _flock(Path(root) / ".sync.lock", False)


class BlobStore:
    def __init__(self, root):
        self.root = Path(root)
        self.files = self.root / "File"
        self.temp = self.root / "Temp"
        self.files.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)

    def path_of(self, hash):
        if not valid_hash(hash):
            raise ValueError(f"无效 SHA-1: {hash}")
        return self.files / hash[:2] / hash

    def has(self, hash):
        return self.path_of(hash).is_file()

    def verify(self, hash):
        path = self.path_of(hash)
        return path.is_file() and hash_file(path) == hash

    def put(self, src_path, expect_hash, expect_size):
        expect_hash = expect_hash.lower()
        source = Path(src_path)
        destination = self.path_of(expect_hash)
        # 校验在锁外完成，持锁时间只有一次重命名
        if source.stat().st_size != expect_size or hash_file(source) != expect_hash:
            raise HashMismatch(f"内容校验失败: {source}")
        with write_lock(self.root):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() == destination.resolve():
                return expect_hash
            if destination.exists():
                source.unlink(missing_ok=True)
            else:
                atomic_replace(source, destination)
        return expect_hash

    def remove(self, hash):
        path = self.path_of(hash)
        with write_lock(self.root):
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def clean_temp(self):
        # .part 是可续传的下载，保留；其余均为写入中断留下的残留
        for path in self.temp.iterdir():
            if path.is_file() and path.suffix != ".part":
                path.unlink(missing_ok=True)
