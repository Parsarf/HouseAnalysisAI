"""Extraction eval harness (spec §18): replays recorded responses offline and
scores per-field accuracy, null rate, and grounding-failure rate against gold
labels. This is the whole harness — run it before adopting a prompt change.

Fixture layout under ``fixtures/recorded_responses/``:

- ``<id>.json``        — a ``RecordedResponse`` (provider payload, schema-shaped
                         or legacy ``{"facts": [...]}``)
- ``<id>.pages.json``  — ``{"unit_type": ..., "pages": {"1": "page text", ...}}``
- ``<id>.gold.json``   — ``{"expected": [{"field_path": ..., "value_parsed"|"value_text"|...}]}``
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from contracts import RecordedResponse

from .client import replay_response

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures" / "recorded_responses"


@dataclass(frozen=True)
class FieldScore:
    expected: int
    matched: int

    @property
    def accuracy(self) -> float:
        return self.matched / self.expected if self.expected else 1.0


@dataclass(frozen=True)
class EvalReport:
    documents: int
    facts_kept: int
    facts_dropped: int
    grounding_failures: int
    null_facts: int
    field_scores: dict[str, FieldScore] = field(default_factory=dict)

    @property
    def grounding_failure_rate(self) -> float:
        total = self.facts_kept + self.facts_dropped
        return self.grounding_failures / total if total else 0.0

    @property
    def null_rate(self) -> float:
        return self.null_facts / self.facts_kept if self.facts_kept else 0.0

    @property
    def overall_accuracy(self) -> float:
        expected = sum(score.expected for score in self.field_scores.values())
        matched = sum(score.matched for score in self.field_scores.values())
        return matched / expected if expected else 1.0


def _is_null(fact) -> bool:
    return all(
        value is None
        for value in (fact.value_raw, fact.value_parsed, fact.value_text, fact.value_date, fact.value_bool)
    )


def _value_matches(fact, expected: dict) -> bool:
    for key in ("value_parsed", "value_text", "value_raw", "value_date", "value_bool"):
        if key not in expected:
            continue
        actual = getattr(fact, key)
        wanted = expected[key]
        if key == "value_parsed":
            return actual is not None and Decimal(str(wanted)) == actual
        return str(actual) == str(wanted) if actual is not None else wanted is None
    return False


def evaluate(fixtures_dir: Path = FIXTURES_DIR) -> EvalReport:
    documents = kept = dropped = grounding_failures = null_facts = 0
    scores: dict[str, list[int]] = {}
    for path in sorted(fixtures_dir.glob("*.json")):
        if path.name.endswith((".pages.json", ".gold.json")):
            continue
        recorded = RecordedResponse.model_validate_json(path.read_text())
        pages_file = path.with_suffix(".pages.json")
        sidecar = json.loads(pages_file.read_text()) if pages_file.exists() else {}
        pages = {int(number): text for number, text in sidecar.get("pages", {}).items()}
        result = replay_response(recorded, pages, unit_type=sidecar.get("unit_type", "liens"))
        documents += 1
        kept += len(result.facts)
        dropped += result.dropped
        grounding_failures += result.counters.get("grounding_failed", 0)
        null_facts += sum(1 for fact in result.facts if _is_null(fact))
        gold_file = path.with_suffix(".gold.json")
        if gold_file.exists():
            expected = json.loads(gold_file.read_text()).get("expected", [])
            for want in expected:
                field_path = want["field_path"]
                scores.setdefault(field_path, [0, 0])
                scores[field_path][1] += 1
                if any(fact.field_path == field_path and _value_matches(fact, want) for fact in result.facts):
                    scores[field_path][0] += 1
    return EvalReport(
        documents, kept, dropped, grounding_failures, null_facts,
        {name: FieldScore(total, matched) for name, (matched, total) in scores.items()},
    )


def main(fixtures_dir: Path = FIXTURES_DIR) -> EvalReport:
    report = evaluate(fixtures_dir)
    if report.documents == 0:
        print("No recorded responses found; populate fixtures/recorded_responses/ to run the eval.")
        return report
    print(f"documents: {report.documents}")
    print(f"facts kept: {report.facts_kept}  dropped: {report.facts_dropped}")
    print(f"grounding failures: {report.grounding_failures} ({report.grounding_failure_rate:.1%})")
    print(f"null rate: {report.null_rate:.1%}")
    print(f"overall field accuracy: {report.overall_accuracy:.1%}")
    for name, score in sorted(report.field_scores.items()):
        print(f"  {name}: {score.matched}/{score.expected} ({score.accuracy:.1%})")
    return report


if __name__ == "__main__":
    main()
