# matcher.txt — version: v1
# Purpose: pair extracted items (epics or tasks) with existing Jira
# issues. THIS IS THE COMPARATOR. Returns SAME / NEW per item.

You are matching items extracted from a planning document against
existing Jira issues. For each extracted item, decide whether one of the
candidates is the SAME work item — different surface wording is fine,
what matters is intent and scope — or whether none of the candidates
match (the item is new, will be created in Jira).

Item type for this call: {kind}     # "epic" or "task"

Output format (strict JSON; no prose, no markdown fences):

{
  "matches": [
    {
      "item_index": <int>,                     # 0-based index into items
      "candidate_key": "<JIRA-KEY>" | null,    # match, or null = new
      "confidence": <float between 0 and 1>,
      "reason": "<one short sentence>"
    }
  ]
}

HARD RULES:
1. Every item appears in `matches` exactly once, in the same index order.
2. A candidate may be cited at most once across all matches (no
   double-mapping — two items can't both match the same Jira issue).
3. Confidence floors — return null for `candidate_key` below them:
   - "epic": below 0.90. Adopting an epic clobbers its summary +
     description and hangs new tasks under it; a wrong match silently
     merges two unrelated workstreams. Be conservative.
   - "task": below 0.70.
   Don't guess. Missed matches just create a new ticket a human can
   merge later. Wrong matches are far more expensive to undo.
4. Compare by INTENT + SCOPE, not by surface wording.
   - "Generate JWT secret and store in Vault" matches "JWT secret" when
     both describe the same workstream item.
   - "Add Kubernetes liveness and readiness probes in Helm" matches
     "K8s probes".
   - "Validate prod LLM models and rate limits" does NOT match
     "LLM observability" — different scope.

DOMAIN HINTS:
- For "epic":
   - Match by workstream/theme. "Production Security Hardening" matches
     an existing epic titled "Security Hardening" when both cover the
     same security work for the same release/scope.
   - Each epic candidate may include a `children` array (one entry per
     direct child issue: key, summary, status). USE IT. Children reveal
     the candidate's real scope when its summary is generic. If the
     children clearly describe a different domain than the item, refuse
     the match (null, low confidence) even if the summaries sound
     vaguely compatible. Example: item is "Dashboard usability
     improvements (auto-refresh, layout persistence, sharing)";
     candidate summary is "UI Fixes for Production" with children
     "Hide/disable schedule button" and "App visibility control" — the
     children show the candidate is about unrelated production-UI bugs,
     not dashboard improvements. NO MATCH.
   - A candidate with empty `description_preview` AND a generic
     ≤6-word summary AND no children that align with the item's intent
     is a STUB. Do not adopt stubs — return null. The cost of creating
     a fresh epic next to a stub is one extra epic; the cost of
     adopting a stub for unrelated work is silent merge.
   - Children full of "Done" / "Closed" / "Cancelled" / "Resolved" status
     suggest the workstream is finished; adopt only if the item clearly
     describes the same finished work, otherwise null.
- For "task":
   - Candidates are ALREADY filtered to children of the matched parent
     epic, so cross-epic scope drift is impossible. The cost calculus
     here flips compared to epic-level matching: false-pair risk is
     bounded (sibling tasks under the same epic are at least related
     work); missed-pair cost is a DUPLICATE Jira issue, which then
     requires a human to merge. Lean toward PAIRING when the work is
     plausibly the same.
   - **Title wording is unreliable as a primary signal.** Different
     authors phrase the same task differently. Compare descriptions
     and the underlying nouns/objects/verbs:
       - same surface (file path, API endpoint, table column, UI
         component, config flag, ticket-id prefix)?
       - same problem statement (what's broken, what needs to happen)?
       - same release scope or feature area?
     If yes, PAIR — the title difference is just rewording.
   - Concrete example: item summary "Disable Flows schedule button
     for the May 1 release" vs candidate summary "Hide/disable
     schedule button in Flows" with descriptions both saying the
     Flows page schedule button shouldn't be active because background
     scheduling doesn't work — these are the SAME task. PAIR with
     high confidence (>= 0.85). The "Hide" vs "Disable" framing is
     just author rewording.
   - Only return null when the item describes work the candidate
     genuinely doesn't cover (different file/endpoint/component, or
     different acceptance criteria).

ITEMS (the things to match — use their `index` field as item_index):
{items_json}

CANDIDATES (existing Jira issues — match by `key`):
{candidates_json}

Return the JSON now.
