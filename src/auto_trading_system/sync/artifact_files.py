"""Content-addressed files for plans and daily uploads, outside the journal.

Publishing uses an exclusive temporary file, fsync and an atomic hard-link on
the same filesystem. Existing immutable content is compared, never overwritten.
The deployment qualification must verify the target NTFS/storage power-loss
behavior; successful fsync alone is not a hardware durability guarantee.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from auto_trading_system.protocol.v2.canonical import digest

_NAME = re.compile(r"^[a-f0-9]{64}\.json$")


class ArtifactFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, content_hash: str) -> Path:
        name = content_hash.removeprefix("sha256:") + ".json"
        if not content_hash.startswith("sha256:") or not _NAME.fullmatch(name):
            raise ValueError("invalid artifact hash")
        path = self.root / name
        if path.is_symlink():
            raise ValueError("artifact symlinks are not allowed")
        return path

    def put(self, data: bytes, *, maximum_bytes: int = 512 * 1024) -> str:
        if len(data) > maximum_bytes:
            raise ValueError("artifact exceeds size limit")
        content_hash = digest(data)
        target = self._path(content_hash)
        if target.exists():
            if self.read(content_hash, maximum_bytes=maximum_bytes) != data:
                raise ValueError("immutable artifact conflict")
            return content_hash
        temporary = self.root / (".pending-" + uuid4().hex)
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if self.read(content_hash, maximum_bytes=maximum_bytes) != data:
                    raise ValueError("immutable artifact conflict") from None
        finally:
            # Only the exact temporary file created by this call is removed.
            temporary.unlink(missing_ok=True)
        return content_hash

    def read(self, content_hash: str, *, maximum_bytes: int = 512 * 1024) -> bytes:
        with self._path(content_hash).open("rb") as source:
            data = source.read(maximum_bytes + 1)
        if len(data) > maximum_bytes or digest(data) != content_hash:
            raise ValueError("artifact is oversized or corrupt")
        return data
