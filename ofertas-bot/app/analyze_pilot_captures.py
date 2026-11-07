"""Read-only offline ranking of preserved Shopee captures. No DB or network.

python -m app.analyze_pilot_captures --input docs/search.json --output .runtime/ranking.json
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.integrations.contracts import fingerprint
from app.services.shopee_opportunities import pilot_opportunities
from app.services.shopee_replay import analyze_sanitized_comparison


def analyze_saved_document(document, *, source_file, now):
    report = document.get("report", document)
    captures = []
    for query in report.get("queries", []):
        if query.get("capture"):
            captures.append(query["capture"])
    if not captures:
        comparison = document.get("result", {}).get("comparison")
        if comparison is None and "candidates" in document:
            comparison = document
        if comparison is not None:
            query = comparison.get("query") or document.get("query")
            if not query:
                raise ValueError("Captura sem termo de pesquisa registrado.")
            # Rebuild conservative assessments; never trust legacy inferred proof.
            analysis = json.loads(
                json.dumps(analyze_sanitized_comparison(comparison, query), default=str)
            )
            captures.append(
                dict(
                    capture_id=fingerprint(document),
                    source_file=source_file,
                    source="PRESERVED_SANITIZED_OFFICIAL_RESPONSE",
                    comparison=analysis,
                    comparison_sha256=fingerprint(analysis),
                )
            )
    return dict(
        source_file=source_file,
        source_document_sha256=fingerprint(document),
        analyzed_at=now.isoformat(),
        preserved_captures=captures,
        unavailable_listing_details=max(
            0,
            report.get("counts", {}).get("found", 0)
            - sum(len(c["comparison"]["candidates"]) for c in captures),
        ),
        **pilot_opportunities(captures, now=now),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.input.resolve() == args.output.resolve():
        parser.error("Use uma saída nova; a captura original não será alterada.")
    document = json.loads(args.input.read_text(encoding="utf-8"))
    result = analyze_saved_document(document, source_file=str(args.input), now=datetime.now(UTC))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=str)
    print(
        json.dumps(
            dict(
                candidates=len(result["analysis_queue"]),
                proven_groups=len(result["proven_comparisons"]),
                output=str(args.output),
            )
        )
    )


if __name__ == "__main__":
    main()
