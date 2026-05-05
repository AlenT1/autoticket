"""Debug: trace field extraction for ARB-AUTH-001."""

from pathlib import Path

from file_to_jira.parse.markdown_parser import (
    _extract_fields,
    _extract_file_hints,
    _looks_like_path,
    parse_markdown,
    read_and_decode,
)
from file_to_jira.util.ids import file_sha256_bytes

raw, decoded = read_and_decode(Path("examples/Bugs_For_Dev_Review_2026-05-03.md"))
sha = file_sha256_bytes(raw)
result = parse_markdown(decoded, source_sha256=sha)

target = next(b for b in result.bugs if b.external_id == "ARB-AUTH-001")
print("RAW BODY:")
print(target.raw_body)
print()
print("HINTED FILES:", target.hinted_files)
print()

# Re-extract fields manually from the body lines.
print("=" * 60)
print("Re-running _extract_fields on the body lines:")
fields = _extract_fields(target.raw_body.splitlines())
for label, text in fields.items():
    print(f"\n--- FIELD: {label!r} ---")
    print(text)

print()
print("=" * 60)
print("Path heuristic on each backticked token in 'affected_files':")
import re

affected = fields.get("affected_files", "")
for tok in re.findall(r"`([^`]+)`", affected):
    print(f"  token={tok!r}  looks_like_path={_looks_like_path(tok)}")
