# Scoring adjudication record

This file records decisions made while reconciling the independent formula
implementation in `fixtures/generate_goldens.py` with `scoring/engine.py`.
The goldens are not regenerated from the engine.

## Decisions implemented

- The overall weights intentionally sum to 1.15 (0.50 FOS + 0.20 distress +
  0.20 DCS - 0.25 risk). A perfect property therefore tops out below 100;
  this preserves the risk penalty's intended conservatism rather than
  renormalizing away risk.
- Recommendation uses `_recommend` in production. Its `near_tie_points`
  setting is therefore active and controls the alternative-strategy list;
  it is not dead configuration.

- `dcs_below_40` is the gate name for the §10 DCS-under-40 cap. The previous
  `needs_review` label was too generic for the contract and is replaced.
- `foreclosure_cap` is applied when an active foreclosure exists and DCS is
  below 75. Overall score is capped at 70, per §8.6. This is separate from
  `insufficient_data` and `open_gating_flag`.
- Score gates remain descriptive strings in `ScoreSet.gates_applied`; these
  values are persisted in `scoring_configs`/`scores` and consumed by ranking.

## Items requiring adjudication before golden acceptance

- FOS: §10's best-strategy and `discount_to_value` interpretation must be
  settled against the independent worksheet outputs fixture by fixture.
- Distress and risk: the decay anchor, category caps, HOA/lien treatment, and
  title-flag mapping must be compared term-by-term with §10 and the worksheet.
- Components: the API contract should expose raw normalized factors and
  weighted point contributions under distinct stable keys; the current engine
  and goldens use incompatible key sets.
- Quantization: §10's 4-decimal score convention should be applied only after
  the above terms are agreed, then asserted by deterministic tests.

No authoritative human gold data is present in this repository, so these
remaining adjudications are not silently resolved by rewriting fixtures.
