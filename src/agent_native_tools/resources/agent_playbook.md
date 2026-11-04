# TadA Campaign Agent Playbook

You are conducting a historical TadA directed-evolution replay. Maximize terminal `cash_final` under the current cash, time and legal-action constraints. Only confirmed improvements earn rewards; screening observations remain provisional.

## Read before acting

Read `agent_playbook.md`, `available_actions.json`, `state.json` and `history.json`. Read `biology_context.md` when the biological meaning or cost of an action needs clarification. Use current public files and actual observed history, not hidden measurements or future paths.

History is a rolling summary. Full pending candidate sequences appear in `state.pending_confirmations` and `available_actions.confirm_hit`. Keep molecular IDs and coordinate conventions when connecting results.

## Legal state changes

Use only the local helper:

~~~bash
./TadACampaign-agent-step --action prepare_seed --seed-id <seed_id> --reasoning "<brief reason>"
./TadACampaign-agent-step --action prepare_library --seed-id <seed_id> --library-id <library_id> --reasoning "<brief reason>"
./TadACampaign-agent-step --action mine_library --seed-id <seed_id> --library-id <library_id> --batch-size <int> --reasoning "<brief reason>"
./TadACampaign-agent-step --action confirm_hit --screen-record-id <int> --reasoning "<brief reason>"
./TadACampaign-agent-step --action stop --reasoning "<brief reason>"
~~~

Do not edit state, history, available actions, Judge session files or benchmark source. After each call, inspect the returned JSON and refresh the public files. On `accepted=false`, inspect the rejection code and reason before choosing again.

## Public state

Visible seeds are backgrounds available for action; prepared seeds have completed preparation. Prepared libraries can be screened if a legal batch fits their remaining sequence count, cash and time. A library's status is locked or prepared. Opening a library can reveal newly visible backgrounds; background preparation alone does not.

Use the seed's actual start sequence, its library's amino-acid interval, the pending screen-record identity and the current history. Equal DNA does not imply equal available library actions. Coordinates are zero-based and half-open; amino-acid interval [a,b) maps to nucleotide interval [3a,3b) under the declared zero frame.

Screening exposes noisy observed_activity. The same sequence within the same seeded run reuses its noisy observation. Only confirm_hit reveals stored confirmed_activity. An accepted confirmation can earn zero reward, and another pending candidate is not silently removed merely because the best activity improved.

## Resource decisions

Inspect `state.economic_summary` before a paid action. It includes confirmation total cost, configured minimum mine-then-confirm reserve, the currently available mine-then-confirm reserve, and budget_regime.

Prefer a legal screening batch that preserves confirmation capacity. An affordable batch is not necessarily profitable. Consider pending candidates before spending their confirmation reserve. When resources are insufficient to complete a useful screen-and-confirm cycle, compare an affordable confirmation, a meaningful alternative path and stopping. Explain any action that intentionally abandons confirmation capacity.

Preparation consumes both direct cost and time burn. Preparing many libraries solely because preparation is cheap can reduce the ability to confirm discoveries. Preserve cash when no credible risk-adjusted marginal gain remains; neither a first hit nor a fixed activity value forces a stop.

## Reward accounting

Reward is paid only for an increase in confirmed best. For each crossed tier segment [lower,upper]:

~~~text
reward += base_asset_value * baseline_activity * tier.multiplier
          * ((1 / lower) - (1 / upper))
~~~

<!-- REWARD_RULES_DYNAMIC_BLOCK -->

The gross reward must be compared with confirmation charges and the opportunity cost of time. Cash, simulated time, host runtime and model expenditure are separate quantities.

## Optional tool use

If `owned_skills_index.json` reports `owned_skill_count > 0`, tools are already owned. Read relevant `owned_skills/<skill_id>/source/SKILL.md` when needed. These capabilities are optional; neither initial ownership nor a purchase requires a call at every step.

Store scientific outputs in `skill_analysis/` and cite their exact findings and paths in action reasoning when they affected a decision. A successful call or sequence QC result does not establish experimental activity or a profitable action. New visible molecular objects can require fresh calculation; reviewing old evidence is not a new scientific measurement.

## Final response

After an accepted stop or legal terminal state, output exactly one JSON line containing status="completed", authoritative stop_code and stop_reason, final action_count and a brief summary. Do not restart a terminal episode.

