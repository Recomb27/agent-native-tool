# filter-sequences — Agent Native v2

Apply explicit DNA length/alphabet/internal-stop QC. This package includes its assigned scientific computation and an
explicit, read-only public-state decision review. It never changes campaign state.

## Start with current visible sequences

Run from the workspace root; no hand-built sequence/window JSON is needed:

```bash
python3 owned_skills/filter-sequences/source/run.py --mode visible --input state.json --output skill_analysis/filter-sequences/initial.json --run-root .
```

Only currently visible seed DNA and visible library windows enter the calculation.
Inputs, the full scientific output and each decision boundary are pinned under
`.agent_native_evidence/filter-sequences/`; a later alias cannot erase those versions.

Read these two compact objects first, rather than dumping the full artifact:

```bash
python3 - <<'PY'
import json
p=json.load(open('skill_analysis/filter-sequences/initial.json'))
print(json.dumps({k:p[k] for k in ['scientific_brief','task_support']},indent=2))
PY
```

The scientific brief binds exact input IDs, lengths and hashes to the verified
calculation. It supports retained/rejected identifiers under declared predicates and source identity. It does not establish
retained means active or profitable; QC flags are measured functional outcomes.

## Revisit a real decision after new actions

Before a costly confirmation, another batch, switching windows, or stopping,
refresh the public-state review if its evidence would affect that decision:

```bash
python3 owned_skills/filter-sequences/source/run.py --mode review --input skill_analysis/filter-sequences/initial.json --output skill_analysis/filter-sequences/review.json --run-root .
```

The prior scientific calculation is verified against its immutable bytes, not
pretended to cover new sequences. If `visible_backgrounds_requiring_fresh_scientific_analysis`
is nonempty, rerun visible mode for the new objects. Identical DNA can expose
different library options: retain the actual visible action choices.

Filter before biological interpretation. Reuse the exact retained source objects, then assess current sampling and cost evidence separately.

The review reads only current `state.json`, `history.json`, `available_actions.json`
and the public `agent_playbook.md`, plus this tool's saved artifacts. It provides:

- Current stop cash, full confirmation cost and the public reward break-even.
- Pending candidate net-reward bounds from the declared screening-noise clip.
  These are bounds, not calibrated probabilities or confidence intervals.
- Each legal batch's mine-plus-confirm cost and the activity needed to repay it
  **if discovery were certain**. Actual discovery probability remains unknown.
- Observed batch novelty and no-new-pending history, with explicit missing-action
  gaps; spending since a known improvement is a sunk outcome, not a stop rule.

`leaves_confirm_capacity=true` means affordable, not profitable. A past hit or a
valid sequence does not prove another batch has positive expected gain. Compare
stop, confirmation, another visible window and another batch using actual new
evidence and costs. Do not spend remaining money merely because it is available;
do not stop automatically at the first improvement or any fixed activity value.

## Independent scientific inputs and intervention

The legacy CLI remains valid:

```bash
python3 owned_skills/filter-sequences/source/run.py --input source_inputs.json --rows rows_to_audit.json --output skill_analysis/filter-sequences/custom.json --run-root .
```

Copy the complete `EXAMPLES.json.scientific_input` object as a minimal fixture and
use `EXAMPLES.json.rows` as an empty rows list. `INPUT_SCHEMA.json` documents the
minimal shapes; optional context is checked by the preserved scientific core.
The standard example uses synthetic coding DNA and is not activity evidence.

For custom predicates, copy the complete `EXAMPLES.json.explicit_input.filter_predicates` object. The required nested keys are `length_nt: {operator, expected}`, `alphabet: {operator, allowed_symbols, case_sensitive}`, and `internal_stop_codons: {frame, exclude_terminal_codon, max_count}`. Omit the entire optional object for defaults; a partial object is an error.

All output writes are atomic and keep an immutable version. Invalid inputs exit
2 with a JSON repair message; an existing output alias is not a success for a
failed invocation. Preserve the immutable result path printed on success. Cite
its specific facts and uncertainty in an action reason only when they mattered.
The tool provides no historical best-round labels, hidden activities, fixed
branch route, or future-success probability. Scientific use and final cash or
activity improvements must be evaluated from the real campaign trajectories.
