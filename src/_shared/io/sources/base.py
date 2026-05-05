"""Source protocol + the generic ``RawDocument`` shape it yields.

A ``Source`` is anything that yields documents — Google Drive folders,
local directories, single files, manual API submissions, future
Slack/Confluence/Notion imports. Each yields a uniform ``RawDocument``;
bodies parse the content according to their own conventions.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class RawDocument:
    """A single input document — content + metadata, tracker-agnostic.

    Attributes:
        id: Stable identifier. Conventions:
            ``"gdrive::<drive_id>"``, ``"local::<sanitized-name>"``,
            ``"file::<sha256>"``. Bodies use ``id`` as a cache key.
        name: Human-readable filename or doc title.
        content: Decoded text content. Sources are responsible for any
            export/conversion (e.g. GDoc → markdown). Binary formats are
            out of scope at this layer.
        mtime: Last-modified timestamp (timezone-aware).
        metadata: Arbitrary source-specific extras — creator, last
            modifying user, mime_type, web_view_link, original size, etc.
            Bodies treat this as opaque optional context.
    """

    id: str
    name: str
    content: str
    mtime: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    """Protocol every input source implements.

    ``iter_documents`` yields documents lazily — sources may stream from
    paginated APIs without loading everything at once.
    """

    def iter_documents(
        self,
        *,
        since: datetime | None = None,
        only: str | None = None,
    ) -> Iterable[RawDocument]:
        """Yield documents.

        Args:
            since: If set, yield only documents whose ``mtime`` is strictly
                greater than ``since`` (timezone-aware). ``None`` returns
                everything available.
            only: If set, yield only the document whose ``name`` matches
                exactly. Used for ``--only`` CLI flags. ``None`` returns
                everything matching the ``since`` filter.
        """
        ...
