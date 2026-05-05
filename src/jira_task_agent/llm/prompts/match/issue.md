# matcher_grouped.txt — version: v1
# Purpose: match items vs candidates across MULTIPLE INDEPENDENT GROUPS
# in one LLM call. Used for Stage-2 task matching where each group is
# (epic's extracted tasks) vs (epic's existing children).

You are matching items against existing Jira issues. The work is
divided into MULTIPLE INDEPENDENT GROUPS. Each group is a separate
matching problem: items from group A only consider candidates from
group A. Items from one group MUST NOT be matched against candidates
from a different group.

Item type for this call: {kind}     # "epic" or "task"

For each item in each group, decide whether one of that group's
candidates is the SAME work item — different surface wording is fine,
what matters is intent and scope — or whether none match (the item is
new and will be created).

Output format (strict JSON; no markdown, no prose):

{
  "groups": [
    {
      "group_id": "<exact group_id from the input>",
      "matches": [
        {
          "item_index": <int>,                  # 0-based, into THIS group's items
          "candidate_key": "<JIRA-KEY>" | null, # match within this group, or null
          "confidence": <float 0..1>,
          "reason": "<one short sentence>"
        }
      ]
    }
  ]
}

HARD RULES:

A. PER-GROUP COMPLETENESS
   - Every input group MUST appear in `groups`, with the same group_id.
   - Within a group, every item MUST appear in `matches` exactly once.

B. SCOPING (CRITICAL)
   - An item in group A may ONLY match a candidate that's listed in
     group A's candidates. Cross-group matching is forbidden.
   - Within a group, a candidate may be cited MORE THAN ONCE if it is a
     "rollup" issue — i.e. its description describes (in bullets, a
     scope list, or "Implemented scope:" / "Includes:" sections) work
     that covers multiple extracted items. In that case, every covered
     item should cite the same candidate_key. Examples:
       - Candidate "Migrate frontend to Next.js" with description listing
         "Added App Router layout / Replaced Vite serving / API rewrites
         …" matches all the doc's per-step migration tasks.
       - Candidate "Production Hardening" with bullets covering JWT,
         CORS, rate limiting, etc. matches all those individual tasks.
     For non-rollup candidates (regular 1:1 issues), keep the usual
     "at most once" behavior.

C. CONFIDENCE FLOOR
   - If confidence < 0.70 → return null for `candidate_key`. Do not
     guess. A missed match just creates a new ticket; a wrong match
     silently merges unrelated work.

D. SEMANTIC MATCHING
   - Compare INTENT + SCOPE, not surface wording.
     - "Generate JWT secret and store in Vault" matches "JWT secret"
       when both describe the same workstream item.
     - "Add Kubernetes liveness and readiness probes in Helm" matches
       "K8s probes".
     - "Validate prod LLM models and rate limits" does NOT match
       "LLM observability" — different scope.

E. DOMAIN HINT (kind={kind})
   - For "task": candidates are pre-filtered to children of one parent
     epic per group; you don't have to consider scope drift across
     epics.
   - For "epic": rare for this prompt — Stage 1 epic-matching uses the
     flat matcher.txt prompt.

GROUPS (input):
{groups_json}

Return the JSON now.
