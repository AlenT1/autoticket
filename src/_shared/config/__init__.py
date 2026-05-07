"""Unified settings layer.

One ``Settings`` object, loaded from ``.env`` + layered YAML, that every
shared resource (Jira client, LLM providers, Drive source, repo cache)
consumes via a uniform ``from_settings(settings)`` classmethod.

Resolution order (later wins):
1. YAML defaults (``configs/shared.yaml``)
2. ``.env`` file at CWD
3. Environment variables
4. Explicit overrides passed to ``Settings(**kwargs)``

Each field has exactly one canonical env-var name (the field name
uppercased — e.g. ``nvidia_api_key`` ↔ ``NVIDIA_API_KEY``). No aliases:
the operator's ``.env`` uses the canonical names only.

Agent-specific YAMLs (``configs/f2j.yaml``,
``configs/jira_task_agent.yaml``) are loaded by the agents directly,
not by this shared layer.
"""
from .settings import Settings, load_settings

__all__ = ["Settings", "load_settings"]
