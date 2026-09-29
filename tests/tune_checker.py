"""Replays saved evaluation results to show how the fact-checker's strictness
(MIN_GROUNDED_SHARE) trades good answers kept against unsupported ones let
through, and recommends a value. Makes no LLM calls.

    python -m tests.tune_checker [results.json]
"""

import glob
import json
import os
import sys

from agent import verifier
from agent.responder import NOT_FOUND_REPLY
from agent.support_agent import get_products

LEVELS = [round(0.30 + 0.05 * i, 2) for i in range(13)]


def load(path=None):
    if path is None:
        files = sorted(glob.glob(os.path.join("tests", "results", "eval_*.json")))
        if not files:
            sys.exit("No results yet. Run: python -m tests.run_eval")
        path = files[-1]
    with open(path, encoding="utf-8") as f:
        return path, json.load(f)


def sweep(records, products):
    original = verifier.MIN_GROUNDED_SHARE
    rows = []
    try:
        for level in LEVELS:
            verifier.MIN_GROUNDED_SHARE = level
            kept = thrown = through = 0
            let_through_ids = []
            for rec in records:
                answer = verifier.clean_answer(rec["raw_answer"])
                accepted = not verifier.verify(answer, rec["context"], products)
                good = rec["raw_answer_acceptable"]
                if good and accepted:
                    kept += 1
                elif good:
                    thrown += 1
                elif accepted:
                    through += 1
                    let_through_ids.append(rec["case_id"])
            rows.append({"level": level, "kept": kept, "thrown_away": thrown,
                         "let_through": through, "let_through_cases": let_through_ids})
    finally:
        verifier.MIN_GROUNDED_SHARE = original
    return rows


def recommend(rows, current):
    safe = [r for r in rows if r["let_through"] == 0]
    pool = safe or [r for r in rows if r["let_through"] == min(x["let_through"] for x in rows)]
    fewest_thrown = min(r["thrown_away"] for r in pool)
    tied = [r for r in pool if r["thrown_away"] == fewest_thrown]
    for r in tied:
        if abs(r["level"] - current) < 1e-9:
            return r
    return max(tied, key=lambda r: r["level"])


def collect(results):
    records = []
    for case in results["cases"]:
        ai = case.get("ai")
        if not ai:
            continue
        if NOT_FOUND_REPLY.lower()[:30] in verifier.clean_answer(ai["raw_answer"]).lower():
            continue
        records.append({**ai, "case_id": case["id"]})
    return records


def main(argv):
    path, results = load(argv[0] if argv else None)
    records = collect(results)
    model = results.get("summary", {}).get("model", "?")
    print(f"Results: {path}  (model: {model})")
    if not records:
        print("No AI answers in this results file. Run the full test sheet: python -m tests.run_eval")
        return 1
    good = sum(r["raw_answer_acceptable"] for r in records)
    print(f"AI answers replayed: {len(records)} ({good} good, {len(records) - good} bad)\n")
    rows = sweep(records, get_products())
    current = verifier.MIN_GROUNDED_SHARE
    print(f"{'strictness':>10} | {'kept':>5} | {'thrown away':>11} | {'let through':>11}")
    print("-" * 48)
    for r in rows:
        mark = "  <- current" if abs(r["level"] - current) < 1e-9 else ""
        print(f"{r['level']:>10.2f} | {r['kept']:>5} | {r['thrown_away']:>11} | {r['let_through']:>11}{mark}")
    best = recommend(rows, current)
    print(f"\nRecommended: {best['level']:.2f} (current: {current:.2f})")
    if best["let_through"]:
        print(f"  No strictness level stops these {best['let_through']} bad answer(s): "
              f"{', '.join(best['let_through_cases'])}. They are backed by the documents but "
              "don't answer the question (or miss a required fact), which the grounding rule "
              "can't judge. Look at them in the results file: fix with a router rule, a "
              "document change, or a new test case.")
    if abs(best["level"] - current) < 1e-9:
        print("  The current setting is already the best one for these results.")
    elif "filters" in results and results["filters"]:
        print("  (This results file is from a partial run. Confirm with a full run before changing.)")
    else:
        print(f"  To use it: set MIN_GROUNDED_SHARE = {best['level']:.2f} in agent/verifier.py,"
              " then run the full test sheet again.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
