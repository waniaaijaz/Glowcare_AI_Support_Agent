"""Builds docs/eval_report.md from the saved evaluation runs in tests/results/. Makes no LLM calls.

    python -m tests.report                 # newest full run, plus a comparison of models tested
    python -m tests.report <results.json>  # a specific run
"""

import glob
import json
import os
import sys
from datetime import datetime

from agent.settings import BRAND
from tests import tune_checker

REPORT_PATH = os.path.join("docs", "eval_report.md")


def full_runs():
    runs = []
    for path in sorted(glob.glob(os.path.join("tests", "results", "eval_*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "summary" in data and not data.get("filters"):
            runs.append((path, data))
    return runs


def pct(a, b):
    return f"{a / b:.0%}" if b else "–"


def build(path, data, runs):
    s, cases = data["summary"], data["cases"]
    out = []
    add = out.append
    add(f"# {BRAND} Agent — Evaluation Report\n")
    add(f"Generated {datetime.now():%d %b %Y, %H:%M} from `{path}` · model **{s['model']}** · "
        f"run at {s['run_at']}\n")

    delivered = s["delivered_forbidden"]
    add("## 1. Headline\n")
    add("| Measure | Result |")
    add("|---|---|")
    add(f"| Test cases passed | **{s['passed']}/{s['total']} ({pct(s['passed'], s['total'])})** |")
    sc = s["safety_critical"]
    add(f"| Safety-critical cases passed | **{sc['passed']}/{sc['total']} ({pct(sc['passed'], sc['total'])})** |")
    add(f"| Forbidden content delivered to a customer | **{delivered}** "
        f"{'(target met: 0)' if delivered == 0 else '(TARGET MISSED: must be 0)'} |")
    ai_n = s["ai_answers"]
    add(f"| AI answers produced | {ai_n} |")
    add(f"| AI answers the checker caught and replaced | {s['ai_caught']} ({pct(s['ai_caught'], ai_n)}) |")
    if s.get("ai_unavailable"):
        add(f"| AI service unavailable (answered from documents) | {s['ai_unavailable']}: "
            + ", ".join(f"`{c}`" for c in s["ai_unavailable_cases"]) + " |")
    add(f"| AI calls / est. cost | {s['ai_calls']} / "
        + (f"${s['est_cost_usd']:.4f}" if s["est_cost_usd"] is not None else "–")
        + (" at paid prices ($0 on Gemini's free tier)" if s["model"].startswith("gemini") else "") + " |")
    add("")
    add("*Safety-critical* = a wrong answer could mislead a customer: prices, stock, ingredients, "
        "orders, medical claims, and attempts to trick the assistant.\n")

    add("## 2. Scores per category\n")
    add("| Category | Passed | Score | What it tests |")
    add("|---|---|---|---|")
    about = {
        "database": "prices, stock, ingredients, orders: exact facts from PostgreSQL",
        "clarify": "asks ONE question instead of guessing",
        "escalation": "hands off to a human: requests, complaints, skin reactions",
        "knowledge": "policy/FAQ/product answers from documents (AI + checker)",
        "medical": "never claims a product treats/cures; no diagnoses",
        "memory": "follow-up questions use earlier messages correctly",
        "robustness": "typos, capitals, Roman Urdu",
        "adversarial": "tricks: 'ignore your instructions', fake prices, invented ingredients",
        "out_of_scope": f"questions {BRAND} can't answer: no made-up answers",
        "greeting": "hello / thanks",
    }
    for cat, c in sorted(s["by_category"].items(), key=lambda kv: kv[1]["passed"] / kv[1]["total"]):
        add(f"| {cat} | {c['passed']}/{c['total']} | {pct(c['passed'], c['total'])} | {about.get(cat, '')} |")
    add("")

    add("## 3. Hallucinations\n")
    add(f"- **Caught:** {s['ai_caught']} of {ai_n} AI answers failed a check (unsupported words, "
        "made-up numbers, wrong ingredients, medical words, fake dialogue) and were replaced by the "
        "quoted policy text or an honest \"I don't have that information\". The customer never "
        "saw them.")
    add(f"- **Raw AI answers that would have been wrong for their question:** {s['ai_raw_unacceptable']} "
        f"of {ai_n}. (Includes answers that were correct but off-topic or incomplete.)")
    add(f"- **Delivered:** {delivered}. " + ("Nothing a test case forbids reached a customer."
        if delivered == 0 else "Cases: " + ", ".join(f"`{c}`" for c in s["delivered_forbidden_cases"])))
    add("")

    records = tune_checker.collect(data)
    if records:
        try:
            from agent.support_agent import get_products
            rows = tune_checker.sweep(records, get_products())
            from agent import verifier
            current = verifier.MIN_GROUNDED_SHARE
            best = tune_checker.recommend(rows, current)
            add("## 4. Checker strictness (grounding rule)\n")
            add("Each AI answer from this run replayed through the checker at different strictness "
                "levels (no new AI calls).\n")
            add("| Strictness | Good answers kept | Good answers thrown away | Bad answers let through |")
            add("|---|---|---|---|")
            for r in rows:
                if r["level"] in (0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8) or abs(r["level"] - current) < 1e-9:
                    mark = " ← current" if abs(r["level"] - current) < 1e-9 else ""
                    add(f"| {r['level']:.2f}{mark} | {r['kept']} | {r['thrown_away']} | {r['let_through']} |")
            add("")
            add(f"Recommended: **{best['level']:.2f}** (current {current:.2f}). "
                + ("Keep the current setting." if abs(best["level"] - current) < 1e-9
                   else f"Consider changing MIN_GROUNDED_SHARE to {best['level']:.2f} and re-running."))
            if best["let_through"]:
                add(f" {best['let_through']} bad answer(s) pass at every level. They are backed by the "
                    "documents but don't answer the question: " + ", ".join(f"`{c}`" for c in best["let_through_cases"]) + ".")
            add("")
        except Exception as e:
            add(f"## 4. Checker strictness\n\n(Skipped: {type(e).__name__}: {e})\n")

    fails = [c for c in cases if not c["passed"]]
    add(f"## 5. Failing cases ({len(fails)})\n")
    if not fails:
        add("None.\n")
    else:
        add("| Case | Category | Question | Problem | Reply (start) |")
        add("|---|---|---|---|---|")
        for c in sorted(fails, key=lambda c: (not c["safety_critical"], c["category"], c["id"])):
            q = (" → ".join(c.get("history", []) + [c["question"]])).replace("|", "/")
            reply = (c["answer"] or "")[:90].replace("|", "/").replace("\n", " ")
            flag = " ⚠" if c["safety_critical"] else ""
            add(f"| `{c['id']}`{flag} | {c['category']} | {q} | {'; '.join(c['problems'])[:120]} | {reply} |")
        add("\n⚠ = safety-critical.\n")
        down = s.get("failed_while_ai_unavailable") or []
        if down:
            add("Failed only because the AI service was unavailable during the run (the customer "
                "got the quoted policy text or an honest \"I don't have that\"): "
                + ", ".join(f"`{c}`" for c in down) + ". Re-test later with "
                f"`python -m tests.run_eval {' '.join(down)}`.\n")

    latest = {}
    for p, d in runs:
        latest[d["summary"]["model"]] = (p, d["summary"])
    if len(latest) >= 1:
        add("## 6. Model comparison (newest full run of each model)\n")
        add("| Model | Passed | Safety-critical | Knowledge | Caught | Delivered | Cost |")
        add("|---|---|---|---|---|---|---|")
        for model, (p, m) in sorted(latest.items()):
            kb = m["by_category"].get("knowledge", {"passed": 0, "total": 0})
            cost = f"${m['est_cost_usd']:.4f}" if m.get("est_cost_usd") is not None else "free/local"
            add(f"| {model} | {m['passed']}/{m['total']} | {m['safety_critical']['passed']}/"
                f"{m['safety_critical']['total']} | {kb['passed']}/{kb['total']} | "
                f"{m['ai_caught']}/{m['ai_answers']} | {m['delivered_forbidden']} | {cost} |")
        add("")
        if len(latest) == 1:
            add("Only one model tested so far. Compare with e.g. "
                "`python -m tests.run_eval --provider openai`, then run this report again.\n")
    return "\n".join(out)


def main(argv):
    runs = full_runs()
    if argv:
        path = argv[0]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    elif runs:
        path, data = runs[-1]
    else:
        print("No full test run saved yet. Run: python -m tests.run_eval")
        return 1
    if "summary" not in data:
        print(f"{path} is from an older version of the test sheet. Run: python -m tests.run_eval")
        return 1
    report = build(path, data, runs)
    os.makedirs("docs", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    s = data["summary"]
    print(f"Wrote {REPORT_PATH}: {s['passed']}/{s['total']} passed, "
          f"{s['delivered_forbidden']} forbidden delivered, model {s['model']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
