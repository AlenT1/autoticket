# Jira Task Agent — Demo

End-to-end agent that mirrors planning docs from a Google Drive folder into
Jira project `CENTPM`: classifies docs, extracts `{epic, tasks}` via LLM,
matches against live Jira via a two-stage LLM matcher, and produces a
dry-run plan or actual writes. All commands run from
`/Users/saar.raynw/Desktop/central/jira_task_agent/`.

## What the agent does

```
Drive folder ─► classify ─► bundle root ─► extract ─┐
                                                    ▼
Jira project ─► fetch_project_tree (1 paginated /search)
                                                    ▼
                two-stage LLM matcher
                  Stage 1: epic match (1 call, all extracted vs all CENTPM)
                  Stage 2: grouped task match (parallel batches of 4)
                                                    ▼
                          reconciler (pure logic)
                                                    ▼
                  ┌──────────────────────────────┐
                  │  dry-run → run_plan.json      │
                  │  --capture → would_send.json  │
                  │  --apply → real Jira writes   │
                  └──────────────────────────────┘
```

## Safety properties (verified)

- **Capture mode** — `--capture PATH` records every intended POST/PUT to a
  JSON file with zero Jira mutations. Reads still go through.
- **No-rename rule on adoption** — `update_epic` keeps the existing Jira
  summary; only the description and child tasks may change. Renaming an
  unrelated epic is structurally impossible.
- **Status guard** — matched epics in `In Staging / In Review / Done /
  Closed / Resolved / Cancelled / Won't Do / Won't Fix` emit
  `skip_completed_epic` and are not touched.
- **Manual-edit guard** — every agent-written description ends with the
  HTML marker `<!-- managed-by:jira-task-agent v1 -->`. Issues whose
  description lacks the marker → `skip_manual_edits`, no overwrite.
- **Identification by remote-link**, never by label. `ai-generated` is a
  content marker only, set automatically on every agent-created issue.
- **Confidence floors** — `epic = 0.90`, `task = 0.70`. Stage-1 epic
  candidates carry their direct children (`key/summary/status`) so the
  matcher can reject generic-summary stubs and Done-only candidates.

---

## Demo 1 — single-epic, fresh creation (V11 Dashboard)

**What it shows:** matcher conservatism. V11 has no real Jira counterpart;
the agent creates a new epic + 7 child tasks instead of falsely adopting
a thinly-related existing epic. Demonstrates Stage 1 epic matching with
children context, confidence floor, and the new-epic + new-tasks path.

```sh
.venv/bin/python -m jira_task_agent run \
    --capture data/would_send_v11.json \
    --only V11_Dashboard_Tasks.md \
    --since 2026-01-01 \
    --no-cache
```

**Expected outcome (~2 min):**

```
classified by role: {'single_epic': 14, 'multi_epic': 2, 'root': 3}
extractions:        ok=1 failed=0
actions by kind:    {'create_epic': 1, 'create_task': 7}
capture: 16 intended write(s) recorded to data/would_send_v11.json
```

Inspect:

```sh
.venv/bin/python -c "
import json
d = json.load(open('data/run_plan.json'))
g = d[0]['groups'][0]
print('EPIC:', g['epic']['kind'], '|', g['epic']['summary'])
for t in g['tasks']:
    print(' TASK:', t['kind'], '|', t['summary'])
"
```

Discussion points:
- Stage 1 epic match — saw 96 CENTPM epics + 204 children (one paginated
  query) and rejected weak matches under the 0.90 floor.
- 16 captured ops = 1 PUT/POST per intended Jira mutation, including
  remote-link back-pointers to the source Drive doc and `ai-generated`
  label on every issue.

---

## Demo 2 — multi-epic, real-world adoption (May1)

**What it shows:** the most complex path. May1 has 9 sub-epics + ~40
tasks; the matcher correctly adopts 6 existing CENTPM epics, creates 1
new sub-epic where there's no good match, correctly skips an in-staging
epic, and detects 28 manual-edit cases (existing children that lack the
agent marker).

```sh
.venv/bin/python -m jira_task_agent run \
    --capture data/would_send_may1.json \
    --only May1_Initial_Version_Tasks.md \
    --since 2026-01-01 \
    --no-cache
```

**Expected outcome (~5 min):**

```
classified by role: {'single_epic': 14, 'multi_epic': 2, 'root': 3}
extractions:        ok=1 failed=0
actions by kind:    {'update_epic': 7, 'create_epic': 1,
                     'skip_completed_epic': 1, 'create_task': 11,
                     'skip_manual_edits': 28, 'orphan': 24}
capture: 66 intended write(s) recorded to data/would_send_may1.json
```

Inspect:

```sh
.venv/bin/python -c "
import json
d = json.load(open('data/run_plan.json'))
for g in d[0]['groups']:
    e = g['epic']
    print(f'{e[\"kind\"]:24s} -> {str(e.get(\"target_key\")):14s} '
          f'conf={e.get(\"match_confidence\")} | {(e.get(\"summary\") or \"\")[:60]}')
"
```

Discussion points:
- **Adoption with no rename** — every `update_epic` action keeps the live
  Jira summary; the `note` field shows `summary kept as '<live>'
  (extractor proposed '<other>', ignored)`.
- **Status guard fires** on V0 NextJS migration → CENTPM-1238 In Staging
  → `skip_completed_epic`, no writes for that group.
- **Manual-edit guard fires** on 28 existing children → `skip_manual_edits`
  (these were created by humans pre-agent and have no marker).
- **Conservative new-epic creation** — Monitoring readiness creates a
  fresh epic instead of adopting CENTPM-1179 because that candidate's
  children are mostly Done and describe a different scope (logging vs.
  liveness/probes).
- **Real overlap detection** — May1's `UI Fixes for Production` correctly
  adopts CENTPM-1235, the same generic-summary epic the matcher
  previously rejected for V11. Children-aware Stage 1 distinguishes the
  two.
