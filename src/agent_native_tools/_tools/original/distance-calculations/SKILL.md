# distance-calculations

This is a reconstructed, deterministic offline tool for the bounded composition audit. It is available only because this condition exposes it.

Capability: Compute all equal-length DNA and translated-AA Hamming pairs.

Run it from the workspace root exactly as follows:

```bash
python3 owned_skills/distance-calculations/source/run.py --input source_inputs.json --rows rows_to_audit.json --output artifacts/distance-calculations.json --run-root .
```

The output is a JSON artifact with `tool_id`, input hashes and a `payload` containing the tool-specific result. Preserve that artifact as evidence. Do not infer biological function or campaign benefit from this tool alone.
