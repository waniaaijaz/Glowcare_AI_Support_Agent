"""Entry point for a customer message: handle_message().

The router picks a path: database facts, knowledge-base answer, clarifying
question, human escalation or a fixed reply. Database answers are built from
fixed sentences, so a price, stock level or order status can't be misread or
invented. The LLM only writes where wording needs flexibility (policy answers)
and, for non-English messages, rewords the final reply into the customer's
language.
"""

from database import db
from agent.router import route_message, GUARDRAIL_REPLY, MEDICAL_REPLY
from agent.memory import apply_memory, rows_for_turn
import sys

from agent.llm_client import (call_llm, LLMUnavailableError, MATCH_CUSTOMER_LANGUAGE,
                              TRANSLATE_RETRIES, TRANSLATE_TIMEOUT)
from agent.settings import BRAND, EXAMPLE_INGREDIENT_PRODUCT, EXAMPLE_ORDER_ID, EXAMPLE_PRODUCT
from agent import responder

# Earlier messages (customer and assistant) that memory looks at.
MEMORY_MESSAGES = 6

_products_cache = None


def get_products():
    """Product list, loaded once per process (for name matching and ingredient checks)."""
    global _products_cache
    if _products_cache is None:
        _products_cache = db.get_all_products()
    return _products_cache


# The Windows console defaults to cp1252, so printing an Urdu-script message or reply in the logs
# above would raise UnicodeEncodeError and turn the customer's reply into a 500 error.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

GREETING_REPLY = (f"Hi! I'm the {BRAND} assistant. I can help with our products, "
                  "prices, stock, your orders, and our shipping, return and refund "
                  "policies. What can I help you with?")
THANKS_REPLY = "You're welcome! Is there anything else I can help you with?"

# Asked which language to use. With language matching on, this English text is reworded into the
# customer's language like every other fixed reply, so it comes out in Roman Urdu for a Roman Urdu message.
LANGUAGE_REPLY_ON = ("Of course! I reply in the language you write in, English or Roman Urdu. "
                     "What would you like to know about our products, prices, orders, shipping or returns?")
LANGUAGE_REPLY_OFF = ("Sorry, I can only reply in English for now. "
                      "What would you like to know about our products, prices, orders, shipping or returns?")

# No escalation and no case number here: an off-topic question isn't a support need, so opening a
# case for it would be noise for the team and a confusing reference number for the customer.
OFF_TOPIC_REPLY = (f"I'm the {BRAND} support assistant, so I can only help with our products, prices, "
                   "orders, and shipping or return policies. What can I help you with?")

ESCALATION_REPLIES = {
    "customer asked for a human":
        f"Of course. I'm passing your conversation to a {BRAND} team member, "
        "who will follow up with you shortly.",
    "possible skin reaction / safety concern":
        "I'm sorry you're dealing with this. Because it may be a reaction to a "
        f"product, I'm passing your message to a {BRAND} team member right away "
        "so a person can help you.",
    "complaint":
        f"I'm sorry about your experience. I'm passing this to a {BRAND} team "
        "member so a person can look into it and follow up with you.",
}


# How urgent each hand-off is. The team works 'high' first.
ESCALATION_PRIORITY = {
    "possible skin reaction / safety concern": "high",
    "customer asked for a human": "normal",
    "complaint": "normal",
    "information not available": "low",
}


def _format_ref(escalation_id: int) -> str:
    return f"ESC-{escalation_id}"


def _record_escalation(result: dict, conversation_id: int, verbose: bool):
    """Save the hand-off so a person follows up, and add the reference number to the reply.

    One open case per chat: a repeat request reuses it, and a more urgent one (for
    example a rash reported after asking for a human) raises its priority. If the
    database write fails the customer still gets a reply, without a reference number.
    """
    reason = result["escalation_reason"]
    priority = ESCALATION_PRIORITY.get(reason, "normal")
    try:
        existing = db.get_open_escalation(conversation_id)
        if existing:
            escalation_id = existing["escalation_id"]
            if db.PRIORITY_RANK[priority] > db.PRIORITY_RANK.get(existing["priority"], 1):
                db.raise_escalation_priority(escalation_id, priority, reason, result["message"])
                result["answer"] += (f" I've marked your case {_format_ref(escalation_id)} "
                                     "as urgent for our team.")
            else:
                priority = existing["priority"]
                result["answer"] += (f" Your case is already with our team "
                                     f"(reference {_format_ref(escalation_id)}).")
            result["escalation_new"] = False
        else:
            escalation_id = db.create_escalation(conversation_id, reason, priority,
                                                 result["message"])
            result["answer"] += f" Your reference number is {_format_ref(escalation_id)}."
            result["escalation_new"] = True
        result["escalation_id"] = escalation_id
        result["escalation_priority"] = priority
        if verbose:
            state = "created" if result["escalation_new"] else "already open"
            print(f"[ESCALATION] {_format_ref(escalation_id)} {state} "
                  f"(priority {priority}, reason: {reason})")
    except Exception as e:
        result["escalation_error"] = f"{type(e).__name__}: {e}"
        result["answer"] += (" If you don't hear from us soon, please contact "
                             f"{BRAND} support directly.")
        if verbose:
            print(f"[ESCALATION] FAILED to save: {result['escalation_error']}")


def _money(amount, currency):
    """Format a price for the customer."""
    if currency == "USD":
        return f"${amount:.2f}"
    if currency == "PKR":
        return f"Rs. {amount:,.0f}" if float(amount).is_integer() else f"Rs. {amount:,.2f}"
    return f"{amount:.2f} {currency}"


def _match_customer_language(customer_message: str, english_text: str) -> str:
    """Reword an already-correct English reply in the customer's language.

    Every fact, number and name was decided by the database or a fixed template
    before this runs; the LLM only changes the wording. On any failure (or when
    MATCH_CUSTOMER_LANGUAGE=off) the English text is returned unchanged.
    """
    if not MATCH_CUSTOMER_LANGUAGE or not english_text:
        return english_text
    prompt = (f"Customer's message: {customer_message}\n\n"
              f"Reply (do not change any number, name, date or fact in it): "
              f"{english_text}\n\n"
              f"Rewrite the reply in the SAME language and script the "
              f"customer used. This includes short greetings and single "
              f"words in Roman Urdu (Urdu spelled with English letters) "
              f"such as \"salam\", \"assalam o alaikum\", \"shukriya\" or "
              f"\"kaisay hain\" \u2014 those count as Roman Urdu, not English, "
              f"so reply in Roman Urdu (Latin letters, never Urdu script) "
              f"for them too. Only leave the reply unchanged if the "
              f"customer actually wrote in English. Output ONLY the "
              f"rewritten reply, nothing else, no quotes around it.")
    system_prompt = ("You are a precise translator. Never add, remove or "
                     "change any fact, number or name \u2014 only translate "
                     "the language.")
    # Gemini often takes 4-10s for this and occasionally drops the connection
    # mid-request, so one retry catches a dropped connection without making
    # every reply wait through the slower, more patient retry schedule.
    for attempt in range(1 + TRANSLATE_RETRIES):
        try:
            translated = call_llm(prompt=prompt, system_prompt=system_prompt,
                                  timeout=TRANSLATE_TIMEOUT, patient=False)
            translated = translated.strip().strip('"').strip()
            if translated:
                return translated
            print(f"[LANGUAGE] Empty rewording for {customer_message!r}, keeping English")
            return english_text
        except LLMUnavailableError as e:
            if attempt < TRANSLATE_RETRIES:
                print(f"[LANGUAGE] Rewording attempt {attempt + 1} failed, retrying: {e}")
                continue
            print(f"[LANGUAGE] Rewording failed for {customer_message!r}, keeping English: {e}")
        except Exception as e:
            print(f"[LANGUAGE] Rewording failed for {customer_message!r}, keeping English: {e}")
            break
    return english_text


def _answer_from_database(route) -> dict:
    """Build the reply for order, price, stock and ingredient questions from database rows only.
    """
    if "order_status" in route.intents:
        order = db.get_order_status(route.order_id)
        if order is None:
            return {
                "answer": (f"I couldn't find an order with the number {route.order_id}. "
                           "Please double-check the number from your order confirmation, "
                           "or I can connect you with a team member."),
                "facts": {"order_id": route.order_id, "found": False},
            }
        items = ", ".join(f"{i['quantity']} x {i['product_name']}" for i in order["items"])
        return {
            "answer": (f"Order {order['order_id']} is currently {order['status']}. "
                       f"It contains: {items}."),
            "facts": order,
        }

    sentences, facts = [], []
    for pid in route.product_ids:
        product = db.get_product_by_id(pid)
        if product is None:
            continue
        facts.append({k: product.get(k) for k in
                      ("product_id", "name", "price", "currency",
                       "stock_quantity", "availability", "ingredients")})
        name = product["name"]
        if "price" in route.intents:
            sentences.append(f"The {name} costs {_money(float(product['price']), product['currency'])}.")
        if "stock" in route.intents:
            quantity = product["stock_quantity"]
            if product["availability"] == "unknown":
                sentences.append(f"I don't have live stock information for the {name}. "
                                 f"A {BRAND} team member can confirm it for you.")
            elif product["availability"] == "in_stock" and quantity is None:
                sentences.append(f"The {name} is currently available.")
            elif product["availability"] == "in_stock" and quantity > 0:
                extra = f" (only {quantity} left)" if quantity < 10 else ""
                sentences.append(f"The {name} is in stock{extra}.")
            else:
                sentences.append(f"The {name} is currently out of stock.")
        if "ingredients" in route.intents:
            ingredients = product.get("ingredients") or []
            if ingredients:
                sentences.append(f"The {name} contains: {', '.join(ingredients)}.")
            else:
                sentences.append(f"I don't have the ingredient list for the {name}. "
                                 f"A {BRAND} team member can help you further.")
    return {"answer": " ".join(sentences) or responder.NOT_FOUND_REPLY, "facts": facts}


def _load_history(conversation_id, history, verbose):
    """Earlier messages for memory: the caller's history if given, otherwise from the database.
    """
    if history is not None:
        return history[-MEMORY_MESSAGES:]
    if conversation_id is None:
        return []
    try:
        return db.get_recent_turns(conversation_id, MEMORY_MESSAGES)
    except Exception as e:
        if verbose:
            print(f"[MEMORY] couldn't load earlier messages: {type(e).__name__}: {e}")
        return []


def _log_turn(conversation_id, message, result, verbose):
    """Save the customer message and the reply. A logging failure never blocks the reply.
    """
    try:
        for row in rows_for_turn(message, result):
            db.log_message(conversation_id, **row)
    except Exception as e:
        result["logging_error"] = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"[LOG] FAILED to save messages: {result['logging_error']}")


def handle_message(message: str, conversation_id: int = None, verbose: bool = True,
                   history: list = None) -> dict:
    """Handle one customer message and return a result dict.

    Pass `conversation_id` to save the chat and enable follow-up memory. The result
    includes the answer, route, source, escalation details and the audit data.
    """
    products = get_products()
    earlier = _load_history(conversation_id, history, verbose)
    route = route_message(message, products)
    route, memory_note = apply_memory(route, message, products, earlier)
    if verbose:
        print(f"\n[ROUTER] {message!r} -> {route.route} ({route.reason})"
              + (f" products={route.product_ids}" if route.product_ids else "")
              + (f" order={route.order_id}" if route.order_id else ""))
        if memory_note:
            print(f"[MEMORY] {memory_note}")

    result = {"message": message, "route": route.route, "reason": route.reason,
              "intents": route.intents, "product_ids": route.product_ids,
              "order_id": route.order_id, "memory": memory_note,
              "escalate": False, "escalation_reason": None, "escalation_id": None,
              "details": None}

    if route.route == "escalate":
        result.update(answer=ESCALATION_REPLIES[route.reason], source="fixed_reply",
                      escalate=True, escalation_reason=route.reason)

    elif route.route == "guardrail":
        result.update(answer=GUARDRAIL_REPLY, source="fixed_reply")

    elif route.route == "medical":
        result.update(answer=MEDICAL_REPLY, source="fixed_reply")

    elif route.route == "off_topic":
        result.update(answer=OFF_TOPIC_REPLY, source="fixed_reply")

    elif route.route == "language":
        result.update(answer=LANGUAGE_REPLY_ON if MATCH_CUSTOMER_LANGUAGE else LANGUAGE_REPLY_OFF,
                      source="fixed_reply")

    elif route.route == "greeting":
        is_thanks = any(w in message.lower() for w in
                        ("thank", "thx", "shukri", "jazakallah", "meherbani"))
        result.update(answer=THANKS_REPLY if is_thanks else GREETING_REPLY,
                      source="fixed_reply")

    elif route.route == "clarify":
        result.update(answer=route.clarify_question, source="fixed_reply")

    elif route.route == "database":
        db_result = _answer_from_database(route)
        result.update(answer=db_result["answer"], source="database",
                      details=db_result["facts"])
        if verbose:
            print(f"[DB] {db_result['facts']}")

    else:
        kb = responder.answer_question(message, products=products, verbose=verbose)
        not_found = kb["answer_type"] == "not_found"
        result.update(answer=kb["answer"], source="knowledge_base", details=kb,
                      escalate=not_found,
                      escalation_reason="information not available" if not_found else None)

    if result["escalate"] and conversation_id is not None:
        _record_escalation(result, conversation_id, verbose)

    if conversation_id is not None:
        _log_turn(conversation_id, message, result, verbose)

    # Knowledge-base answers already come back in the customer's language. Every other reply is
    # built in English, so it is reworded here, after logging, so the saved chat stays in English.
    if route.route != "knowledge":
        result["answer"] = _match_customer_language(message, result["answer"])

    if verbose:
        print(f"[REPLY] ({result['source']}{', ESCALATE' if result['escalate'] else ''}) "
              f"{result['answer']}")
    return result


if __name__ == "__main__":
    demo = [
        f"What ingredients are in the {EXAMPLE_INGREDIENT_PRODUCT}?",
        f"How much is the {EXAMPLE_PRODUCT} and is it in stock?",
        f"Where is my order {EXAMPLE_ORDER_ID}?",
        "Can I return a product after opening it?",
        "Do you sell products in the UK?",
        "How much does it cost?",
        "I got a rash after using the cleanser",
    ]
    for q in demo:
        print("=" * 70)
        handle_message(q)
