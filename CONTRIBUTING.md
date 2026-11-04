# Contributing

Use a separate branch and checkout for a change. Install the development extras, run the relevant tests and explain the scientific behavior affected by the change.

Keep the frozen Original and Adapted packages and manuscript data unchanged. A revised algorithm or contract belongs in a separately identified version with its own scientific checks and experiment identity. Include boundary inputs, molecular units and independent expected results when changing a calculation.

Run python scripts/verify_release.py, pytest and python paper/scripts/recompute_results.py before submitting a change that affects the release. For figures, regenerate into a separate output directory and compare numerical audits with the frozen data.

Use English throughout source, comments, documentation, filenames and published examples. Do not commit credentials, local environments, machine-specific paths, generated campaign outputs or private session traces. Report the exact command and dependency version with an issue; use synthetic minimal inputs where possible.

This repository's research records distinguish computational correctness, valid interpretation, observed use and campaign contribution. Preserve those distinctions in documentation and claims.

