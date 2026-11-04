# Frozen manuscript data

These files are a public projection of the V13.3 manuscript's derived evidence. Numerical values and source hashes are retained. Machine-local run paths become non-resolving archive:// provenance identifiers. They are not download links.

| File | Rows / objects | Meaning |
| --- | ---: | --- |
| paired_endpoints.csv / .json | 120 pairs | Original, Adapted and shared Original-control endpoints for each tool/repeat |
| campaign_usage_and_strategy.csv | 240 campaigns | Invocation, trace linkage, terminal outcome and stopping summaries |
| accepted_state_points.csv | 6,265 rows | Initial and accepted post-action states for the 240 tool campaigns |
| daily_values.csv | 43,440 rows | 240 tool campaigns × days 0–180, with last-state carry-forward |
| original_no_tool_daily.csv | 1,810 rows | Ten shared references × days 0–180 |
| six_dimension_scores.csv | 144 scores | Twelve tools × two arms × six ordinal dimensions |
| six_dimension_deltas.csv | 72 rows | Adapted-minus-Original score changes |
| six_dimension_matrix.csv | 12 rows | Wide tool-wise score table |
| score_summary.json | One summary | Dimension means, direction counts, assessment scope and rater agreement |
| endpoint_effects.csv | 60 rows | Per-tool cash, activity, reward, cost and days differences and intervals |
| endpoints.json | One summary | Frozen endpoint means, paired contrasts, intervals and population scope |
| usage_coverage.csv | 24 rows | Tool/arm invocation, trace linkage and baseline-improvement counts |
| case_sequence_properties_r08.json | One pair | Retrospectively selected scientific-use example for Figure 3 |
| tool_source_changes.csv | 96 file records | Original and Adapted source hashes and change classifications |
| two_arm_input_and_client_comparability.json | One audit | Matching biological inputs/settings and client comparability |

## Units and definitions

Cash, costs and rewards are simulator budget units, not model-service prices. Activity is confirmed historical activity normalized to TadA8e = 1. Days are simulated experimental days. Agent seconds are host/runtime accounting and are not simulated days.

In paired_endpoints, original__*, adapted__* and original_control__* identify the three populations. Every repeat's control values are repeated for twelve tools; only ten distinct references exist. Profit is terminal cash minus 100,000. The progress field is an inherited auxiliary metric and is not a primary manuscript endpoint.

A trace_reference is a later accepted-action rationale linking a path or filename after a call completed. It establishes trace linkage, not scientific validity. Boolean values are recorded as True/False.

Six-dimensional scores are ordinal integers in [0,10]. The public score table intentionally omits private-language review prose; all 144 numerical decisions are preserved. The English rubric and actual evidence scope are described in docs/evaluation.md.

## Provenance and limits

All files derive from the frozen V13.3 source archive, SHA-256 2bbb978d1483270de258601ac180fd08137f8930f22acf24c00ffaa6fec74311. Original and public per-file hashes are recorded in provenance/sources.json.

These tables can reconstruct the manuscript's statistics and figures without private local paths. They are not raw model logs, a substitute for scientific adjudication, or the full million-sequence biological dataset.

