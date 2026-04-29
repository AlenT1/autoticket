#!/usr/bin/env bash
# provider-jira.sh — Jira task provider (Server/DC bearer or Cloud basic auth)

_jira_require_host() {
  if [[ -z "${JIRA_HOST:-}" ]]; then
    echo "ERROR: JIRA_HOST must be set in .autodev/config (e.g. jira.yourcompany.com)" >&2
    return 1
  fi
  local host="$JIRA_HOST"
  host="${host#http://}"
  host="${host#https://}"
  host="${host%/}"
  JIRA_API_BASE="https://${host}/rest/api/2"
}

_load_task_token() {
  local token=""
  local role_file="${HOME}/.autodev/tokens/task-jira-${REPO_NAME:-unknown}"
  local legacy_file="${HOME}/.autodev/tokens/${REPO_OWNER:-unknown}-${REPO_NAME:-unknown}"
  if [[ -f "$role_file" ]]; then
    token=$(cat "$role_file")
  elif [[ -f "$legacy_file" ]]; then
    token=$(cat "$legacy_file")
  elif [[ -n "${AUTODEV_TOKEN:-}" ]]; then
    token="$AUTODEV_TOKEN"
  fi
  if [[ -z "$token" ]]; then
    echo "ERROR: no Jira token found at $role_file or $legacy_file, and \$AUTODEV_TOKEN is unset" >&2
    return 1
  fi
  printf '%s' "$token"
}

_jira_api() {
  local method="$1" endpoint="$2"; shift 2
  _jira_require_host || return 1
  local token
  token=$(_load_task_token) || return 1

  if [[ "${JIRA_AUTH_MODE:-bearer}" == "basic" ]]; then
    if [[ -z "${JIRA_USER_EMAIL:-}" ]]; then
      echo "ERROR: JIRA_AUTH_MODE=basic requires JIRA_USER_EMAIL in .autodev/config" >&2
      return 1
    fi
    local auth
    auth=$(printf '%s:%s' "$JIRA_USER_EMAIL" "$token" \
      | python3 -c "import base64,sys; print(base64.b64encode(sys.stdin.buffer.read().rstrip()).decode())")
    curl -s --compressed -X "$method" \
      -H "Authorization: Basic $auth" \
      -H "Content-Type: application/json" \
      -H "Accept-Encoding: identity" \
      "$@" \
      "${JIRA_API_BASE}${endpoint}"
  else
    curl -s --compressed -X "$method" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -H "Accept-Encoding: identity" \
      "$@" \
      "${JIRA_API_BASE}${endpoint}"
  fi
}

_issue_key() {
  local input="$1"
  if [[ "$input" =~ ^[0-9]+$ ]]; then
    echo "${REPO_NAME}-${input}"
  else
    echo "$input"
  fi
}

_jira_transition_id_for_status() {
  local issue_key="$1" target="$2"
  AUTODEV_TARGET_STATUS="$target" _jira_api GET "/issue/${issue_key}/transitions" \
    | AUTODEV_TARGET_STATUS="$target" python3 -c "
import json, sys, os
target = os.environ.get('AUTODEV_TARGET_STATUS', '')
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for t in data.get('transitions', []):
    if t.get('to', {}).get('name') == target:
        print(t.get('id', ''))
        break
" 2>/dev/null || true
}

# Returns JSON shaped like {title, body, milestone, labels} for process.sh.
provider_issue_view() {
  local issue_key
  issue_key=$(_issue_key "$1")
  _jira_api GET "/issue/${issue_key}" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('{}')
    sys.exit(0)
f = d.get('fields', {}) or {}
out = {
    'title': f.get('summary', 'unknown'),
    'body': f.get('description') or '',
    'milestone': {'title': ((f.get('fixVersions') or [{}])[0] or {}).get('name', 'none')},
    'labels': [{'name': l} for l in (f.get('labels') or [])],
}
print(json.dumps(out))
"
}

provider_issue_edit() {
  local issue_key
  issue_key=$(_issue_key "$1"); shift
  while [[ $# -gt 0 ]]; do
    case "${1:-}" in
      --add-label)
        local label="$2"
        local updated
        updated=$(_jira_api GET "/issue/${issue_key}" \
          | AUTODEV_LABEL="$label" python3 -c "
import json, sys, os
label = os.environ.get('AUTODEV_LABEL', '')
try:
    d = json.load(sys.stdin)
except Exception:
    print('[]'); sys.exit(0)
labels = (d.get('fields') or {}).get('labels') or []
if label and label not in labels:
    labels.append(label)
print(json.dumps(labels))
")
        _jira_api PUT "/issue/${issue_key}" \
          -d "{\"fields\":{\"labels\":${updated}}}" >/dev/null || true
        shift 2 ;;
      --remove-label)
        local label="$2"
        local updated
        updated=$(_jira_api GET "/issue/${issue_key}" \
          | AUTODEV_LABEL="$label" python3 -c "
import json, sys, os
label = os.environ.get('AUTODEV_LABEL', '')
try:
    d = json.load(sys.stdin)
except Exception:
    print('[]'); sys.exit(0)
labels = (d.get('fields') or {}).get('labels') or []
print(json.dumps([l for l in labels if l != label]))
")
        _jira_api PUT "/issue/${issue_key}" \
          -d "{\"fields\":{\"labels\":${updated}}}" >/dev/null || true
        shift 2 ;;
      *)
        shift ;;
    esac
  done
}

provider_issue_comment() {
  local issue_key
  issue_key=$(_issue_key "$1")
  local body="$2"
  local escaped
  escaped=$(printf '%s' "$body" \
    | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))")
  local resp
  resp=$(_jira_api POST "/issue/${issue_key}/comment" -d "{\"body\":${escaped}}" 2>&1)
  if echo "$resp" | jq -e '.id' >/dev/null 2>&1; then
    return 0
  fi
  echo "WARN: Jira comment on ${issue_key} failed:" >&2
  echo "$resp" | head -c 300 | sed 's/^/  /' >&2
  echo "" >&2
  return 1
}

provider_issue_close() {
  local issue_key
  issue_key=$(_issue_key "$1")
  local tid
  tid=$(_jira_transition_id_for_status "$issue_key" "${DONE_STATUS:-Done}")
  if [[ -z "$tid" ]]; then
    echo "WARN: no transition to '${DONE_STATUS:-Done}' from current status of ${issue_key}" >&2
    return 0
  fi
  _jira_api POST "/issue/${issue_key}/transitions" \
    -d "{\"transition\":{\"id\":\"${tid}\"}}" >/dev/null || true
}

provider_issue_list_labeled() {
  local label="$1"
  local jql="project=${REPO_NAME} AND labels='${label}' ORDER BY created ASC"
  _jira_api GET "/search" -G \
    --data-urlencode "jql=${jql}" \
    --data-urlencode "maxResults=1" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
issues = d.get('issues') or []
if issues:
    print(issues[0].get('key', ''))
"
}

provider_issue_url() {
  local issue_key host
  issue_key=$(_issue_key "$1")
  host="${JIRA_HOST:-}"
  host="${host#http://}"
  host="${host#https://}"
  host="${host%/}"
  echo "https://${host}/browse/${issue_key}"
}

provider_label_create() { return 0; }

provider_board_init() {
  local resp
  resp=$(_jira_api GET "/project/${REPO_NAME}" 2>/dev/null || true)
  if [[ -z "$resp" ]] || echo "$resp" | grep -qi 'errorMessages'; then
    echo "WARN: Jira project '${REPO_NAME}' not found or not accessible" >&2
    return 1
  fi
  return 0
}

provider_board_set_status() {
  local issue_key
  issue_key=$(_issue_key "$1")
  local target="$2"
  local tid
  tid=$(_jira_transition_id_for_status "$issue_key" "$target")
  if [[ -z "$tid" ]]; then
    echo "WARN: no Jira transition to '${target}' from current status of ${issue_key}" >&2
    return 0
  fi
  _jira_api POST "/issue/${issue_key}/transitions" \
    -d "{\"transition\":{\"id\":\"${tid}\"}}" >/dev/null || true
}

provider_board_find_ready() {
  local filter="${READY_JQL_FILTER-assignee=currentUser() AND}"
  local jql="project=${REPO_NAME} AND status='${READY_STATUS:-Ready}'"
  [[ -n "$filter" ]] && jql="${jql} AND ${filter}"
  jql="${jql} ORDER BY created ASC"
  _jira_api GET "/search" -G \
    --data-urlencode "jql=${jql}" \
    --data-urlencode "maxResults=1" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
issues = d.get('issues') or []
if issues:
    print(issues[0].get('key', ''))
"
}

provider_rate_limit_remaining() { echo 1000; }
provider_rate_limit_reset()     { echo 0; }
provider_branch_prefix() { echo "autodev/"; }


provider_info() {
  cat <<EOF
Task provider: Jira (${JIRA_HOST})
Project key:   ${REPO_NAME}
Auth mode:     ${JIRA_AUTH_MODE:-bearer}
Ready status:  ${READY_STATUS:-Ready}
In progress:   ${IN_PROGRESS_STATUS:-In progress}
In review:     ${IN_REVIEW_STATUS:-In review}
Done:          ${DONE_STATUS:-Done}

Note: Jira handles ISSUE TRACKING only.
      Code operations (branches, PRs/MRs) come from CODE_PROVIDER.
EOF
}
