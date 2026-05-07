"""Shared infrastructure for file_to_jira and jira_task_agent.

This namespace is the canonical home for the cross-tool abstractions —
input sources, output sinks, LLM providers — that both bodies consume and
that autodev (stage 4) imports.

Quick reference:

    # LLM provider
    from _shared.llm import LLMProvider, OpenAICompatProvider, get_provider

    # Input sources (yield RawDocument)
    from _shared.io.sources import (
        RawDocument,
        Source,
        GDriveSource,
        LocalFolderSource,
        SingleFileSource,
    )

    # Output sinks (TicketSink protocol + Ticket shape)
    from _shared.io.sinks import Ticket, TicketSink
    from _shared.io.sinks.jira import JiraSink
    from _shared.io.sinks.jira.strategies import (
        LabelSearchStrategy,
        CacheTrustStrategy,
        PickerWithCacheStrategy,
        StaticMapStrategy,
        DeterministicChainStrategy,
        NoOpStrategy,
    )
"""

# Load `.env` into os.environ ONCE, at the earliest possible moment — when
# the `_shared` package is first imported. This happens during pytest
# collection (because test files import from `_shared.io...`), before any
# test runs. As a result, `monkeypatch.delenv(...)` / `monkeypatch.setenv(...)`
# in tests are honored: there's no later re-load to silently restore values.
#
# `override=False` keeps any env vars set by the user / shell / CI.
from pathlib import Path as _Path  # noqa: E402

_env_file = _Path(__file__).resolve().parent.parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv as _load_dotenv  # noqa: E402
    _load_dotenv(_env_file, override=False)
del _Path

