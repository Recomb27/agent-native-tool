# Reproducing the software and manuscript analysis

There are three distinct tasks: executing a scientific tool, reconstructing statistics from frozen derived evidence, and collecting new agent-campaign evidence. Choose the one needed; the first two require no model service.

## Installation and release verification

From the repository root:

~~~bash
uv sync --locked --extra campaign --extra figures --extra dev
uv run python scripts/verify_release.py
uv run pytest
~~~

The verifier checks the exact release checksum set, language hygiene, portable paths and package completeness. Scientific tests exercise successful and rejected inputs and verify the evidence bindings. Test workspaces are synthetic and are not historical experiments.

## Endpoint tables

~~~bash
python3 paper/scripts/recompute_results.py --output-dir outputs/analysis
~~~

This path uses the Python standard library. It requires the bundled paired endpoint, usage, score and endpoint-reference tables. Expected output is outputs/analysis/recomputed.json.

The script checks 120 unique pairs, ten repeat clusters and ten distinct shared controls; checks resource identities; reconstructs paired cluster intervals; and checks all integer scores and score deltas. Its tolerance is 1e-8 absolute or 1e-11 relative for endpoint comparisons.

The source data retain more precision than the manuscript tables. Compare before rounding. Successful reconstruction validates consistency of the released derived evidence, not the completeness of the original raw session archive.

## Figures

~~~bash
uv run --extra figures python paper/scripts/make_v133_figures.py \
  --only all --output-dir outputs/figures
~~~

The system commands inkscape and pdftoppm must be installed separately. The full rebuild writes SVG, PDF and PNG for fig1_overview, fig2_radars, fig3_case, fig4_daily and fig5_tool_deltas. It also writes per-figure numerical and typography audits and the Figure 4 pointwise-interval JSON.

The renderer reads the bundled font files and retains their license. PNG files are rendered from exported PDFs. Figure hashes can depend on the renderer version even when all plotted data agree; compare numeric audit values separately from exact rendering bytes.

The original frozen figures are under paper/figures/. New figures go to outputs/, preserving those inputs.

Figure 4 uses seed 20261104 and 10,000 repeat-cluster bootstrap draws. It checks the 43,440 daily tool-arm values against 6,265 accepted state points, and validates 1,810 Original no-tool daily rows against the corresponding endpoints. All curves retain the same episode denominator after stopping.

## Scientific tool packages

~~~bash
uv run agent-native-tool example sequence-properties --workspace outputs/qc
uv run agent-native-tool run sequence-properties --workspace outputs/qc --rows rows.json
~~~

The 24 packages are independent file bundles. Tests run each package using real input/output and verify Adapted artifact hashes. Fixtures have no measured biological labels.

Visible and review mode examples are described in the main README. An independent user dataset must satisfy that tool's declared alphabet, coordinate, frame and scope assumptions.

## New campaigns

Read [campaign.md](campaign.md) and obtain the biological dataset independently. The prepare_run.py command creates local inputs without launching an agent.

A fresh campaign is not required to reconstruct the paper's descriptive results. If collecting one, record exact code/data/config/prompt hashes, requested and reported model identities, client/runtime versions, UTC times, wall time, cumulative budget and every attempt. Use a persistent evidence root outside temporary checkouts; retain raw files and explicit empty trajectory indexes for no-tool runs.

The historical study has separate Original and Adapted cohorts, shared references and unequal execution conditions. A new experiment must define its own population, matching and stopping criteria before collection. Do not silently expand ten shared references into 120 independent controls, select endpoints by reward, or merge new results into frozen manuscript tables.

