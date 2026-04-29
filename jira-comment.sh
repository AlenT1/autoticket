#!/usr/bin/env bash
# jira-comment.sh — Post a comment to a Jira issue.
# Usage: bash jira-comment.sh <issue_key> <message>

set -euo pipefail

ISSUE_KEY="${1:?usage: jira-comment.sh ISSUE_KEY BODY}"
BODY="${2:?usage: jira-comment.sh ISSUE_KEY BODY}"
JIRA_HOST="${JIRA_HOST:?JIRA_HOST must be set}"

TOKEN=""
for f in \
  "${HOME}/.autodev/tokens/task-jira-${REPO_NAME:-unknown}" \
  "${HOME}/.autodev/tokens/${REPO_OWNER:-unknown}-${REPO_NAME:-unknown}"; do
  if [[ -f "$f" ]]; then TOKEN=$(cat "$f"); break; fi
done
[[ -z "$TOKEN" && -n "${AUTODEV_TOKEN:-}" ]] && TOKEN="$AUTODEV_TOKEN"
[[ -z "$TOKEN" ]] && { echo "ERROR: no Jira token available" >&2; exit 1; }

ESCAPED=$(printf '%s' "$BODY" \
  | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))")

if [[ "${JIRA_AUTH_MODE:-bearer}" == "basic" ]]; then
  AUTH="Basic $(printf '%s:%s' "${JIRA_USER_EMAIL:?JIRA_USER_EMAIL required for basic auth}" "$TOKEN" \
    | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read().rstrip()).decode())')"
else
  AUTH="Bearer $TOKEN"
fi

curl -s --compressed -X POST \
  -H "Authorization: $AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept-Encoding: identity" \
  "https://${JIRA_HOST}/rest/api/2/issue/${ISSUE_KEY}/comment" \
  -d "{\"body\":${ESCAPED}}" >/dev/null

echo "Comment posted to ${ISSUE_KEY}"
