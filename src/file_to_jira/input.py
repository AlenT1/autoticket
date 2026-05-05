"""f2j input adapter — yields a :class:`_shared.io.sources.RawDocument`.

f2j's parse subcommand consumes a single markdown bug-list file. This
module bridges that file path into the unified ``RawDocument`` shape used
by the shared sources layer.

Why not :class:`SingleFileSource` directly? f2j has charset detection
(UTF-8 → CP1252 → Latin-1 fallback), BOM stripping, and line-ending
normalization that the plain ``SingleFileSource`` (UTF-8 only) doesn't.
Sharon's bug-list docs are reliably UTF-8, but pre-merge byte-equivalence
matters for the parser test suite, so we keep f2j's existing
``read_and_decode`` in the loop.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from _shared.io.sources import RawDocument

from .parse import read_and_decode


class F2JFileSource:
    """A :class:`Source` that yields one :class:`RawDocument` for the f2j
    parse path. Honors f2j's encoding-detection + line-ending normalization
    via :func:`read_and_decode`.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def iter_documents(
        self,
        *,
        since: datetime | None = None,
        only: str | None = None,
    ) -> Iterable[RawDocument]:
        if not self.path.exists() or not self.path.is_file():
            return
        if only and self.path.name != only:
            return
        st = self.path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if since is not None and mtime <= since:
            return

        # f2j's read_and_decode returns (raw_bytes_bom_stripped, decoded_normalized).
        # The sha is computed against the raw bytes (post-BOM-strip) so it's
        # stable across line-ending translation on the way in.
        raw, decoded = read_and_decode(self.path)
        sha = hashlib.sha256(raw).hexdigest()

        yield RawDocument(
            id=f"file::{sha}",
            name=self.path.name,
            content=decoded,
            mtime=mtime,
            metadata={
                "source_kind": "f2j_single_file",
                "absolute_path": str(self.path.resolve()),
                "size": st.st_size,
                "content_sha256": sha,
                "raw_bytes_size": len(raw),
            },
        )
