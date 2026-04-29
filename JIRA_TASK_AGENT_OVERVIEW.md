# Doc → Task Tracker Sync — Overview

## Goal

Keep a task tracker in sync with planning documents stored in a shared input
source. New work appearing in the documents becomes new trackable items;
edits propagate as updates with audit comments; unchanged items are left
alone; manual edits in the tracker are not overwritten.

## Flow

```
   Input source (planning documents)
            │
            ▼
   Discover documents changed since last successful run
            │
            ▼
   Classify each document  ──►  task-bearing | context | ignore
            │
            ▼
   For each task-bearing document:
      Extract one parent unit + ordered list of child tasks
      (context documents enrich descriptions but produce no entries)
            │
            ▼
   Reconcile against live tracker state
            │
   ┌────────┼────────┬────────────┐
   ▼        ▼        ▼            ▼
 create  update    no-op       orphan-flag
            │
            ▼
   Post audit comment + tag current owner on every change
            │
            ▼
   Persist last successful run timestamp
```

## How input maps to tracker entries

| Document role | Used for |
|---|---|
| task-bearing  | Generates exactly one **parent unit**. Each task line in the document generates one **child task** under that parent. |
| context       | Read-only background. Content is woven into parent and child descriptions but never produces its own entry. |
| ignore        | Not processed. |

## What goes into each entry

- **Parent summary** — derived from the document's overview, not from the file name.
- **Parent description** — the document's overview plus relevant slices of context documents.
- **Child task summary** — informative rewrite of the task line, not the raw bullet text.
- **Child task description** — comprehensive enough that an owner can break it into subtasks without re-reading the source. Always includes a Definition of Done section.
- **Back-pointer** — hidden link to the source document attached to every entry. This is the only marker the agent leaves on the tracker; no labels or tags are added.

## Update behaviour

On each run, for every task-bearing document:

1. Update entries whose source content changed.
2. Add entries for new task lines under the **same** existing parent.
3. Post a comment on each updated entry tagging the current owner with what changed.
4. Skip entries whose source is unchanged — no needless writes.
5. If a manual edit is detected in the tracker, refuse to overwrite — post a "manual edits detected" comment instead.
6. Existing children with no match in the new extraction are flagged in the run report; never deleted.

## Triggering

Two equal entry points, identical logic and identical configuration:

- **Manual** — operator invokes the agent on demand.
- **Scheduled** — agent runs at a fixed interval.

Configuration is read once from an environment file. CLI flags are reserved for per-run overrides; running with no flags works.

## Persistence

One value persists between runs: the timestamp of the last successful run. The next run uses it to filter the input source by modification time. Identity of agent-managed entries is recovered each run from the back-pointer attached to every entry; no database, no per-entry mapping table.

