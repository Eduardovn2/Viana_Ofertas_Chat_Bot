"""Analyze an existing sanitized search report without credentials, database or HTTP."""

import argparse
import json
from pathlib import Path

from app.services.shopee_replay import analyze_recorded_search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    result = analyze_recorded_search(report)
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2, default=str)
            output.write("\n")
    summary = {
        key: result[key]
        for key in (
            "mode",
            "status",
            "returned_listings",
            "distinct_shops",
            "review_warnings",
            "lowest_price_highlights",
            "external_http_calls",
            "review_queue",
        )
    }
    summary["proven_group_count"] = len(result["proven_comparisons"])
    summary["opportunity_counts"] = result["opportunity_view"]["counts"]
    summary["commercial_handoff_allowed"] = result["opportunity_view"]["commercial_handoff"][
        "can_create_offer"
    ]
    summary["unproven_candidates"] = [
        {
            key: row[key]
            for key in (
                "identity",
                "name",
                "announced_price",
                "currency",
                "equivalence_classification",
            )
        }
        for row in result["unproven_candidates"]
    ]
    summary["stores"] = [
        dict(
            shop_id=store["shop_id"],
            shop_name_variants=store["shop_name_variants"],
            listing_identities=[row["identity"] for row in store["listings"]],
        )
        for store in result["stores"]
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
