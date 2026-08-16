"""Offline tests for calibration/ (WP-17): outcome recording, predicted-vs-
actual analyses, min-sample gating, suggestion acceptance, and rank preview."""
from decimal import Decimal
from uuid import uuid4

from calibration import (
    DealOutcome,
    InMemoryCalibrationStore,
    PredictionSnapshot,
    RealizedDeal,
    accept_suggestion,
    apply_scoring_suggestion,
    build_suggestions,
    gut_rating_correlation,
    holding_period_analysis,
    rank_deltas,
    repair_cost_analysis,
    score_band_analysis,
    scoring_weight_analysis,
    valuation_analysis,
)
from calibration.suggestions import (
    KIND_REPAIR_PSF,
    KIND_SCORING_OVERALL_WEIGHTS,
    CalibrationSuggestion,
)
from contracts import (
    AcquisitionCosts,
    AssumptionSet,
    AttachmentBasis,
    HoldingAssumptions,
    RepairAssumptions,
    ResaleAssumptions,
    StrategyAssumptions,
)


def assumptions():
    return AssumptionSet(
        id=uuid4(), version=3, name="test",
        acquisition=AcquisitionCosts(closing_pct=Decimal(".01"), title_pct=Decimal(".005"),
                                     escrow_flat=Decimal(1500), financing_points=Decimal(".02"),
                                     financing_flat=Decimal(1200), inspection_flat=Decimal(600),
                                     legal_flat=Decimal(1500), acq_fee_pct=Decimal(".01")),
        repairs=RepairAssumptions(psf_by_condition={"moderate": Decimal(40), "cosmetic": Decimal(15)},
                                  low_multiplier=Decimal(".75"), high_multiplier=Decimal("1.4"),
                                  regional_index=Decimal(1)),
        holding=HoldingAssumptions(insurance_pct_yr=Decimal(".0035"), utilities_monthly=Decimal(180),
                                   maintenance_pct_yr=Decimal(".005"), acquisition_months=Decimal(2),
                                   repair_months_by_condition={"moderate": Decimal(4)},
                                   market_days_default=60),
        resale=ResaleAssumptions(commission_pct=Decimal(".05"), seller_closing_pct=Decimal(".01"),
                                 concessions_pct=Decimal(".01"), staging_flat=Decimal(3500),
                                 misc_pct=Decimal(".0025")),
        strategy=StrategyAssumptions(cash_target_margin=Decimal(".2"),
                                     flip_target_margin_by_arv_band={"default": Decimal(".2")},
                                     wholesale_investor_pct=Decimal(".7"),
                                     min_assignment_spread=Decimal(15000),
                                     hard_money={"rate": Decimal(".1"), "points": Decimal(".02"), "ltv": Decimal(".85")},
                                     rental={"vacancy": Decimal(".06"), "maintenance_pct": Decimal(".08"),
                                             "management_pct": Decimal(".08")}),
        attachment_probability={AttachmentBasis.OWNER_NAMED_ONLY: Decimal(".35"),
                                AttachmentBasis.UNKNOWN: Decimal(".5")},
        unknown_lien_medians={"judgment": Decimal(18000)},
        valuation_weights={"avm": Decimal("0.5"), "comp_sales": Decimal("0.5")})


def deal(*, outcome=DealOutcome.SOLD, purchase=Decimal(200000), repairs=Decimal(60000),
         days=120, sale=Decimal(350000), costs=Decimal(30000), snapshot=None):
    return RealizedDeal(property_id=uuid4(), purchase_price=purchase, actual_repairs=repairs,
                        actual_holding_days=days, sale_price=sale, actual_costs=costs,
                        outcome=outcome, snapshot=snapshot or PredictionSnapshot())


def test_store_roundtrip():
    store = InMemoryCalibrationStore()
    first, second = deal(), deal(outcome=DealOutcome.DEAD, sale=None)
    store.record(first)
    store.record(second)
    assert store.list() == [first, second]


def test_realized_profit_and_hit():
    assert deal().realized_profit == Decimal(60000)
    assert deal().is_hit
    losing = deal(sale=Decimal(240000))
    assert losing.realized_profit == Decimal(-50000)
    assert not losing.is_hit
    dead = deal(outcome=DealOutcome.DEAD, sale=None)
    assert dead.realized_profit is None
    assert not dead.is_hit


def test_repair_suggestion_converges_on_planted_true_psf():
    """20 synthetic deals: predicted at $40/sqft, actuals planted at $50/sqft
    (with noise); the suggestion must land within 5% of the planted value."""
    current, true_psf = Decimal(40), Decimal(50)
    deals = []
    for index in range(20):
        sqft = Decimal(1200 + 37 * index)
        noise = Decimal(1) + Decimal((index % 5) - 2) / Decimal(100)  # ±2% noise
        deals.append(deal(snapshot=PredictionSnapshot(
            condition="moderate", sqft=sqft,
            predicted_repairs=current * sqft),
            repairs=(true_psf * sqft * noise).quantize(Decimal("0.01"))))
    analysis = repair_cost_analysis(deals, assumptions())
    assert len(analysis) == 1
    suggested = analysis[0].suggested_psf
    assert suggested is not None
    assert abs(suggested - true_psf) / true_psf <= Decimal("0.05")
    assert analysis[0].sample_size == 20


def test_repair_suggestion_withheld_below_min_sample():
    deals = [deal(snapshot=PredictionSnapshot(condition="moderate", sqft=Decimal(1500),
                                              predicted_repairs=Decimal(60000)))
             for _ in range(4)]
    analysis = repair_cost_analysis(deals, assumptions())
    assert analysis[0].sample_size == 4
    assert analysis[0].suggested_psf is None
    assert build_suggestions(assumptions(), deals) == []


def test_valuation_reweighting_favors_accurate_candidate():
    deals = []
    for index in range(6):
        sale = Decimal(300000 + 5000 * index)
        deals.append(deal(sale=sale, snapshot=PredictionSnapshot(valuation_predictions={
            "comp_sales": sale * Decimal("1.01"),   # 1% off
            "avm": sale * Decimal("1.10"),          # 10% off
        })))
    result = valuation_analysis(deals)
    assert result.suggested_weights is not None
    assert sum(result.suggested_weights.values()) == Decimal(1)
    assert result.suggested_weights["comp_sales"] > Decimal("0.85")
    assert result.suggested_weights["comp_sales"] > result.suggested_weights["avm"]
    avm_stats = next(stat for stat in result.stats if stat.valuation_type == "avm")
    assert avm_stats.mean_signed_error_pct == Decimal("0.10")


def test_valuation_reweighting_withheld_below_min_sample():
    deals = [deal(sale=Decimal(300000), snapshot=PredictionSnapshot(valuation_predictions={
        "comp_sales": Decimal(303000), "avm": Decimal(330000)})) for _ in range(4)]
    assert valuation_analysis(deals).suggested_weights is None


def test_holding_period_bias_suggests_new_market_days():
    deals = [deal(days=120, snapshot=PredictionSnapshot(predicted_holding_days=90))
             for _ in range(6)]
    result = holding_period_analysis(deals, assumptions())
    assert result.mean_bias_days == Decimal(30)
    assert result.suggested_market_days == 90  # 60-day default + 30-day bias
    short = holding_period_analysis(deals[:4], assumptions())
    assert short.suggested_market_days is None


def test_gut_rating_correlation():
    strong = [deal(snapshot=PredictionSnapshot(gut_rating=rating,
                                               overall_score=Decimal(rating * 20)))
              for rating in (1, 2, 3, 4, 5)]
    result = gut_rating_correlation(strong)
    assert result.coefficient == Decimal(1)
    weak = gut_rating_correlation(strong[:3])
    assert weak.coefficient is None  # below min sample


def test_score_band_hit_rates():
    deals = []
    for index in range(4):  # top band: all hits
        deals.append(deal(snapshot=PredictionSnapshot(overall_score=Decimal(90))))
    for index in range(4):  # bottom band: all misses
        deals.append(deal(sale=Decimal(240000),
                          snapshot=PredictionSnapshot(overall_score=Decimal(30))))
    bands = score_band_analysis(deals)
    by_band = {stat.band[0]: stat for stat in bands}
    assert by_band[Decimal(85)].hit_rate == Decimal(1)
    assert by_band[Decimal(0)].hit_rate == Decimal(0)
    assert by_band[Decimal(85)].mean_realized_profit == Decimal(60000)
    assert by_band[Decimal(60)].sample_size == 0
    assert by_band[Decimal(60)].hit_rate is None


def _pillar_deals(hits_fos, misses_fos, hits_dcs, misses_dcs,
                  hits_distress=70, misses_distress=30, hits_risk=20, misses_risk=20):
    deals = []
    for fos, dcs in zip(hits_fos, hits_dcs):
        deals.append(deal(snapshot=PredictionSnapshot(pillar_scores={
            "fos": Decimal(fos), "distress": Decimal(hits_distress),
            "dcs": Decimal(dcs), "risk": Decimal(hits_risk)})))
    for fos, dcs in zip(misses_fos, misses_dcs):
        deals.append(deal(sale=Decimal(240000), snapshot=PredictionSnapshot(pillar_scores={
            "fos": Decimal(fos), "distress": Decimal(misses_distress),
            "dcs": Decimal(dcs), "risk": Decimal(misses_risk)})))
    return deals


def test_scoring_weight_proposal_moves_weight_to_discriminating_pillar():
    weights = {"fos": Decimal("0.50"), "distress": Decimal("0.20"),
               "dcs": Decimal("0.20"), "risk": Decimal("0.25")}
    deals = _pillar_deals([80, 82, 78, 85, 81, 79], [20, 25, 22, 18, 24, 21],
                          [50, 52, 48, 55, 51, 49], [60, 62, 58, 65, 61, 59])
    result = scoring_weight_analysis(deals, weights)
    assert result.suggested_overall_weights is not None
    assert result.suggested_overall_weights["dcs"] == Decimal("0.15")
    assert result.suggested_overall_weights["fos"] == Decimal("0.55")
    assert sum(result.suggested_overall_weights.values()) == sum(weights.values())
    assert "dcs" in result.rationale


def test_scoring_weight_no_proposal_when_all_pillars_discriminate():
    weights = {"fos": Decimal("0.50"), "distress": Decimal("0.20"),
               "dcs": Decimal("0.20"), "risk": Decimal("0.25")}
    deals = _pillar_deals([80, 82, 78, 85, 81, 79], [20, 25, 22, 18, 24, 21],
                          [80, 82, 78, 85, 81, 79], [30, 32, 28, 35, 31, 29],
                          hits_risk=10, misses_risk=40)
    assert scoring_weight_analysis(deals, weights).suggested_overall_weights is None
    # and below min sample
    assert scoring_weight_analysis(deals[:5], weights).suggested_overall_weights is None


def test_accept_suggestion_creates_new_version_and_recompute_request():
    base = assumptions()
    deals = [deal(snapshot=PredictionSnapshot(condition="moderate", sqft=Decimal(1500),
                                              predicted_repairs=Decimal(60000)),
                  repairs=Decimal(75000))
             for _ in range(6)]
    suggestion = next(item for item in build_suggestions(base, deals)
                      if item.kind == KIND_REPAIR_PSF)
    new_set, request = accept_suggestion(base, suggestion)
    assert new_set.version == base.version + 1
    assert new_set.id != base.id
    assert new_set.repairs.psf_by_condition["moderate"] == suggestion.proposed == Decimal("50.00")
    # old version untouched and rollback-able
    assert base.repairs.psf_by_condition["moderate"] == Decimal(40)
    assert new_set.repairs.psf_by_condition["cosmetic"] == Decimal(15)
    assert request.assumption_set_id == new_set.id
    assert request.reason == "calibration:repair_psf"


def test_apply_scoring_suggestion_bumps_config_version():
    config = {"version": 7,
              "weights": {"overall": {"fos": Decimal("0.50"), "distress": Decimal("0.20"),
                                      "dcs": Decimal("0.20"), "risk": Decimal("0.25")},
                          "fos": {"profit": Decimal("0.30")}},
              "bounds": {"profit": (Decimal(0), Decimal(150000))}}
    suggestion = CalibrationSuggestion(
        kind=KIND_SCORING_OVERALL_WEIGHTS, target="scoring_config.weights.overall",
        current=config["weights"]["overall"],
        proposed={"fos": Decimal("0.55"), "distress": Decimal("0.20"),
                  "dcs": Decimal("0.15"), "risk": Decimal("0.25")},
        sample_size=12, rationale="dcs does not separate hits from misses.")
    new_config = apply_scoring_suggestion(config, suggestion)
    assert new_config["version"] == 8
    assert new_config["weights"]["overall"]["fos"] == Decimal("0.55")
    assert new_config["weights"]["fos"] == config["weights"]["fos"]  # other sections intact
    assert config["weights"]["overall"]["dcs"] == Decimal("0.20")    # old config untouched


def test_rank_deltas_preview():
    pids = [uuid4() for _ in range(3)]
    before = {pids[0]: Decimal(80), pids[1]: Decimal(70), pids[2]: Decimal(60)}
    after = {pids[0]: Decimal(65), pids[1]: Decimal(75), pids[2]: Decimal(60)}
    changes = {change.property_id: change for change in rank_deltas(before, after)}
    assert changes[pids[1]].prev_rank == 2 and changes[pids[1]].new_rank == 1
    assert changes[pids[1]].delta == 1  # moved up 1
    assert changes[pids[0]].delta == -1
    assert changes[pids[2]].delta == 0
