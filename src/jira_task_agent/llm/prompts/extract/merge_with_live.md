# merge_with_live.md — version: v1
# Purpose: decide how to combine the agent's freshly-generated body
# with the live Jira description. Preserves user-mutable state
# (checkmarks, etc.) when the doc-side change is additive/cosmetic;
# yields to the doc-side update when the user genuinely changed the
# DoD or AC sections.

You are merging the agent's new description for a Jira issue with
the issue's current live Jira description. Your output is the final
markdown body the agent will PUT to Jira.

YOU RECEIVE TWO INPUTS:

  - `NEW_BODY`: the agent's freshly-generated description, derived
    from the source-doc bullet that the team just edited. Any new
    content the doc author added is here. This is markdown.
  - `LIVE_JIRA_BODY`: the issue's current Jira description, as it
    sits in Jira right now. The user may have ticked DoD checkboxes
    or otherwise marked the body. This may be markdown or Jira wiki
    syntax (Jira wiki uses `* (/) text` for done, `* (x) text` for
    not-done; markdown uses `- [x]` and `- [ ]`).

THE QUESTION YOU MUST ANSWER:

  For each structured section (Acceptance criteria, Definition of
  Done, Source footer): does NEW_BODY's version genuinely supersede
  LIVE_JIRA_BODY's, or did the source-doc edit only touch the
  CONTEXT paragraph and leave the structured sections effectively
  unchanged?

DECISION RULES:

1. CONTEXT PARAGRAPH (the prose above `### Acceptance criteria`):
   Always use NEW_BODY's version. This is where source-doc edits
   typically land and the agent's regeneration is intended.

2. ACCEPTANCE CRITERIA section:
   - If NEW_BODY's bullets are clearly the SAME items as
     LIVE_JIRA_BODY's (just reworded): use LIVE_JIRA_BODY's bullets
     verbatim (preserve any marks the user added).
   - If NEW_BODY adds, removes, or replaces bullets in a meaningful
     way (different scope, different observable outcomes): use
     NEW_BODY's bullets.
   - When in doubt, prefer LIVE_JIRA_BODY (don't lose user state).

3. DEFINITION OF DONE section — most important:
   - The user may have marked items complete in Jira (`(/)` in wiki
     or `[x]` in markdown). These marks reflect actual work done
     and MUST NOT be silently dropped.
   - If LIVE_JIRA_BODY's DoD items are present (possibly reworded)
     in NEW_BODY: use LIVE_JIRA_BODY's DoD verbatim — same items,
     same checkmark state.
   - If NEW_BODY's DoD has items that don't appear in LIVE_JIRA_BODY,
     keep LIVE_JIRA_BODY's existing items (with their marks) AND
     append the new items as `[ ]` (unchecked).
   - If NEW_BODY genuinely REMOVES some DoD items (the source change
     deleted that requirement): drop them. But this is the rare
     case — only do it when NEW_BODY clearly omits them on purpose,
     not when the LLM forgot them.

4. SOURCE FOOTER (`### Source` block + `<!-- managed-by:jira-task-agent v1 -->`):
   Use NEW_BODY's version (the doc + last-editor metadata may have
   shifted).

OUTPUT FORMAT:

  Return a SINGLE markdown body. Use markdown checkbox syntax in
  the DoD: `- [x]` for checked, `- [ ]` for unchecked. The wrapper
  converts to Jira wiki at write time — do NOT output wiki syntax.

  When you carry over a checked LIVE bullet, write it as `- [x]
  <text>`. The text should be exactly what NEW_BODY's matching item
  says (so the user sees consistent wording going forward), with
  the LIVE checkmark state.

  No JSON. No fenced code block around the output. No commentary
  before or after. Just the markdown body, ending with the marker
  line `<!-- managed-by:jira-task-agent v1 -->`.

INPUTS:

NEW_BODY:
---
{new_body}
---

LIVE_JIRA_BODY:
---
{live_jira_body}
---

Output the final merged markdown body now.
