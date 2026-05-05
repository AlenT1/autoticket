"""Live smoke for jira-task-agent in capture mode (no Jira writes).

Loads .env (f2j-style env vars), bridges them to the drive's expected
names, then invokes the drive runner. Usage:

    uv run python scripts/smoke_drive_capture.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env first so JIRA_PAT / NVIDIA_LLM_API_KEY appear in os.environ.
load_dotenv()

# Bridge f2j naming → drive naming (the merge will eventually consolidate).
if "JIRA_TOKEN" not in os.environ and os.environ.get("JIRA_PAT"):
    os.environ["JIRA_TOKEN"] = os.environ["JIRA_PAT"]
if "NVIDIA_API_KEY" not in os.environ and os.environ.get("NVIDIA_LLM_API_KEY"):
    os.environ["NVIDIA_API_KEY"] = os.environ["NVIDIA_LLM_API_KEY"]

# Drive expects host-only (no scheme). f2j config has the full URL — read it
# back out via f2j's loader if not already set.
if not os.environ.get("JIRA_HOST"):
    from file_to_jira.config import load_config
    cfg = load_config()
    if cfg.jira.url:
        host = cfg.jira.url
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        os.environ["JIRA_HOST"] = host.rstrip("/")
    if cfg.jira.project_key:
        os.environ.setdefault("JIRA_PROJECT_KEY", cfg.jira.project_key)
    if cfg.jira.auth_mode:
        os.environ.setdefault("JIRA_AUTH_MODE", cfg.jira.auth_mode)

print("--- bridged env (drive's view) ---")
for k in ("JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_AUTH_MODE"):
    print(f"  {k} = {os.environ.get(k) or 'UNSET'}")
for k in ("JIRA_TOKEN", "NVIDIA_API_KEY"):
    print(f"  {k} = {'set' if os.environ.get(k) else 'UNSET'}")
print()

# Now run drive — capture mode, --target-epic standing test epic, local source
# (no Google OAuth needed). Will do real LLM calls (~$0.05).
from jira_task_agent.runner import run_once

report = run_once(
    apply=True,                                # required for capture; capture overrides actual writes
    source="local",                            # data/local_files/ — no Google OAuth needed
    download_dir="data/gdrive_files",
    local_dir="data/local_files",
    capture_path="data/would_send.json",       # writes intended POSTs here, NOT to Jira
    target_epic="CENTPM-1253",                 # scope any creates to standing test epic
    only_file_name=None,                       # all files in local_files/
    since_override=None,
    state_path=None,
    use_cache=True,
)

print()
print("=" * 72)
print(f"Run finished. apply={report.apply}")
print(f"  files seen:        {report.files_total}")
print(f"  classified:        {dict(report.files_classified)}")
print(f"  extractions:       ok={report.extractions_ok} failed={report.extractions_failed}")
print(f"  cache hits:        classify={report.cache_hits_classify}  "
      f"extract={report.cache_hits_extract}  match={report.cache_hits_match}")
print(f"  actions by kind:   {dict(report.actions_by_kind)}")
if report.errors:
    print(f"  errors ({len(report.errors)}):")
    for e in report.errors:
        print(f"    - {e}")
print("=" * 72)

# Show the captured payload summary.
capture = Path("data/would_send.json")
if capture.exists():
    import json
    data = json.loads(capture.read_text(encoding="utf-8"))
    print(f"\ndata/would_send.json: {len(data) if isinstance(data, list) else 'n/a'} captured ops")
    if isinstance(data, list) and data:
        print("First captured op:")
        print(json.dumps(data[0], indent=2, default=str)[:600])
