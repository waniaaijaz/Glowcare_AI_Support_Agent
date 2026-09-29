"""Checks the language layer with a fake LLM (no API calls).

Replies are reworded into the customer's language only when MATCH_CUSTOMER_LANGUAGE
is on, the saved chat history stays in English, and any failure falls back to
the original English text. Also checks Roman Urdu routing.
"""

import sys
from unittest import mock

from database import db
from agent.llm_client import LLMUnavailableError
from agent import responder, support_agent
from agent.router import route_message
from agent.settings import EXAMPLE_ORDER_ID, EXAMPLE_PRODUCT

checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"\n      got: {detail}" if detail and not condition else ""))


def cleanup(conversation_id):
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM escalations WHERE conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM conversations WHERE conversation_id = %s", (conversation_id,))
        conn.commit()


def main():
    # Plain fixture text for testing the rewording mechanism in isolation -- not tied to any
    # real product, so this file has no brand-specific text and works unchanged for any business.
    english = "The Test Product costs $19.99."
    urdu = "Test Product ki qeemat $19.99 hai."

    print("Reply rewording")
    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", return_value=f"  {urdu}  ") as llm:
        out = support_agent._match_customer_language("serum ki price kya hai", english)
        prompt = llm.call_args.kwargs["prompt"]
    check("reply is replaced by the LLM's rewording", out == urdu, out)
    check("the LLM sees the customer's message and the exact English reply",
          "serum ki price kya hai" in prompt and english in prompt)

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", side_effect=RuntimeError("down")):
        out = support_agent._match_customer_language("serum ki price kya hai", english)
    check("LLM error -> English reply is kept", out == english, out)

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", return_value="   "):
        out = support_agent._match_customer_language("hi", english)
    check("empty LLM reply -> English reply is kept", out == english, out)

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", False), \
         mock.patch.object(support_agent, "call_llm", return_value=urdu) as llm:
        out = support_agent._match_customer_language("serum ki price kya hai", english)
    check("switched off -> no LLM call, English reply", out == english and not llm.called, out)

    print("\nNever slows the customer down")
    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", return_value=urdu) as llm:
        support_agent._match_customer_language("hi", english)
    check("rewording is an impatient call (no retry waits)", llm.call_args.kwargs.get("patient") is False, llm.call_args.kwargs)
    with mock.patch.object(responder, "TRANSLATE_FOR_SEARCH", True), \
         mock.patch.object(responder, "call_llm", return_value="x") as llm:
        responder._translate_for_search("hi")
    check("search translation is an impatient call too", llm.call_args.kwargs.get("patient") is False, llm.call_args.kwargs)

    print("\nFull agent")
    chat_id = db.create_conversation(channel="test")
    try:
        real_price = db.get_product_price(EXAMPLE_PRODUCT)
        with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
             mock.patch.object(support_agent, "call_llm", return_value=urdu):
            result = support_agent.handle_message(f"{EXAMPLE_PRODUCT.lower()} ki price kya hai",
                                                  conversation_id=chat_id, verbose=False)
        check("customer receives the reworded reply", result["answer"] == urdu, result["answer"])
        saved = [m for m in db.get_conversation_messages(chat_id) if m["sender"] == "assistant"]
        price_text = support_agent._money(real_price["price"], real_price["currency"])
        check("saved chat keeps the English reply for the team",
              saved and price_text in saved[0]["content"] and saved[0]["content"] != urdu,
              saved[0]["content"] if saved else None)

        stub = {"answer": "Unopened products can be returned within 30 days.", "answer_type": "llm",
                "chunks_used": [], "problems": [], "llm_error": None}
        with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
             mock.patch.object(support_agent, "call_llm", return_value="SHOULD NOT BE USED") as llm, \
             mock.patch.object(responder, "answer_question", return_value=stub):
            result = support_agent.handle_message("what is your return policy", verbose=False)
        check("knowledge answers are not reworded a second time",
              result["answer"] == stub["answer"] and not llm.called, result["answer"])
    finally:
        cleanup(chat_id)

    print("\nRetries a dropped connection once, then gives up")
    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm",
                            side_effect=[LLMUnavailableError("dropped"), urdu]) as llm:
        out = support_agent._match_customer_language("salam", english)
    check("recovers on the retry after one dropped connection", out == urdu and llm.call_count == 2, out)

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm",
                            side_effect=LLMUnavailableError("dropped")) as llm:
        out = support_agent._match_customer_language("salam", english)
    check("gives up and keeps English after the connection keeps dropping",
          out == english and llm.call_count == 2, f"{out!r}, {llm.call_count} attempts")

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", side_effect=RuntimeError("other error")) as llm:
        out = support_agent._match_customer_language("salam", english)
    check("a non-connection error is not retried", out == english and llm.call_count == 1, llm.call_count)

    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", True), \
         mock.patch.object(support_agent, "call_llm", return_value='"Salam!"'):
        out = support_agent._match_customer_language("salam", "Hi!")
    check("stray quotes around the rewording are stripped", out == "Salam!", out)

    print("\nSearch translation and reply parsing")
    with mock.patch.object(responder, "TRANSLATE_FOR_SEARCH", True), \
         mock.patch.object(responder, "call_llm", return_value=" What is the shipping policy? "):
        out = responder._translate_for_search("shipping policy kia hai apki")
    check("question is restated in English for the search", out == "What is the shipping policy?", out)
    with mock.patch.object(responder, "TRANSLATE_FOR_SEARCH", True), \
         mock.patch.object(responder, "call_llm", side_effect=RuntimeError("down")):
        out = responder._translate_for_search("shipping policy kia hai apki")
    check("translation failure -> original question is searched", out == "shipping policy kia hai apki", out)
    with mock.patch.object(responder, "TRANSLATE_FOR_SEARCH", False), \
         mock.patch.object(responder, "call_llm", return_value="x") as llm:
        out = responder._translate_for_search("shipping policy kia hai apki")
    check("switched off -> no LLM call", out == "shipping policy kia hai apki" and not llm.called)
    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", False), \
         mock.patch.object(responder, "TRANSLATE_FOR_SEARCH", True), \
         mock.patch.object(responder, "call_llm", return_value="What is the return policy?") as llm:
        out = responder._translate_for_search("kia me product kholne k bad return krskta hun")
    check("search translation still works when replies are kept in English",
          out == "What is the return policy?" and llm.called, out)

    parse = responder._parse_llm_reply
    check("JSON reply is split into customer and English versions",
          parse('{"answer": "Wapsi 30 din mein.", "answer_english": "Returns within 30 days."}')
          == ("Wapsi 30 din mein.", "Returns within 30 days."))
    check("JSON inside a markdown fence is understood",
          parse('```json\n{"answer": "Yes.", "answer_english": "Yes."}\n```') == ("Yes.", "Yes."))
    check("plain text (no JSON) is used for both",
          parse("Returns within 30 days.") == ("Returns within 30 days.", "Returns within 30 days."))
    check("missing English copy falls back to the answer",
          parse('{"answer": "Yes."}') == ("Yes.", "Yes."))

    print("\nRoman Urdu routing")
    products = db.get_all_products()
    product_phrase = EXAMPLE_PRODUCT.lower()
    order_word = EXAMPLE_ORDER_ID[:2] + "99999"
    cases = [
        (f"{product_phrase} stock mein hai ya nahi", "database", "stock"),
        (f"{product_phrase} kis se bana hai", "database", "ingredients"),
        (f"{product_phrase} kitne ka hai", "database", "price"),
        (f"mera order {order_word} kahan hai", "database", "order_status"),
        ("wapsi ki policy kya hai", "knowledge", None),
        ("mask ne kaam nahi kiya", "escalate", None),
        ("apka mask kaam nahi kara mujhpe", "escalate", None),
        ("cream ka koi asar nahi hua", "escalate", None),
        ("mujhe insaan se baat karni hai", "escalate", None),
        ("cleanser lagane se kharish ho rahi hai", "escalate", None),
    ]
    for text, route, intent in cases:
        r = route_message(text, products)
        ok = r.route == route and (intent is None or intent in r.intents)
        check(f"{text!r} -> {route}" + (f" ({intent})" if intent else ""), ok, f"{r.route} {r.intents}")

    print("-" * 60)
    print(f"{sum(checks)}/{len(checks)} passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
