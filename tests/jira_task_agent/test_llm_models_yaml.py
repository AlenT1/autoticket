"""Tests for jira_task_agent.llm.client model resolution from YAML.

Verifies:
- Models come from configs/jira_task_agent.yaml when present.
- Hard-coded defaults kick in when YAML is missing.
- Fallback chain de-dupes (primary not repeated).
- Cache is invalidated between tests via direct module attribute reset.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def reload_llm_client(monkeypatch, tmp_path):
    """Point _CONFIG_PATH at a temp YAML and reset the cache between runs."""
    from jira_task_agent.llm import client as llm_client

    yaml_path = tmp_path / "jira_task_agent.yaml"
    monkeypatch.setattr(llm_client, "_CONFIG_PATH", yaml_path)
    monkeypatch.setattr(llm_client, "_yaml_cache", None)

    def write(content: str | None) -> None:
        if content is None:
            if yaml_path.exists():
                yaml_path.unlink()
        else:
            yaml_path.write_text(content)
        # Force re-read on next call.
        monkeypatch.setattr(llm_client, "_yaml_cache", None)

    return llm_client, write


def test_models_come_from_yaml(reload_llm_client):
    llm_client, write = reload_llm_client
    write(
        "models:\n"
        "  classify:  cls-model\n"
        "  extract:   ext-model\n"
        "  summarize: sum-model\n"
    )

    assert llm_client.models_classify()[0] == "cls-model"
    assert llm_client.models_extract()[0] == "ext-model"
    assert llm_client.models_summarize()[0] == "sum-model"


def test_yaml_missing_uses_hardcoded_defaults(reload_llm_client):
    llm_client, write = reload_llm_client
    write(None)  # no YAML file

    assert llm_client.models_classify()[0] == "meta/llama-3.1-70b-instruct"
    assert llm_client.models_extract()[0] == "openai/openai/gpt-5.2"
    assert llm_client.models_summarize()[0] == "meta/llama-3.1-8b-instruct"


def test_partial_yaml_falls_back_per_task(reload_llm_client):
    """Only `classify` set — extract/summarize use hard defaults."""
    llm_client, write = reload_llm_client
    write(
        "models:\n"
        "  classify:  custom-classifier\n"
    )

    assert llm_client.models_classify()[0] == "custom-classifier"
    assert llm_client.models_extract()[0] == "openai/openai/gpt-5.2"
    assert llm_client.models_summarize()[0] == "meta/llama-3.1-8b-instruct"


def test_fallback_chain_dedupes_primary(reload_llm_client):
    """If the YAML primary is also in the fallback list, it appears only once."""
    llm_client, write = reload_llm_client
    write(
        "models:\n"
        "  classify: openai/openai/gpt-5.2\n"  # also in the hard fallback list
    )

    chain = llm_client.models_classify()
    assert chain[0] == "openai/openai/gpt-5.2"
    assert chain.count("openai/openai/gpt-5.2") == 1


def test_invalid_yaml_raises(reload_llm_client):
    llm_client, write = reload_llm_client
    write("- this is a list, not a mapping\n")

    with pytest.raises(ValueError, match="mapping"):
        llm_client.models_classify()


def test_real_shipped_yaml_is_consumable():
    """End-to-end: the actual configs/jira_task_agent.yaml shipped in the repo
    parses cleanly and produces a valid model chain."""
    from jira_task_agent.llm import client as llm_client
    importlib.reload(llm_client)

    chain = llm_client.models_classify()
    assert isinstance(chain, list)
    assert len(chain) >= 1
    assert all(isinstance(m, str) and m for m in chain)
