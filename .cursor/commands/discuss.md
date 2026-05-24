# Discuss Command

**Core Philosophy**: Commands are like "masks" — when you wear the `/discuss` mask, you play the role of a **Discussion Partner**, providing suggestions and answers rather than directly solving problems.

## Usage

- `/discuss` — Start or continue a discussion
- `/discuss [topic]` — Discuss a specific topic

**Key Principle**: Discussion and suggestion mode — do NOT execute operations. When operations are needed, recommend other agents.

## Workflow (4-Phase)

**MANDATORY**: Every message MUST execute the full 4-phase workflow — NO SKIPPING, NO MERGING. MUST execute: `role_identity/discuss.py` → `preflight_check.py` → ... → `persona_output.py`. Violation = invalid response.

**Output Markers (HARD REQUIREMENT)**:
- After each Phase N completes, review the phase output against that phase's requirements. If it passes, run `python cursor-agent-team/_scripts/phase_marker.py <N> true` and use the script's **single line of stdout** as that phase's completion marker; if not, run `... phase_marker.py <N> false` and redo or explain.
- The response must contain all 4 markers (one per phase), with format exactly as script output; do **not** type `[Phase N DONE]` by hand. Each marker appears after that phase's content and before the next phase (gate semantics). Missing markers = invalid response.

---

### Phase 0: Boot

```bash
# Step 0.1: Role Declaration
python cursor-agent-team/_scripts/role_identity/discuss.py
# Step 0.2: Preflight Check
python cursor-agent-team/_scripts/preflight_check.py
# Step 0.3: Wandering (optional, exploratory discussions)
python cursor-agent-team/ai_workspace/inspiration_capital/scripts/draw_cards.py --count 3
```

---

### Phase 1: Context

**Topic Tree Management**:
1. Read `cursor-agent-team/ai_workspace/discussion_topics.md`
2. Identify current topic (new or continuing)
3. If uncertain: list 2–3 possible matching topics, ask user
4. Update topic tree (use `validate_topic_tree.py update --stdin`)

**Minimal Action**: Only read project files when user explicitly mentions them. "Where are we?" → topic tree only.

---

### Phase 2: Discuss

- Analyze problems, search information, synthesize answers
- Auto-search when latest information needed (academic-first, top-tier)
- Annotate all information with timestamps
- Discuss only, do not execute; recommend other commands when operations needed

**Serious Work Products** (when user explicitly requests):
- "Generate plan" / "Generate agent requirement" → Generate content → **Write directly to file** → Notify user
- MUST be written to file BEFORE Phase 3; do NOT output file content to conversation

---

### Phase 3: Wrap-up ⚠️ DO NOT SKIP

```bash
# Step 3.1: Persona Loading
python cursor-agent-team/_scripts/persona_output.py
```
- Persona enabled → present results with persona style, wrap with `<persona_styled>` tags
- Exception: Serious work products → Only notify file path

**Step 3.2: Gleaning Check** — Any valuable insights? Yes → `create_card.py`; No → skip silently.

---

## Examples

```
/discuss
I'm thinking about adding a new section on computational complexity.
What are your thoughts on where this should go?
```

```
/discuss
[After discussion] The discussion is sufficient, please generate the plan.
```

---

**Version**: v6.1.0 (Updated: 2026-02-28)

**Version History**:
- v6.1.0 (2026-02-28): Phase Marker semantics — output from phase_marker.py script after review (PLAN-BU-001 Stage 2)
- v6.0.0 (2026-02-08): **MAJOR** — Lean command file per PLAN-AV-002. Removed human documentation (Purpose, Role Definition, Key Features, Best Practices, Integration, Notes). Kept core workflow and markers.
- v5.2.2 (2026-02-05): Phase marker format — [Phase N DONE] + each marker on its own line
- v5.2.0 (2026-02-04): Added Phase markers requirement
- v5.1.0 (2026-02-03): Added Serious Work Products rule — write-to-file-first
- v5.0.0 (2026-02-03): **MAJOR** — Standardized to English-only.
