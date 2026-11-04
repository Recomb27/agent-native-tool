# TadA Campaign Biology Context

This file is optional context for agents running TadA Campaign. Use it when the current decision needs biological meaning, cost basis, or experimental intuition. The authoritative game state remains `state.json`, `available_actions.json`, and `history.json`.

## Campaign Abstraction

TadA Campaign models a TadA directed-evolution campaign as a budgeted sequence of experimental commitments. A seed is a verified background sequence that can support later library construction. A library is one concrete mutational window on that background. Mining a library is a noisy discovery screen; confirming a hit is the slower orthogonal measurement that can turn a provisional signal into settled value.

The benchmark intentionally exposes action-level biology instead of a wet-lab protocol. The goal is to support realistic planning, not to provide an executable procedure.

## Action Meanings

`prepare_seed` turns a visible background into a reusable program start. In experimental terms, this represents sequence preparation, cloning or equivalent assembly, identity checks, archival storage, and template preparation for future libraries. It does not reveal downstream seeds because preparing a background does not itself prove which mutational window is useful.

`prepare_library` opens one specific mutational window. It represents designing and assembling a focused variant pool for the seed, followed by basic quality checks. Branch reveal is tied to this action because the downstream path is only meaningful after the relevant window exists as a usable library.

`mine_library` is a discovery screen. It samples a batch from the prepared library and returns proxy evidence. The reported `observed_activity` can be shifted by bottlenecks, amplification bias, and measurement noise, so it is useful for ranking candidates but does not settle value.

`confirm_hit` is a per-candidate orthogonal validation step. Once started, its cost and elapsed time are spent regardless of whether the candidate improves the current best. Only confirmed activity can update `confirmed_best_activity` and trigger reward.


`stop` represents ending the campaign and preserving the current cash position.

## Time And Cost Basis

All costs are expressed in benchmark budget units. Public docs use `$` when a currency symbol is needed. The numeric scale is a normalized simulator budget; it does not measure the model-service bill or establish a current laboratory price.

Days are simulated experimental time, independent of host runtime and model-serving latency. Each action consumes its configured simulated duration.

Current public presets use these action-level settings:

| Action | Direct cost | Time days | Planning interpretation |
| --- | ---: | ---: | --- |
| `prepare_seed` | `$365` | `3` | full background activation, including heavier synthesis cases |
| `prepare_library` | `$142` | `2` | one focused mutational-window library |
| `mine_library` | `$1200-$1400` | `9-10` | one high-throughput discovery batch |
| `confirm_hit` | `$467` | `12` | one candidate confirmed by an orthogonal measurement |
| `stop` | `$0` | `0` | no further experiment |

`daily_burn_cost` is `$800` in the public presets. It represents the opportunity cost of keeping a campaign active for one elapsed day. Total action cost is always:

```text
direct_action_cost + time_days * daily_burn_cost
```

## Mining Throughput

The public mining menu uses batch sizes `780`, `880`, `1020`, and `1280`. These are variant counts, not prices. The corresponding direct costs increase modestly because the dominant burden is setting up and processing the screen rather than paying linearly per candidate.

Small or sparse libraries may have fewer remaining sequences than the smallest batch. In that case `mine_library` is not legal for that library, but preparing that library can still be strategically useful if it reveals a downstream seed.

## Observed Versus Confirmed

`observed_activity` is a screening readout. It is stable for a sequence within a run but can differ from the underlying confirmed value.

`confirmed_activity` appears only after `confirm_hit`. This is the value used for reward and `confirmed_best_activity`. A strong provisional hit can be worth confirming, but confirmation should be compared against direct cost, time burn, and the opportunity cost of occupying the remaining campaign window.

## Seed And Library Structure

The canonical V2 structure presents the campaign as seven agent-facing seed backgrounds:

| Seed | Agent-facing description |
| --- | --- |
| `seed1` | visible starting background group |
| `seed2` | background group revealed only after the relevant upstream library is prepared |
| `seed3` | alternate background group revealed only after the relevant upstream library is prepared |
| `seed4` | background group revealed only after the relevant upstream library is prepared |
| `seed5` | background group with multiple mutational-window libraries |
| `seed6` | background group with two mutational-window libraries |
| `seed7` | background group with one mutational-window library |

Branch structure is learned through actions. After `prepare_library`, `newly_visible_seed_ids` in the action response and the refreshed `visible_seed_ids` record whether that library opened new seed backgrounds.

At each decision point, the agent knows the current visible seeds, their start sequences, the libraries attached to those seeds, each visible library's mutational window and status, prepared-library screen counts, observed and confirmed evidence so far, pending confirmations, current cash, remaining days, and its own action history.

Branch value and immediate activity value are different. A library can be useful because it reveals additional legal options, but that branch-opening value must be balanced against cost, time, sequence information, observed/confirmed evidence, and concrete analysis from the current state.

