# Tool trajectory records

A scientific tool's immutable result evidence and a campaign recorder provide complementary provenance. The campaign recorder records each actual exposed tool invocation in its own directory.

~~~text
agent_workspace/
├── tool_calls.jsonl
└── tool_trajectories/<tool_id>/call-*/
    ├── manifest.json
    ├── stdout.log
    └── stderr.log
results/
├── tool_trajectory_index.json
└── tool_trajectory_index.jsonl
~~~

A call manifest records tool ID, sequence number, UTC and elapsed times, command arguments, exit code, input/output paths and hashes. It may link surrounding agent events and the public state visible near the call. The final index joins these records and reports missing or inconsistent artifacts.

Original and Adapted packages retain their historical logging behavior. When materialized in a campaign, the host wraps run.py with a recorder so logging remains outside the scientific computation. Standalone launcher outputs are not a complete campaign trajectory archive.

A no-tool run should retain an explicit empty call ledger and trajectory index with an explanation that no tool surface was exposed. Preserve failures and rejected inputs alongside successful calls.

This release provides recorder source and tests. It does not bundle the full historical per-run trajectory archive; archive:// labels in publication data are provenance identifiers only.

