"""jira_task_agent — Doc-to-Jira sync agent.

Mirrors planning docs in a Google Drive folder (or local folder) into the
configured Jira project: classifies each doc, extracts ``{epic, tasks}``
via LLM, matches against live Jira via a two-stage LLM matcher, then
creates / updates / no-ops. Writes are gated behind ``apply=True``;
default is dry-run, producing ``data/run_plan.json``.

Public Python API for autodev / library callers:

    from jira_task_agent import run_once, RunReport, __version__

CLI: ``python -m jira_task_agent run [flags]`` or ``jira-task-agent run``.
"""
from .runner import RunReport, run_once

__version__ = "0.1.0"

__all__ = ["RunReport", "run_once", "__version__"]
