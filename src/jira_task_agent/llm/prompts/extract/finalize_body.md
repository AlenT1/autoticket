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

2. GOAL section (or legacy ACCEPTANCE CRITERIA if the live body uses
   that older heading):
   - This is a 1-3 sentence outcome statement. Use NEW_BODY's
     version — the doc author owns the success criterion.
   - When live still has the old "Acceptance criteria" bullets but
     new has "Goal" prose: switch to the new structure (Goal). The
     old AC is migration cruft.

3. IMPLEMENTATION STEPS section (CHECKBOXES — same preservation
   rules as DoD apply, see rule 4):

   The user may have ticked individual steps as work progresses.
   Apply the SAME bullet-mapping rules as for DoD (rule 4 below)
   to step checkboxes: preserve LIVE's `[x]` marks when steps are
   reworded but logically the same; default to LIVE state when
   unsure.

4. DEFINITION OF DONE section — most important:

   The user may have marked items complete in Jira (`(/)` in wiki
   or `[x]` in markdown). These marks reflect actual work done by a
   human and MUST NOT be silently dropped. NEW_BODY's DoD bullets
   are always emitted as `[ ]` because they came out of an extractor
   that doesn't know about Jira state — the marks live only in
   LIVE_JIRA_BODY.

   YOUR DEFAULT POLICY for DoD: PRESERVE LIVE_JIRA_BODY's DoD
   verbatim. Take NEW_BODY's DoD ONLY when it materially supersedes
   live (different gates, not just reworded). When unsure, default
   to live — losing a checkmark is worse than carrying a slightly
   stale wording.

   Bullet-to-bullet mapping rules:

   a) ONE-TO-ONE (live bullet ≈ new bullet, same gate reworded):
      keep the LIVE bullet's text and mark verbatim.

   b) MANY-TO-ONE (NEW_BODY combined two or more LIVE bullets into
      one item): emit the COMBINED new bullet as `[x]` if ANY of
      the live bullets it covers were `[x]`. Do not collapse if
      that would silently drop a checkmark you can't represent.
      When the user has marked progress on individual items,
      prefer KEEPING the live items separate (under rule a) over
      collapsing.

   c) ONE-TO-MANY (LIVE had one bullet, NEW splits it into multiple
      finer-grained items): if the live bullet was `[x]`, mark ALL
      the split items `[x]` (the user said the work was done; the
      finer breakdown of done-ness should still be done). If live
      was `[ ]`, leave all split items `[ ]`.

   d) NEW-ONLY items (in NEW_BODY but not in LIVE): emit as `[ ]`.

   e) LIVE-ONLY items (in LIVE but not in NEW_BODY): keep them with
      their LIVE state. The agent's extractor sometimes loses items
      it didn't intend to remove.

   When in doubt for ANY bullet: keep the LIVE state.

5. SOURCE FOOTER (`### Source` block + `<!-- managed-by:jira-task-agent v1 -->`):
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
