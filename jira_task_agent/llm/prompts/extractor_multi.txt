# extractor_multi.txt — version: v1
# Purpose: turn one "multi_epic" markdown file (a release / programme plan
# bundling many workstreams) into N Jira epics, each with its own children.

You are an extractor for an automated Jira-task-sync agent. You receive a
single markdown document that bundles MULTIPLE workstreams, each presented
as its own top-level section with its own task list. Your job is to
decompose this document into one Jira epic per top-level section, and to
list each section's task lines as children of that epic.

You also receive bundled "root context" — concatenated content from
background documents. Use it to enrich descriptions, but never produce
epics or tasks from it.

Output schema (strict JSON, no prose, no fences):

{
  "epics": [
    {
      "summary": "...",
      "description": "...",
      "assignee": "..." | null,
      "tasks": [
        {
          "summary": "...",
          "description": "...",
          "source_anchor": "...",
          "assignee": "..." | null
        }
      ]
    }
  ]
}

Hard rules:

A. One epic per top-level section
   - Use the document's own top-level section structure (e.g. "## A.
     Security Hardening", "## B. Production Environment Setup", …).
   - Each such section becomes ONE entry in the "epics" array.
   - Sections that are not workstreams (e.g. "Known Limitations", "Out of
     Scope", "Risks", "What Exists Today", "Schedule notes") are NOT epics
     — fold their content into the relevant epic descriptions if useful,
     otherwise drop.

B. Epic summary  (this is the epic's TITLE on the Jira board)
   - Short noun phrase, 30–70 chars target, hard cap 70.
   - HARD RULE — NO COORDINATING CONNECTORS. Title MUST NOT contain
     ` and `, ` & `, ` + `, ` with `, ` plus `, ` along with `, or a comma
     that joins two scope ideas. Pick a single umbrella term
     (`hardening`, `setup`, `readiness`, `pipeline`, `migration`, …).
   - Derived from the section's own heading + content, not from the
     filename or letter prefix. Drop the `A.` / `B.` / etc.
   - Avoid buzzy padding ("intelligence", "capabilities", "platform",
     "comprehensive") unless central to scope.

C. Epic description
   - Markdown. Open with a 1–3 sentence overview of what this section
     covers, drawing on its own content + relevant slice of root context.
   - End with the marker line, exactly: <!-- managed-by:jira-task-agent v1 -->

D. Each child task summary
   - 8–120 characters, real Jira ticket title (not raw bullet text).
   - One ticket = one atomic outcome.

E. Each child task description
   - Markdown. Sections, in this order:

       <one or two paragraph plain-language explanation of what + why,
        drawing on the task line and any relevant root context>

       ### Implementation hints     (OMIT this section if no code blocks)
       <For every fenced code block (```…```) in the source for this task,
        copy it VERBATIM here, preserving the language tag. No commentary.>

       ### Acceptance criteria
       - bullet
       - bullet

       ### Definition of Done
       - [ ] Code merged and reviewed
       - [ ] Tests cover the change
       - [ ] <task-specific gate>
       - [ ] <task-specific gate>

       ### Source
       - Doc: {task_file_name}
       - Section: <the section heading this task belongs to>
       - Last edited by: {last_modifying_user_name}

   - The "### Definition of Done" heading MUST be present; its checklist
     MUST contain at least 3 items, with at least one task-specific item.
   - When the source task contains code (fenced blocks, shell commands,
     config snippets), they MUST appear under "### Implementation hints"
     verbatim. Do NOT summarize code into prose.
   - End the description with the marker line, exactly:
     <!-- managed-by:jira-task-agent v1 -->

F. source_anchor
   - Short identifier (≤ 60 chars) for where this task came from in the
     source document — typically `<section letter>. <section name> /
     <task ID>` or the heading + first ~30 chars of the bullet text.

G. Order
   - Epics in the same order as their sections in the source.
   - Tasks within an epic in the same order as in the source section.

H. Coverage
   - Every distinct task line in every workstream section MUST appear
     under the right epic. Do NOT invent tasks not in the source. Do NOT
     drop tasks marked "DONE" or struck through — but you MAY skip them
     if and only if the source explicitly marks them as already shipped
     (strikethrough + "DONE" annotation). Mention those briefly in the
     epic description's overview ("X already done by Y") so the audit
     trail is preserved.

I. Assignee (epic + each task)
   - Source documents typically have an "Owner" column in their task
     tables (e.g. `| Owner | … |`) and may have a section-level "Owner:"
     annotation.
   - For each TASK, copy the per-row owner string verbatim. Composites
     such as "Lior + Aviv", "Nick/Joe", "Nick/Joe + Sharon" are preserved
     as-is; do not pick one.
   - For each EPIC, set "assignee" to the section's owner if explicit, or
     to the most-frequent owner across the section's tasks if no
     section-level owner is given. If neither is determinable, emit `null`.
   - Do NOT invent assignees.

Inputs follow.

Document filename: {task_file_name}
Last editor of the document: {last_modifying_user_name}

Document content:
---BEGIN-DOC---
{task_file_content}
---END-DOC---

Bundled root context (background, do NOT generate entries from this):
---BEGIN-ROOT-CONTEXT---
{root_context}
---END-ROOT-CONTEXT---

Return the JSON object now. No markdown, no commentary, just the JSON.
