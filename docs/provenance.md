# Source provenance and release transformations

This release is aligned to manuscript V13.3, titled *Agent-Native Bioinformatics Tools: Scientific Use and Adaptation in Protein Engineering*. A paper version is separate from a tool version or physical campaign attempt.

## Frozen sources

| Component | Source identity |
| --- | --- |
| Original tool packages | The tool surface of the completed V9.2 first-batch rerun |
| Adapted tool packages | Final Agent Native v2 tool surface, normal-recovery-v4-final campaign |
| Adapted development commit | `971acef19a9ccefc87ba944524f0d403fd1f5c31` |
| Scientific game source | `f9cc34b47bbb93a676cda46c83723fc3df6685a9` |
| Assigned checkout base | `392e0b7bcd8c59dcee995b2869912614a09349b6` |
| V13.3 source archive | `Agent_Native_Bioinformatics_Tools_V13_3_Source.zip` |
| Source-archive SHA-256 | `2bbb978d1483270de258601ac180fd08137f8930f22acf24c00ffaa6fec74311` |

Internal research commit identifiers identify the input lineage. They are not commits that readers must fetch from the single-commit public repository.

The Original packages describe themselves as reconstructed deterministic offline tools. Those bytes are the tested baseline surface. A missing earlier temporary-worktree archive is not treated as recovered evidence and is not the source of this release.

## Transformations

The 24 scientific packages are copied byte for byte from their evaluated tool surfaces. The portable public CLI invokes those frozen entry points; it does not replace their scientific kernels.

Simulator messages, prompts and test prose are translated into English. Scientific arithmetic, action semantics and machine-readable action/rejection codes are retained. English message translations in environment.py and reporting.py are checked against the source for an identical non-string Python syntax tree. Historical multilingual stop-reason parsing is outside the English interface. The default optional Claude executable is the standard claude command rather than a machine-local wrapper.

The publication tables retain numerical values and original source hashes. Absolute local source paths are converted into non-resolving archive:// identifiers. The score table is projected to arm, tool, dimension and integer score; private-language support/gap/rationale prose is omitted. Dimension names in the summary are translated. This projection does not re-adjudicate scores.

The analysis script gains an explicit output directory. The figure script reads the same numeric data and defaults to a separate output location. These integration edits do not change the statistical design. English campaign documents, packaging, examples, validation, CI and contributor guidance are publication additions.

## What the public package establishes

The bundled derived tables support endpoint reconstruction, fixed daily-trajectory checks, figure regeneration and score summaries. The per-file manifest in [sources.json](../provenance/sources.json) distinguishes exact copies, projections and integration edits, recording original and public SHA-256 values.

The compact release does not contain all original stdout/stderr, model session streams, attempt ledgers or per-call raw trajectory bundles. Hashes and archive identifiers establish a provenance relation but do not make those files downloadable or independently re-auditable here. The upstream biological dataset is acquired separately.

The English reference runner supports a new local experiment. Its prompts are not byte-identical to historical prompts, and the historical model-serving snapshot was not pinned. Fresh outcomes should receive a new experiment identity and their own raw archive.

