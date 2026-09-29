"""Checks what happens when the LLM service is slow or down: retries on 5xx, rate limits
and timeouts, then a fallback to quoted source text or 'not available'.
Uses fake HTTP responses, so no real API calls are made.
"""

import sys
from unittest import mock

import requests

from agent import llm_client, responder
from agent.llm_client import LLMUnavailableError

checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {"error": {"message": f"simulated {status}"}}
        self.text = str(self._body)

    def json(self):
        return self._body


def ok_response(text="Unopened products can be returned within 30 days."):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def call_api(responses, patient=True):
    responses = list(responses)
    calls, waits = [], []

    def fake_post(*args, **kwargs):
        calls.append(1)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    with mock.patch.object(llm_client.requests, "post", side_effect=fake_post), \
         mock.patch.object(llm_client.time, "sleep", side_effect=waits.append), \
         mock.patch("builtins.print"):
        try:
            out = llm_client._call_chat_api("gemini", "https://example.invalid", "test-key",
                                            "gemini-3.1-flash-lite", "prompt", "", 5, patient)
        except Exception as e:
            out = e
    return out, len(calls), waits


CHUNKS = [
    {"text": "GlowCare Return Policy > Condition for Returns\n"
             "Products must be unopened and unused to be returned within 30 days.",
     "source": "return_policy.md", "distance": 0.31,
     "section": "Return Policy > Condition for Returns"},
]


def ask_during_outage(chunks):
    fake_retrieval = {"context": "\n\n".join(c["text"] for c in chunks), "chunks": chunks}
    with mock.patch.object(responder, "retrieve", return_value=fake_retrieval), \
         mock.patch.object(responder, "call_llm",
                           side_effect=LLMUnavailableError("gemini returned error 503 (simulated)")):
        return responder.answer_question("Can I return a product after opening it?", verbose=False)


def main():
    out, n, waits = call_api([FakeResponse(503), ok_response()], patient=False)
    check("optional call (patient=False): one attempt, no waiting, error raised at once",
          isinstance(out, LLMUnavailableError) and n == 1 and waits == [], f"{out!r}, {n} requests, waits {waits}")
    out, n, waits = call_api([FakeResponse(429), ok_response()], patient=False)
    check("optional call also gives up at once on a rate limit", isinstance(out, Exception) and n == 1 and waits == [], f"{out!r}, {n}, {waits}")
    check("waiting is short enough for a live chat",
          sum(llm_client.OVERLOAD_WAITS) <= 20 and sum(llm_client.RATE_LIMIT_WAITS) <= 40)

    out, n, waits = call_api([FakeResponse(503), ok_response()])
    check("503 then OK -> retried once, answer returned", isinstance(out, str) and n == 2,
          f"got {out!r} after {n} requests")
    check("waits before retrying a busy AI", waits == llm_client.OVERLOAD_WAITS[:1], f"waits {waits}")

    for status in (500, 502, 504):
        out, n, _ = call_api([FakeResponse(status), ok_response()])
        check(f"{status} is retried", isinstance(out, str) and n == 2, f"{out!r}, {n} requests")
    out, n, _ = call_api([requests.exceptions.Timeout("simulated"), ok_response()])
    check("timeout is retried", isinstance(out, str) and n == 2, f"{out!r}, {n} requests")

    out, n, _ = call_api([FakeResponse(503)] * 10)
    check("503 on every try -> LLMUnavailableError",
          isinstance(out, LLMUnavailableError), f"got {type(out).__name__}")
    check("gives up after a fixed number of tries", n == len(llm_client.OVERLOAD_WAITS) + 1, f"{n} requests")
    out, _, _ = call_api([requests.exceptions.ConnectionError("simulated")] * 10)
    check("no internet on every try -> LLMUnavailableError",
          isinstance(out, LLMUnavailableError), f"got {type(out).__name__}")

    out, n, waits = call_api([FakeResponse(429), ok_response()])
    check("429 is retried with the rate-limit wait",
          isinstance(out, str) and waits == llm_client.RATE_LIMIT_WAITS[:1], f"waits {waits}")

    for status in (400, 401, 404):
        out, n, _ = call_api([FakeResponse(status), ok_response()])
        check(f"{status} (setup error) fails at once, loudly",
              isinstance(out, RuntimeError) and not isinstance(out, LLMUnavailableError) and n == 1,
              f"got {type(out).__name__} after {n} requests")

    kb = ask_during_outage(CHUNKS)
    check("AI down -> policy text quoted instead of a crash",
          kb["answer_type"] == "quoted_source" and "unopened" in kb["answer"], kb["answer"][:80])
    check("the outage is recorded for the dashboard", bool(kb["llm_error"]) and kb["llm_answer"] is None)
    check("the reason is logged", any("unavailable" in p for p in kb["problems"]), kb["problems"])

    weak = [dict(CHUNKS[0], distance=0.95)]
    kb = ask_during_outage(weak)
    check("AI down + weak match -> 'I don't have that'",
          kb["answer_type"] == "not_found" and kb["answer"] == responder.NOT_FOUND_REPLY, kb["answer"][:80])

    from agent import support_agent
    fake = {"question": "q", "chunks_used": [], "prompt_sent_to_llm": "p", "llm_answer": None,
            "problems": ["AI service unavailable (simulated)"], "answer": responder.NOT_FOUND_REPLY,
            "answer_type": "not_found", "llm_error": "simulated"}
    with mock.patch.object(support_agent.responder, "answer_question", return_value=fake):
        r = support_agent.handle_message("Can I return a product after opening it?", verbose=False,
                                         history=[])
    check("full agent: knowledge question during an outage doesn't crash",
          r["route"] == "knowledge" and r["answer"] == responder.NOT_FOUND_REPLY)
    check("full agent: 'I don't have that' during an outage still flags a human", r["escalate"])

    from tests.run_eval import HONEST_NOT_FOUND
    check("test sheet knows the exact 'I don't have that' reply",
          HONEST_NOT_FOUND == responder.NOT_FOUND_REPLY.lower())

    passed = sum(checks)
    print("-" * 60)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
