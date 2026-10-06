"""Where the broker keeps a case's archives when nodes send them to it.

A node ships a case in parts as it runs (archives.py): ``<case>.mesh.tar.gz``,
one ``<case>.case_NNN.tar.gz`` per finished direction, and ``<case>.tar.gz``
last. Every part used to go to a Syncthing master while the broker held a
pointer; that master is gone (2026-10-06). With a store configured
(``CASEBROKER_PARTS_DIR``), a node uploads each part to the broker, in chunks,
and the broker keeps it here -- the only copy that leaves the node.

Why a directory and not the database, although the database is right beside
it: a campaign case is ~8.5 GB of parts (measured 2026-10-06: the mesh ~185 MB,
each of 32 directions ~260 MB, already gzip). In Postgres every byte of that
would be written twice (WAL, then the heap), the nightly ``pg_dump`` would carry
terabytes, a value cannot pass 1 GB, and reading one back goes through the
broker's memory. Here a part is one file, written once, named by its content,
and backups copies only what is new. The database keeps what is ABOUT the
part -- which case, which part, its hash, its size, when it arrived -- which is
what every question about custody asks.

Layout, all under the configured directory::

    objects/<aa>/<sha256>      a stored part, named by its sha256 (aa = its first two hex digits)
    incoming/<sha256>          an upload in progress: its size is how far it has got
    incoming/<sha256>.verify   being hashed, after the last chunk

A part is only ever visible under ``objects/`` once its whole content has been
hashed and matched, and it gets there by a rename on the same file system.
Two cases never share a file by accident: the name is the content, so the same
name is the same bytes.

Nothing here touches the database, and nothing here is fast to be clever: the
files are large and the requests few.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

SHA256 = re.compile(r"[0-9a-f]{64}")
# Cloudflare refuses a request body over 100 MB on its free and Pro plans, and a
# part is up to ~450 MB (a legacy single-archive case, 6.5 GB). So a part moves in
# chunks; this is the largest the broker takes, with headroom under the 100 MB.
MAX_CHUNK = 64 * 1024 * 1024
# What the broker suggests: one chunk is one request, and Cloudflare also gives a
# request 100 s. 32 MiB is 27 s at 10 Mbit/s up.
SUGGESTED_CHUNK = 32 * 1024 * 1024
_READ = 4 * 1024 * 1024


class PartStoreError(Exception):
    """An upload this store will not take: ``status`` is the HTTP code to answer."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass(frozen=True)
class Usage:
    objects: int
    bytes: int
    incoming_bytes: int
    free_bytes: int
    reserve_bytes: int
    max_bytes: int | None = None


class PartStore:
    """The directory, and the rules for writing into it.

    ``reserve_bytes`` is free space the store never eats into: the volume also
    holds Postgres, the host and everything else on the server, and a store that fills
    it takes all of them down with it. An upload that would leave less than the
    reserve is refused before its first byte, and the node keeps its part.
    """

    def __init__(self, root: str | os.PathLike, reserve_bytes: int = 100 * 1024**3,
                 max_bytes: int | None = None):
        self.root = Path(root)
        self.reserve_bytes = int(reserve_bytes)
        # A ceiling of its own, whatever the volume has free: the server is not only
        # this store's, and "until the disk is nearly full" is not a budget.
        self.max_bytes = int(max_bytes) if max_bytes else None
        self.objects = self.root / "objects"
        self.incoming = self.root / "incoming"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)
        # One upload of a given content at a time. The broker is one process, so a
        # lock per hash is enough; it guards the incoming file's length, which IS
        # the upload's offset.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._verifying: set[str] = set()
        self._failed: dict[str, str] = {}

    # -- paths -----------------------------------------------------------------

    @staticmethod
    def check_sha(sha256: str) -> str:
        sha = (sha256 or "").strip().lower()
        if not SHA256.fullmatch(sha):
            raise PartStoreError(422, "sha256 must be 64 hex characters")
        return sha

    def object_path(self, sha256: str) -> Path:
        sha = self.check_sha(sha256)
        return self.objects / sha[:2] / sha

    def _incoming_path(self, sha: str) -> Path:
        return self.incoming / sha

    def _lock(self, sha: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(sha, threading.Lock())

    # -- state -------------------------------------------------------------------

    def has(self, sha256: str) -> bool:
        return self.object_path(sha256).is_file()

    def offset(self, sha256: str) -> int:
        """How much of an upload has arrived (0 for none)."""
        p = self._incoming_path(self.check_sha(sha256))
        try:
            return p.stat().st_size
        except FileNotFoundError:
            return 0

    def state(self, sha256: str) -> str:
        """``stored``, ``verifying``, ``failed: <why>``, ``partial`` or ``absent``."""
        sha = self.check_sha(sha256)
        if self.has(sha):
            return "stored"
        if sha in self._verifying:
            return "verifying"
        if sha in self._failed:
            return "failed: " + self._failed[sha]
        return "partial" if self.offset(sha) else "absent"

    def usage(self) -> Usage:
        n = size = 0
        for p in self.objects.glob("*/*"):
            if p.is_file():
                n += 1
                size += p.stat().st_size
        incoming = sum(p.stat().st_size for p in self.incoming.iterdir() if p.is_file())
        return Usage(n, size, incoming, shutil.disk_usage(self.root).free, self.reserve_bytes, self.max_bytes)

    # -- writing -----------------------------------------------------------------

    def admit(self, sha256: str, size: int) -> int:
        """Whether an upload of ``size`` bytes may start or go on; returns the offset
        to send from. Refuses (507) when what is still to come would cut into the
        reserve -- checked at every call, so an upload that started with room and
        lost it stops at its next chunk, not at a full disk."""
        sha = self.check_sha(sha256)
        if size is None or size <= 0:
            raise PartStoreError(422, "bytes must be the part's size, more than 0")
        if self.has(sha):
            return size
        have = self.offset(sha)
        if have > size:
            # An earlier upload of the same content claimed a different size: one of
            # them was wrong, and the bytes on disk cannot be trusted to be either.
            self.discard(sha)
            have = 0
        still = size - have
        free = shutil.disk_usage(self.root).free
        if free - still < self.reserve_bytes:
            raise PartStoreError(507, "the part store is full", free_bytes=free,
                                 reserve_bytes=self.reserve_bytes, needed_bytes=still)
        if self.max_bytes is not None:
            u = self.usage()
            if u.bytes + u.incoming_bytes + still > self.max_bytes:
                raise PartStoreError(507, "the part store is at its size limit",
                                     used_bytes=u.bytes + u.incoming_bytes,
                                     max_bytes=self.max_bytes, needed_bytes=still)
        self._failed.pop(sha, None)
        return have

    def append(self, sha256: str, size: int, offset: int, chunks) -> int:
        """Append one chunk at ``offset``; ``chunks`` yields its bytes. Returns the new
        offset. 409 (with the offset this store has) when ``offset`` is not where
        the upload stands -- a retry of a chunk that did arrive, or one that was
        lost -- so the sender resumes from the right place instead of guessing."""
        sha = self.check_sha(sha256)
        with self._lock(sha):
            if self.has(sha):
                return size
            if sha in self._verifying:
                raise PartStoreError(409, "the part is being verified", offset=size, state="verifying")
            path = self._incoming_path(sha)
            have = self.offset(sha)
            if offset != have:
                raise PartStoreError(409, f"the upload stands at {have}, not {offset}", offset=have)
            written = 0
            try:
                with open(path, "ab") as f:
                    for piece in chunks:
                        written += len(piece)
                        if written > MAX_CHUNK:
                            raise PartStoreError(413, f"a chunk is at most {MAX_CHUNK} bytes")
                        if have + written > size:
                            raise PartStoreError(422, f"the chunk runs past the part's {size} bytes")
                        f.write(piece)
            except PartStoreError:
                # The chunk is all or nothing: cut the file back to where it stood.
                os.truncate(path, have)
                raise
            except OSError as exc:
                os.truncate(path, have)
                raise PartStoreError(507, f"could not write the chunk: {exc.strerror or exc}") from None
            return have + written

    def complete(self, sha256: str, size: int) -> str:
        """Hash an upload that has all its bytes and, if it is what it claims, move it
        into ``objects/``. Returns ``stored`` or raises 422 (the content is not the
        sha256 it was uploaded as: the incoming bytes are dropped). Blocking: a
        6.5 GB archive takes a minute on the server's CPU, so a caller runs this
        off the request (see verify_in_background)."""
        sha = self.check_sha(sha256)
        if self.has(sha):
            return "stored"
        path = self._incoming_path(sha)
        if self.offset(sha) != size:
            raise PartStoreError(409, "the upload is not complete", offset=self.offset(sha))
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while block := f.read(_READ):
                h.update(block)
        if h.hexdigest() != sha:
            self.discard(sha)
            raise PartStoreError(422, f"the upload hashes to {h.hexdigest()}, not {sha}: send it again")
        target = self.object_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(path, target)
        try:
            dirfd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass                       # not every file system lets a directory be fsynced
        return "stored"

    def verify_in_background(self, sha256: str, size: int, on_stored) -> None:
        """complete() on a thread, then ``on_stored(sha)``; a failure is kept for
        state() to report, and the incoming bytes are gone with it."""
        sha = self.check_sha(sha256)
        with self._lock(sha):
            if sha in self._verifying or self.has(sha):
                return
            self._verifying.add(sha)

        def run():
            try:
                self.complete(sha, size)
            except PartStoreError as exc:
                self._failed[sha] = str(exc)
                return
            except OSError as exc:
                self._failed[sha] = f"could not store it: {exc.strerror or exc}"
                return
            finally:
                self._verifying.discard(sha)
            on_stored(sha)

        threading.Thread(target=run, name=f"verify-{sha[:12]}", daemon=True).start()

    def discard(self, sha256: str) -> None:
        """Drop an upload in progress."""
        sha = self.check_sha(sha256)
        try:
            self._incoming_path(sha).unlink()
        except FileNotFoundError:
            pass

    def remove(self, sha256: str) -> bool:
        """Delete a stored part's file. The caller has checked nothing refers to it."""
        try:
            self.object_path(sha256).unlink()
            return True
        except FileNotFoundError:
            return False
