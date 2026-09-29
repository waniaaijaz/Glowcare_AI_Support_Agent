"""Unit checks for how answers are worded and verified. Uses made-up products, so it doesn't
depend on any database or on which business the agent is set up for."""

import sys
from types import SimpleNamespace
from unittest import mock

from agent import memory, router, support_agent, verifier
from agent.settings import BRAND

checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"\n      got: {detail}" if detail and not condition else ""))


def stock_reply(availability, quantity):
    product = {"product_id": "X-1", "name": "Test Serum", "price": 10, "currency": "USD",
               "stock_quantity": quantity, "availability": availability, "ingredients": []}
    route = SimpleNamespace(intents=["stock"], product_ids=["X-1"], order_id=None)
    with mock.patch.object(support_agent.db, "get_product_by_id", return_value=product):
        return support_agent._answer_from_database(route)["answer"]


def main():
    print("Prices")
    money = support_agent._money
    check("USD", money(24.99, "USD") == "$24.99", money(24.99, "USD"))
    check("PKR whole rupees with thousands separator", money(1999, "PKR") == "Rs. 1,999", money(1999, "PKR"))
    check("PKR keeps paisa when there are any", money(999.5, "PKR") == "Rs. 999.50", money(999.5, "PKR"))
    check("other currencies", money(12.5, "EUR") == "12.50 EUR", money(12.5, "EUR"))

    print("\nStock wording")
    check("known quantity", "in stock" in stock_reply("in_stock", 42) and "left" not in stock_reply("in_stock", 42))
    check("low stock says how many are left", "only 3 left" in stock_reply("in_stock", 3), stock_reply("in_stock", 3))
    check("out of stock", "out of stock" in stock_reply("out_of_stock", 0), stock_reply("out_of_stock", 0))
    reply = stock_reply("in_stock", None)
    check("available but quantity unknown: no invented number, not 'out of stock'",
          "available" in reply and "out of stock" not in reply and not any(c.isdigit() for c in reply), reply)
    reply = stock_reply("unknown", None)
    check("unknown stock: says it doesn't know and offers a team member",
          "don't have live stock" in reply and f"{BRAND} team member" in reply, reply)

    print("\nProduct question")
    few = [{"name": f"P{i}"} for i in range(5)]
    many = [{"name": f"Product {i}"} for i in range(17)]
    check("small catalogue: lists every product", all(p["name"] in router._which_product_question(few) for p in few))
    long = router._which_product_question(many)
    check("large catalogue: short list, not all 17", "Product 0" in long and "Product 16" not in long and len(long) < 300, long)

    print("\nFollow-ups never borrow another product")
    earlier = [{"sender": "assistant", "details": {"route_info": {
        "route": "database", "intents": ["price"], "product_ids": ["X-1"], "order_id": None}}}]
    catalogue = [{"product_id": "X-1", "name": "Test Serum", "ingredients": []}]

    def follow_up(text):
        route = router.Route("clarify", "price question without a product", intents=["price"],
                             clarify_question="Which product do you mean?")
        return memory.apply_memory(route, text, catalogue, earlier)[0]

    check("an unknown product name is not answered with the previous product",
          follow_up("hair oil kitne ka hai").route == "clarify", follow_up("hair oil kitne ka hai"))
    check("same in English", follow_up("how much is the hair oil").route == "clarify")
    check("same for stock questions about a product we don't have",
          memory.apply_memory(router.Route("clarify", "stock question", intents=["stock"], clarify_question="?"),
                              "is the lip balm in stock", catalogue, earlier)[0].route == "clarify")
    for text in ["how much is it", "what does it cost", "kitne ka hai", "iski price kya hai", "aur iski qeemat", "and the price?"]:
        check(f"a real follow-up still uses the last product: {text!r}",
              follow_up(text).product_ids == ["X-1"], follow_up(text))

    print("\nLanguage requests")
    aliases = {"X-1": ["test serum"]}
    with mock.patch.dict(router.PRODUCT_ALIASES, aliases):
        route = lambda text: router.route_message(text, catalogue)
        for text in ["roman me baat kro mjhse", "roman urdu mein baat karo", "urdu me jawab do", "please reply in english",
                     "can you speak urdu", "kya aap roman urdu samajhte hain", "english mein batao", "answer in roman urdu"]:
            check(f"language request is not a knowledge question: {text!r}", route(text).route == "language", route(text))
        for text, expected in [("roman me test serum ki price batao", "database"), ("urdu mein shipping policy batao", "knowledge"),
                               ("how much is the test serum in english", "database"), ("hi", "greeting"),
                               ("what is your return policy in english", "knowledge")]:
            check(f"a real question that names a language is still answered: {text!r}", route(text).route == expected, route(text))
    check("reply promises only what it can do",
          "English or Roman Urdu" in support_agent.LANGUAGE_REPLY_ON and "only reply in English" in support_agent.LANGUAGE_REPLY_OFF)
    result = {}
    with mock.patch.object(support_agent, "MATCH_CUSTOMER_LANGUAGE", False):
        result = support_agent.handle_message("roman me baat kro mjhse", verbose=False)
    check("no escalation and no case for a language request",
          result["route"] == "language" and not result["escalate"] and "only reply in English" in result["answer"], result)

    print("\nProduct names with brackets")
    bundle = [{"product_id": "B-1", "name": "Wax Quad (2 x Strips , 1 x Post Wax Oil)"},
              {"product_id": "B-2", "name": "Strips Pack"}, {"product_id": "B-3", "name": "Post Wax Oil (60ML)"}]
    aliases = {"B-1": ["wax quad"], "B-2": ["strips pack"], "B-3": ["post wax oil"]}
    with mock.patch.dict(router.PRODUCT_ALIASES, aliases):
        find = lambda text: router.find_products(text, bundle)
        check("pasting a bundle's full name finds only the bundle",
              find("how much is the Wax Quad (2 x Strips , 1 x Post Wax Oil)") == ["B-1"],
              find("how much is the Wax Quad (2 x Strips , 1 x Post Wax Oil)"))
        check("a different comma style inside the brackets still finds only the bundle",
              find("price of wax quad (2 x strips, 1 x post wax oil)") == ["B-1"],
              find("price of wax quad (2 x strips, 1 x post wax oil)"))
        check("a size in brackets is part of the product", find("post wax oil (60ml) price") == ["B-3"])
        check("bundle and one of its items asked together are both found",
              find("wax quad (2 x strips, 1 x post wax oil) and strips pack") == ["B-1", "B-2"],
              find("wax quad (2 x strips, 1 x post wax oil) and strips pack"))

    print("\nRisky claim words")
    check("'free' is rejected when the source never says it",
          any("free" in p for p in verifier.check_risky_claims("Shipping is free on all orders.", "Shipping costs a flat rate.")))
    check("'free' is fine when the source says it",
          not verifier.check_risky_claims("Shipping is free over the minimum.", "Free shipping over the minimum."))
    check("'sulfate-free' style words don't trip it when the source has them",
          not verifier.check_risky_claims("It is sulfate-free.", "A sulfate-free cleanser."))
    check("'unlimited' and 'always' are caught too",
          len(verifier.check_risky_claims("Returns are unlimited and always accepted.", "Returns within 5 days.")) == 2)
    check("verify() applies it", any("free" in p for p in verifier.verify(
        "Shipping is free.", "Shipping costs 250 rupees for all orders.", [])))

    print("\nMedical wording check")
    products = [{"product_id": "T-1", "name": "Anti-Acne Cleanser", "ingredients": []},
                {"product_id": "T-2", "name": "Face Wash", "ingredients": []}]
    context = "Anti-Acne Cleanser costs 999 rupees. Face Wash is gentle."
    check("a product's own name isn't a medical claim",
          not verifier.check_medical(verifier._without_product_names("The Anti-Acne Cleanser is available.", products)))
    check("a real medical claim is still caught even with such a product named",
          verifier.check_medical(verifier._without_product_names("The Anti-Acne Cleanser cures acne.", products)))
    check("verify() accepts an answer that only names the product",
          not verifier.verify("Anti-Acne Cleanser costs 999 rupees.", context, products),
          verifier.verify("Anti-Acne Cleanser costs 999 rupees.", context, products))
    check("verify() still rejects 'treats'",
          any("medical" in p for p in verifier.verify("The Face Wash treats eczema.", context, products)))
    check("brand words don't count as evidence in the fact-check",
          all(w in verifier.STOPWORDS for w in [BRAND.split()[0].lower()]))

    print("-" * 60)
    print(f"{sum(checks)}/{len(checks)} passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
