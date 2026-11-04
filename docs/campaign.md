# Preparing a new campaign

The reference runner uses the scientific game implementation frozen for the manuscript. Its public messages and prompts are English translations. A new campaign has its own experiment identity and must not replace a historical endpoint.

## Biological inputs

Acquire the six DNA/protein train, validation and test Parquet files from the fixed TadA-Bench dataset revision 07168448caaafab4efb26eca04ec3e503edf1c04:

https://huggingface.co/datasets/JinGao/TadA-Bench/tree/07168448caaafab4efb26eca04ec3e503edf1c04

The prepare-data command expects a ZIP with entries:

~~~text
data/all.DNA.train-00000-of-00001.parquet
data/all.DNA.val-00000-of-00001.parquet
data/all.DNA.test-00000-of-00001.parquet
data/all.AA.train-00000-of-00001.parquet
data/all.AA.val-00000-of-00001.parquet
data/all.AA.test-00000-of-00001.parquet
~~~

Use upstream file bytes and preserve their hashes. A newly constructed ZIP is new packaging of fixed public inputs, not recovery of a missing historical ZIP. The prepared tables retain the measured sequence, functional label and round membership needed by the simulator.

~~~bash
TadACampaign prepare-data --zip-path /path/to/TadABench-1M.zip --output-dir prepared
~~~

The published collection has 1,027,200 unique DNA sequences and 409,869 proteins after synonymous collapse. These published counts describe upstream data; repository tests use small synthetic inputs.

## Configuration preparation

~~~bash
python scripts/prepare_run.py \
  --arm adapted --tool sequence-properties \
  --prepared-root prepared --output-dir outputs/run-setup
~~~

Choose original for the Original surface. Choose no-tool and omit --tool for a reference configuration. The default random seed is 20260329; supply --seed when defining matched repeats.

Preparation writes config.toml, tool_surface/skill_market.json, copied tool files, hashes and a SETUP.json receipt. It checks the prepared manifest, validates the configuration with the actual loader and refuses to overwrite an existing setup. It performs no model request.

The generated scientific settings use the manuscript resource rules. A tool-enabled run has exactly one assigned package, no purchases and a zero acquisition fee. A no-tool configuration has no tool surface. The model and reasoning values are explicit requested settings; they do not pin server-side weights.

## Reference execution

~~~bash
TadACampaign run --config outputs/run-setup/config.toml --agent
~~~

The above command launches the separately configured local agent client. Run only after reviewing its environment and intended model access. The repository does not install authentication, change account configuration or provide model credentials.

The host Judge must retain private prepared measurements and configuration. The agent should receive only its public workspace, legal-action helper and assigned package. A plain host process is not an isolation boundary. For research collection, configure an appropriate sandbox/container that excludes prepared tables, hidden labels, raw history and the host source from agent access; the legacy container adapter is reference code and does not alone establish that separation.

Capture exact prompt/config/source/data hashes, requested and runtime-reported model identity, Python/agent/Docker versions, start/end UTC, wall time, exit code, all attempts, public state, Judge actions, model events and tool trajectories. Keep one cumulative runtime clock across transport retries and resumes. A context-window handoff must continue the same Judge state.

## Study design and archives

The reported historical dataset has 120 Original-tool and 120 Adapted-tool campaigns plus ten shared Original no-tool references. Each underlying 12-tool arm campaign can have a 130-assignment design, but the paper uses the stated 250-population projection and does not include the Adapted no-tool cohort. Do not conflate campaign-root counts with paper counts.

A collection controller should expose an explicit concurrency ceiling, retain a stable key per physical attempt and update indexes during collection. The source campaign ceiling was ten concurrent runs. This release's preparation command does not launch a controller or enlarge that design.

Use a persistent campaign root containing README, frozen protocol and input manifest, assignments, run_control, runs, index, analysis, archive and checksums. Keep completed raw files append-only. Never place the authoritative raw archive beneath a temporary worktree that could later be removed.

A complete raw campaign record requires readable call bundles and inventory checks. Summaries, commands, a provenance hash or a session transcript do not replace missing raw files.

