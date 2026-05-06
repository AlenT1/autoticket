"""Lock the user-state preservation contract.

`preserve_dod_checkboxes(new_body, live_body)` must transfer `[x]` /
`(x)` ticks from the live Jira description into the agent's new
markdown body, surviving:
  - format mismatch (live in Jira-wiki, new in markdown)
  - paraphrase of the bullet wording
  - DoD bullets renamed / reordered

Without ever flipping a `[ ]` to `[x]` outside the DoD section.
"""
from __future__ import annotations

from jira_task_agent.pipeline.preserve_state import preserve_dod_checkboxes


_NEW_TASK_BODY = """\
Task context paragraph that explains the change.

### Acceptance criteria
- The Flows page no longer renders the schedule button.
- Ad-hoc flow execution still works.

### Definition of Done
- [ ] Frontend PR merged to release branch
- [ ] Manual smoke on staging confirmed
- [ ] Release notes updated
- [ ] Tech-lead sign-off

### Source
- Doc: example.md
- Last edited by: Saar Riftin

<!-- managed-by:jira-task-agent v1 -->
"""


def test_no_live_body_returns_unchanged():
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, "")
    assert out == _NEW_TASK_BODY


def test_live_with_no_dod_returns_unchanged():
    live = "Some description without any DoD section."
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, live)
    assert out == _NEW_TASK_BODY


def test_live_dod_with_no_checkmarks_returns_unchanged():
    live = """\
Body.

### Definition of Done
- [ ] Item one
- [ ] Item two
"""
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, live)
    assert out == _NEW_TASK_BODY


def test_markdown_live_check_transfers_to_new_body():
    live = """\
Body.

### Definition of Done
- [x] Frontend PR merged to release branch
- [ ] Manual smoke on staging confirmed
- [x] Release notes updated
- [ ] Tech-lead sign-off
"""
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, live)
    assert "- [x] Frontend PR merged to release branch" in out
    assert "- [x] Release notes updated" in out
    assert "- [ ] Manual smoke on staging confirmed" in out
    assert "- [ ] Tech-lead sign-off" in out


def test_jira_wiki_live_check_transfers_to_md_body():
    """Jira's MD->wiki converter writes `[x]` as `(/)` (green tick)
    and `[ ]` as `(x)` (red X). So `(/)` is what we treat as checked
    on the live side; a wiki `(x)` is the agent's untouched
    'unchecked' state and must NOT be promoted to `[x]`."""
    live = """\
Body.

h3. Definition of Done
* (/) Frontend PR merged to release branch
* (x) Manual smoke on staging confirmed
* (/) Release notes updated
* (x) Tech-lead sign-off
"""
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, live)
    assert "- [x] Frontend PR merged to release branch" in out
    assert "- [x] Release notes updated" in out
    assert "- [ ] Manual smoke on staging confirmed" in out
    assert "- [ ] Tech-lead sign-off" in out


def test_jira_wiki_red_x_is_not_checked():
    """A wiki `(x)` is the agent's [ ] rendered as a red X — must NOT
    flip the new MD body's [ ] to [x]."""
    live = """\
h3. Definition of Done
* (x) Frontend PR merged to release branch
"""
    out = preserve_dod_checkboxes(_NEW_TASK_BODY, live)
    assert "- [x] Frontend PR merged to release branch" not in out
    assert "- [ ] Frontend PR merged to release branch" in out


def test_paraphrased_bullet_still_matches():
    """Live: 'Frontend PR merged'. New: 'Frontend PR merged to release branch'.
    First-5-words of both normalize to 'frontend pr merged to release' — match."""
    live = """\
### Definition of Done
- [x] Frontend PR merged to release branch
"""
    new = """\
### Definition of Done
- [ ] Frontend PR merged to release branch tomorrow afternoon
- [ ] Other thing
"""
    out = preserve_dod_checkboxes(new, live)
    assert "- [x] Frontend PR merged to release branch tomorrow afternoon" in out
    assert "- [ ] Other thing" in out


def test_unrelated_bullet_stays_unchecked():
    live = """\
### Definition of Done
- [x] Frontend PR merged
"""
    new = """\
### Definition of Done
- [ ] Backend service deployed
- [ ] Database migration applied
"""
    out = preserve_dod_checkboxes(new, live)
    # Neither matches "Frontend PR merged" → both stay unchecked.
    assert out.count("- [x]") == 0
    assert out.count("- [ ]") == 2


def test_only_dod_section_is_modified():
    """A `[ ]` bullet outside the DoD heading must NOT be flipped, even
    if its text matches a checked DoD item."""
    live = """\
### Definition of Done
- [x] Frontend PR merged to release branch
"""
    new = """\
### Acceptance criteria
- [ ] Frontend PR merged to release branch

### Definition of Done
- [ ] Frontend PR merged to release branch
- [ ] Other gate
"""
    out = preserve_dod_checkboxes(new, live)
    ac_section, dod_section = out.split("### Definition of Done", 1)
    # AC bullet (above the DoD heading) stays unchecked — defensive: the
    # tool only ever modifies inside the DoD block.
    assert "- [ ] Frontend PR merged to release branch" in ac_section
    assert "- [x] Frontend PR merged to release branch" in dod_section


def test_already_checked_in_new_body_stays_checked():
    live = """\
### Definition of Done
- [x] Frontend PR merged to release branch
"""
    new = """\
### Definition of Done
- [x] Frontend PR merged to release branch
"""
    out = preserve_dod_checkboxes(new, live)
    assert out == new  # nothing to change


def test_handles_multiple_paragraphs_and_trailing_section():
    """Real Jira body shape: AC + DoD + Source footer with a heading
    after the DoD. Make sure DoD-detection stops at the next heading."""
    live = """\
### Definition of Done
- [x] Frontend PR merged
- [ ] Manual smoke

### Source
- Doc: x
- Last edited by: y
"""
    new = """\
### Definition of Done
- [ ] Frontend PR merged to release branch
- [ ] Manual smoke on staging
- [ ] New gate added later

### Source
- Doc: x
- Last edited by: y
"""
    out = preserve_dod_checkboxes(new, live)
    assert "- [x] Frontend PR merged to release branch" in out
    assert "- [ ] Manual smoke on staging" in out
    assert "- [ ] New gate added later" in out
    # Source footer untouched.
    assert "### Source\n- Doc: x" in out
