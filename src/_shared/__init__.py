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
