"""Root-bound, no-symlink source reads and streaming content identity."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

DESCRIPTOR_OPENS = os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

SOURCE_ACCOUNTING_FIELDS = ('operations', 'successful_operations', 'failed_operations',
                            'open_operations', 'stream_bytes', 'hashed_bytes',
                            'returned_prefix_bytes', 'hash_passes')


def empty_source_accounting():
    """Fixed counters only; no source content, path history or observer."""
    return dict.fromkeys(SOURCE_ACCOUNTING_FIELDS, 0)


def _source_measurements(measurements):
    if measurements is not None:
        if type(measurements) is not dict or measurements:
            raise ValueError('Fresh source measurement dictionary required')
        measurements.update(empty_source_accounting(), operations=1)
    return measurements


def source_hash(raw, *, measurements=None):
    """Hash actual immutable bytes; optional counters are outside content identity."""
    measurements = _source_measurements(measurements)
    complete = False
    try:
        if type(raw) is not bytes:
            raise ValueError('Immutable source bytes required')
        digest = hashlib.sha256()
        digest.update(raw)
        if measurements is not None:
            measurements.update(hashed_bytes=len(raw), hash_passes=1)
        result = digest.hexdigest()
        complete = True
        return result
    finally:
        if measurements is not None:
            measurements['successful_operations' if complete else 'failed_operations'] = 1


def code_identity():
    observed = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if observed != _LOADED_SOURCE_SHA256:
        raise RuntimeError('Source guard implementation changed since module import')
    return observed


class PublicationError(RuntimeError):
    """The artifact was replaced; its crash durability is unconfirmed."""
    def __init__(self, owner, artifact):
        super().__init__('Artifact was published, but directory synchronization failed; crash durability is uncertain')
        self.owner, self.artifact = owner, artifact


class SourceRoot:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.secure = DESCRIPTOR_OPENS
        self.fd = None
        if self.secure:
            fd = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in self.root.parts[1:]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
            except BaseException:
                os.close(fd)
                raise
            self.fd = fd
        info = os.fstat(self.fd) if self.fd is not None else self.root.stat()
        self.identity = hashlib.sha256(os.fsencode(self.root) + f':{info.st_dev}:{info.st_ino}'.encode()).hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def parts(path: str):
        value = Path(path)
        if value.is_absolute() or value.drive or not value.parts or '..' in value.parts:
            raise OSError(errno.EPERM, 'Path must name a file inside the repository')
        return value.parts

    @contextmanager
    def open(self, path: str, *, create: bool = False, metadata: bool = False):
        parts = self.parts(path)
        if not self.secure:
            # Fail closed: portable lstat checks cannot prevent an ancestor swap during open.
            raise OSError(errno.ENOTSUP, 'Secure source reads require descriptor-relative no-symlink opens')
        parent = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL) if create else (
                os.O_PATH if metadata and hasattr(os, 'O_PATH') else os.O_RDONLY)
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'rb', buffering=0) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError(errno.EPERM, 'Source must be a regular file')
                yield stream
        finally:
            os.close(parent)

    def info(self, path: str):
        if self.secure:
            # Linux O_PATH pins metadata without dropping another connection's SQLite POSIX locks on close.
            with self.open(path, metadata=True) as stream:
                return os.fstat(stream.fileno())
        # Metadata-only maps remain usable on platforms without safe source reads.
        current = self.root
        for part in self.parts(path):
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise OSError(errno.EPERM, 'Source symlinks are excluded')
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EPERM, 'Source must be a regular file')
        return info

    def read(self, path: str, limit: int, *, hash_full: bool = True, cancel=None, max_bytes=None,
             measurements=None):
        measurements = _source_measurements(measurements)
        complete = False
        try:
            if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
                raise ValueError('Source read byte budget must be nonnegative')
            digest, prefix, consumed = hashlib.sha256(), bytearray(), 0
            with self.open(path) as stream:
                if measurements is not None: measurements['open_operations'] = 1
                before = os.fstat(stream.fileno())
                required = before.st_size if hash_full else min(max(0, limit), before.st_size)
                if max_bytes is not None and required > max_bytes:
                    raise OSError(errno.EFBIG, 'Source read byte budget exceeded')
                while True:
                    size = 64 * 1024 if hash_full else max(0, limit - len(prefix))
                    if max_bytes is not None: size = min(size, max_bytes - consumed)
                    if size <= 0 or not (chunk := stream.read(size)): break
                    if measurements is not None: measurements['stream_bytes'] += len(chunk)
                    if cancel is not None and cancel():
                        raise InterruptedError('Source read cancelled')
                    consumed += len(chunk)
                    if max_bytes is not None and consumed > max_bytes:
                        raise OSError(errno.EFBIG, 'Source read byte budget exceeded')
                    digest.update(chunk)
                    if measurements is not None:
                        measurements['hashed_bytes'] += len(chunk)
                        measurements['hash_passes'] = 1
                    prefix.extend(chunk[:max(0, limit - len(prefix))])
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError(errno.EAGAIN, 'Source changed during read; retry the map')
            if hash_full and consumed != after.st_size:
                raise OSError(errno.EAGAIN, 'Complete source read required')
            result = bytes(prefix), digest.hexdigest(), after
            complete = True
            if measurements is not None:
                measurements.update(returned_prefix_bytes=len(result[0]), hash_passes=1)
            return result
        finally:
            if measurements is not None:
                measurements['successful_operations' if complete else 'failed_operations'] = 1

    @contextmanager
    def atomic_writer(self, name: str, *, text: bool = False, before_replace=None):
        if len(self.parts(name)) != 1 or not self.secure:
            raise OSError(errno.ENOTSUP, 'Atomic cache writes require a guarded directory')
        temporary = f'.{name}.{uuid.uuid4().hex}.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'w' if text else 'wb', encoding='utf-8' if text else None) as stream:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
            if before_replace is not None:
                before_replace()
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            try:
                os.fsync(self.fd)
            except OSError as error:
                raise PublicationError(self.identity, name) from error
        finally:
            try: os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError: pass

    def write_json(self, name: str, value):
        with self.atomic_writer(name, text=True) as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
