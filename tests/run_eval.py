"""Automated evaluation.

Runs every case in tests/eval_cases.json through the real agent (real database,
knowledge base and LLM) and reports pass/fail per case, scores per category and
hallucination numbers: 'caught' are LLM answers the fact-checker rejected (the
customer never saw them); 'delivered' are replies that reached the customer
containing something a case forbids. Delivered must be 0.

    python -m tests.run_eval                          # all cases
    python -m tests.run_eval kb- db-price             # only ids starting with these
    python -m tests.run_eval --category adversarial   # one category
    python -m tests.run_eval --no-ai                  # only cases that never call the LLM
    python -m tests.run_eval --provider openai        # use another provider for this run

Language matching is switched off for the run so replies stay in English and
facts can be compared. Results are saved to tests/results/ for tests.report.
"""

import os

# Replies stay in English so the checks below can compare exact text.
os.environ.setdefault("MATCH_CUSTOMER_LANGUAGE", "off")
os.environ.setdefault("TRANSLATE_FOR_SEARCH", "off")

import json
import sys
import time
from datetime import datetime

CASES_PATH = "tests/eval_cases.json"
RESULTS_DIR = "tests/results"
NO_AI_ROUTES = {"database", "clarify", "escalate", "greeting", "guardrail", "medical"}


def parse_args(argv):
    opts = {"filters": [], "category": None, "no_ai": False, "provider": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--category":
            opts["category"] = argv[i + 1]; i += 1
        elif a == "--provider":
            opts["provider"] = argv[i + 1]; i += 1
        elif a == "--no-ai":
            opts["no_ai"] = True
        else:
            opts["filters"].append(a)
        i += 1
    return opts


def text_problems(case: dict, text: str) -> list:
    problems, low = [], (text or "").lower()
    for t in case.get("must_include", []):
        if t.lower() not in low:
            problems.append(f"missing '{t}'")
    for group in case.get("must_include_any", []):
        if not any(t.lower() in low for t in group):
            problems.append(f"missing one of {group}")
    return problems


def forbidden_found(case: dict, text: str) -> list:
    low = (text or "").lower()
    return [t for t in case.get("must_not_include", []) if t.lower() in low]


from agent.settings import BRAND

HONEST_NOT_FOUND = ("i'm sorry, i don't have that information. "
                    f"a {BRAND.lower()} team member can help you further.")


def check_case(case: dict, result: dict) -> list:
    problems = []
    if "route" in case and result["route"] != case["route"]:
        problems.append(f"route was '{result['route']}', expected '{case['route']}'")
    if "escalate" in case and result["escalate"] != case["escalate"]:
        problems.append(f"escalate was {result['escalate']}, expected {case['escalate']}")
    problems += text_problems(case, result.get("answer"))
    problems += [f"contains forbidden '{t}'" for t in forbidden_found(case, result.get("answer"))]
    return problems


def run_case(case, handle_message, rows_for_turn):
    history = []
    for earlier in case.get("history", []):
        r = handle_message(earlier, verbose=False, history=history)
        history += rows_for_turn(earlier, r)
    return handle_message(case["question"], verbose=False, history=history)


def ai_record(case, result, verifier):
    d = result.get("details") if isinstance(result.get("details"), dict) else {}
    raw = d.get("llm_answer")
    if not raw:
        return None
    chunks = d.get("chunks_used") or []
    context = "\n\n".join(c.get("text", "") for c in chunks)
    cleaned = verifier.clean_answer(raw)
    share = verifier.grounded_share(cleaned, context)
    raw_ok = not text_problems(case, cleaned) and not forbidden_found(case, cleaned)
    return {"raw_answer": raw, "context": context, "grounded_share": share,
            "checker_problems": d.get("problems") or [],
            "rejected": bool(d.get("problems")) and d.get("answer_type") != "llm",
            "raw_answer_acceptable": raw_ok, "answer_type": d.get("answer_type")}


def main(argv):
    opts = parse_args(argv)
    if opts["provider"]:
        os.environ["LLM_PROVIDER"] = opts["provider"]
    from agent.support_agent import handle_message
    from agent.memory import rows_for_turn
    from agent import llm_client, verifier

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if opts["filters"]:
        cases = [c for c in cases if any(c["id"].startswith(x) for x in opts["filters"])]
    if opts["category"]:
        cases = [c for c in cases if c.get("category") == opts["category"]]
    if opts["no_ai"]:
        cases = [c for c in cases if c.get("route") in NO_AI_ROUTES]

    rows = []
    for case in cases:
        start = time.time()
        try:
            result = run_case(case, handle_message, rows_for_turn)
            problems = check_case(case, result)
        except Exception as e:
            result = {"route": "ERROR", "answer": f"{type(e).__name__}: {e}",
                      "source": None, "escalate": None, "details": None}
            problems = [f"crashed: {type(e).__name__}: {e}"]
        seconds = time.time() - start
        ok = not problems
        ai = ai_record(case, result, verifier)
        delivered_bad = [t for t in forbidden_found(case, result.get("answer"))
                         if t.lower() not in HONEST_NOT_FOUND]
        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        ai_down = bool(details.get("llm_error"))

        label = case["question"] if not case.get("history") else \
            " -> ".join(case["history"] + [case["question"]])
        note = "  [AI unavailable: answered from documents]" if ai_down else ""
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<28} ({seconds:4.1f}s)  {label[:90]}{note}")
        if not ok:
            print(f"      answer : {result['answer'][:220]!r}")
            for p in problems:
                print(f"      problem: {p}")
            if ai and ai["checker_problems"]:
                print(f"      checker: {'; '.join(ai['checker_problems'])}")
        rows.append({"id": case["id"], "category": case.get("category", "other"),
                     "question": case["question"], "history": case.get("history", []),
                     "safety_critical": bool(case.get("safety_critical")),
                     "passed": ok, "problems": problems, "route": result["route"],
                     "source": result.get("source"), "answer": result["answer"],
                     "delivered_forbidden": delivered_bad, "ai": ai, "ai_unavailable": ai_down,
                     "seconds": round(seconds, 1)})

    summary = summarize(rows, llm_client)
    print_summary(summary)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, datetime.now().strftime("eval_%Y%m%d_%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": rows,
                   "filters": {k: v for k, v in opts.items() if v}}, f, indent=2, default=str)
    print(f"Saved full results to {path}")
    return 0 if summary["passed"] == summary["total"] else 1


def summarize(rows, llm_client):
    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"passed": 0, "total": 0})
        c["total"] += 1
        c["passed"] += r["passed"]
    ai_rows = [r["ai"] for r in rows if r["ai"]]
    critical = [r for r in rows if r["safety_critical"]]
    cost = llm_client.estimated_cost_usd()
    return {
        "model": f"{llm_client.LLM_PROVIDER}/{llm_client.MODEL_NAME}",
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "passed": sum(r["passed"] for r in rows), "total": len(rows),
        "by_category": by_cat,
        "safety_critical": {"passed": sum(r["passed"] for r in critical), "total": len(critical)},
        "ai_answers": len(ai_rows),
        "ai_caught": sum(a["rejected"] for a in ai_rows),
        "ai_raw_unacceptable": sum(not a["raw_answer_acceptable"] for a in ai_rows),
        "ai_unavailable": sum(bool(r.get("ai_unavailable")) for r in rows),
        "ai_unavailable_cases": [r["id"] for r in rows if r.get("ai_unavailable")],
        "failed_while_ai_unavailable": [r["id"] for r in rows
                                        if r.get("ai_unavailable") and not r["passed"]],
        "delivered_forbidden": sum(bool(r["delivered_forbidden"]) for r in rows),
        "delivered_forbidden_cases": [r["id"] for r in rows if r["delivered_forbidden"]],
        "ai_calls": llm_client.usage_totals.get("calls", 0),
        "est_cost_usd": round(cost, 5) if cost is not None else None,
    }


def print_summary(s):
    print("-" * 72)
    print(f"{s['passed']}/{s['total']} passed ({s['passed'] / s['total']:.0%})" if s["total"] else "No cases matched.")
    if not s["total"]:
        return
    print("By category:")
    for cat, c in sorted(s["by_category"].items()):
        print(f"  {cat:<14} {c['passed']:>3}/{c['total']:<3} ({c['passed'] / c['total']:.0%})")
    sc = s["safety_critical"]
    if sc["total"]:
        print(f"Safety-critical cases: {sc['passed']}/{sc['total']} passed")
    if s["ai_answers"]:
        print(f"AI answers: {s['ai_answers']} | caught by the checker: {s['ai_caught']} "
              f"({s['ai_caught'] / s['ai_answers']:.0%}) | raw AI answers that would have been wrong: "
              f"{s['ai_raw_unacceptable']}")
    if s.get("ai_unavailable"):
        print(f"AI service unavailable (busy/down, even after retrying): {s['ai_unavailable']} case(s) "
              f"answered from the documents instead -> {', '.join(s['ai_unavailable_cases'])}")
        if s["failed_while_ai_unavailable"]:
            print("  These failed only because the AI was unavailable. Re-test them later with:\n"
                  f"  python -m tests.run_eval {' '.join(s['failed_while_ai_unavailable'])}")
    status = "OK" if s["delivered_forbidden"] == 0 else "PROBLEM"
    print(f"Forbidden content delivered to a customer: {s['delivered_forbidden']} [{status}]"
          + (f" -> {', '.join(s['delivered_forbidden_cases'])}" if s["delivered_forbidden"] else ""))
    line = f"Model: {s['model']} | AI calls: {s['ai_calls']}"
    if s["est_cost_usd"] is not None:
        line += f" | est. cost ${s['est_cost_usd']:.4f}"
        if s["model"].startswith("gemini"):
            line += " at paid prices ($0 on Gemini's free tier)"
    print(line)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
