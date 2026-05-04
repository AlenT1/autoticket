"""The `submit_enrichment` final-output tool.

Built as a closure over the resolved repo paths so that file-existence checks
can be enforced. This is the single highest-leverage validation in the agent
loop — it catches hallucinated file paths before they end up in Jira.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...models import EnrichedBug


_PENDING_META = {
    "model": "_pending",
    "started_at": "_pending",
    "finished_at": "_pending",
}


def _stub_enrichment_meta(payload: dict[str, Any]) -> dict[str, Any]:
    """Inject a placeholder enrichment_meta if the agent didn't supply one.

    The orchestrator overwrites this with real metadata after a successful submit.
    """
    if "enrichment_meta" in payload:
        return payload
    return {**payload, "enrichment_meta": _PENDING_META}


def _format_validation_errors(ve: ValidationError) -> list[str]:
    out: list[str] = []
    for err in ve.errors():
        loc = ".".join(str(x) for x in err["loc"])
        out.append(f"{loc}: {err['msg']}")
    return out


def _check_one_reference(
    index: int,
    ref: Any,
    repo_paths: dict[str, Path],
) -> str | None:
    """Validate a single CodeReference against the cloned repo. Returns an error
    message or None if the reference is OK."""
    if ref.repo_alias not in repo_paths:
        return (
            f"code_references[{index}]: unknown repo_alias {ref.repo_alias!r} "
            f"(known: {sorted(repo_paths)})"
        )
    repo_root = repo_paths[ref.repo_alias]
    full = (repo_root / ref.file_path).resolve()
    try:
        full.relative_to(repo_root)
    except ValueError:
        return f"code_references[{index}]: path escapes repo root: {ref.file_path!r}"
    if not full.is_file():
        return (
            f"code_references[{index}]: file not found in repo "
            f"{ref.repo_alias!r}: {ref.file_path!r}"
        )
    return None


def _check_all_references(
    enriched: EnrichedBug, repo_paths: dict[str, Path]
) -> list[str]:
    errors: list[str] = []
    for i, ref in enumerate(enriched.code_references):
        err = _check_one_reference(i, ref, repo_paths)
        if err:
            errors.append(err)
    return errors


def build_submit_tool(
    repo_paths: dict[str, Path],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a `submit_enrichment(payload) -> result` callable.

    The returned callable validates ``payload`` against the ``EnrichedBug``
    schema. On success it returns ``{"ok": True, "enriched": ...}``; on failure
    it returns ``{"ok": False, "errors": [...]}`` so the agent can self-correct
    without raising — the SDK shows the error string back to the agent as a
    tool result.

    Each entry in ``payload["code_references"]`` must point at an existing file
    in the corresponding cloned repo. Invalid references go in ``errors``.
    """
    repo_paths = {alias: Path(p).resolve() for alias, p in repo_paths.items()}

    def submit_enrichment(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "errors": [f"payload must be an object, got {type(payload).__name__}"],
            }
        payload = _stub_enrichment_meta(payload)

        try:
            enriched = EnrichedBug.model_validate(payload)
        except ValidationError as ve:
            return {"ok": False, "errors": _format_validation_errors(ve)}

        ref_errors = _check_all_references(enriched, repo_paths)
        if ref_errors:
            return {"ok": False, "errors": ref_errors}

        return {"ok": True, "enriched": enriched.model_dump(mode="json")}

    return submit_enrichment
