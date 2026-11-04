# filter-sequences

This is a reconstructed, deterministic offline tool for the bounded composition audit. It is available only because this condition exposes it.

Capability: Apply explicit DNA length, alphabet and stop-codon QC predicates.

Run it from the workspace root exactly as follows:

```bash
python3 owned_skills/filter-sequences/source/run.py --input source_inputs.json --rows rows_to_audit.json --output artifacts/filter-sequences.json --run-root .
```

The output is a JSON artifact with `tool_id`, input hashes and a `payload` containing the tool-specific result. Preserve that artifact as evidence. Do not infer biological function or campaign benefit from this tool alone.
