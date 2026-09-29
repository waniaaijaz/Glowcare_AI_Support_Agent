"""Conversation memory for follow-up questions.

Resolves short follow-ups against the last few turns: 'how much is it?' after a
product was discussed, 'and the sunscreen?' after a stock question, or a bare
product name after 'which product do you mean?'. It returns a corrected Route
and a note describing what was reused, which is saved in the audit trail.
"""

import re

from agent.router import Route, PRODUCT_ALIASES, POLICY_TOPIC_PATTERNS

# How many earlier assistant turns to search for a product.
PRODUCT_LOOKBACK_TURNS = 3

PRONOUN_PATTERN = (r"\b(it|its|it's|this|that|these|those|them|they|one|"
                   r"iska|iski|iske|isko|isay|uska|uski|uske|yeh|ye|woh|wo)\b")
FOLLOW_UP_PATTERN = r"^\s*(and|what about|how about|also|same for|and what about)\b"
FILLER_WORDS = {"the", "a", "an", "for", "of", "please", "pls", "one", "my", "i", "mean",
                "want", "about", "that", "this", "is", "it", "its", "ok", "okay",
                "and", "what", "how", "also", "same"}

# Words that ask about a product without naming one ("how much is it?", "kitne ka hai?").
# A message with any other word may be naming a product we don't know, so it must not reuse the last one.
QUESTION_WORDS = {"price", "cost", "costs", "much", "many", "stock", "available", "availability",
                  "ingredients", "ingredient", "contain", "contains", "have", "has", "does", "do", "are",
                  "you", "me", "tell", "show", "s", "what's", "whats", "how's", "which", "in",
                  "kitna", "kitne", "kitni", "ka", "ki", "ke", "hai", "hain", "kya", "aur",
                  "qeemat", "qimat", "mojood", "batao", "batain", "btao", "bataye", "mein", "me", "ab"}

PRODUCT_INTENTS = ("price", "stock", "ingredients")


def turn_info(row: dict):
    """Route details saved with an assistant message (None for customer messages)."""
    if not row or row.get("sender") != "assistant":
        return None
    details = row.get("details") or {}
    return details.get("route_info")


def _assistant_turns(recent_turns: list) -> list:
    """Route details of earlier assistant turns, newest first."""
    infos = [turn_info(r) for r in recent_turns]
    return [i for i in reversed(infos) if i]


def _topic_changed(info: dict) -> bool:
    """True if this turn moved the chat to a new topic, so older products should not be reused.
    """
    if info.get("route") in ("greeting", "guardrail", "medical", "language", "off_topic"):
        return False
    if info.get("route") == "clarify":
        return "order_status" in info.get("intents", [])
    return True


def _names_something_else(message: str) -> bool:
    """True if the message has a word beyond question and filler words, such as "hair oil" in
    "hair oil kitne ka hai". That is a product we didn't recognise, not a follow-up.
    """
    words = re.findall(r"[a-z']+", message.lower())
    return any(w not in FILLER_WORDS and w not in QUESTION_WORDS for w in words)


def _is_bare_product_reply(message: str, products: list) -> bool:
    """True if the message is only a product name plus filler words ('the cleanser please').
    """
    text = message.lower()
    for p in products:
        for name in [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], []):
            text = re.sub(r"\b" + re.escape(name) + r"\b", " ", text)
    leftover = [w for w in re.findall(r"[a-z']+", text) if w not in FILLER_WORDS]
    return not leftover


def apply_memory(route: Route, message: str, products: list, recent_turns: list):
    """Rewrite `route` using earlier turns when the message is a follow-up.

    Returns (route, note). The note is None when nothing was reused.
    """
    if not recent_turns or route.route in ("escalate", "greeting", "guardrail", "medical", "language", "off_topic"):
        return route, None
    if any(re.search(p, message, flags=re.IGNORECASE) for p in POLICY_TOPIC_PATTERNS):
        return route, None

    earlier = _assistant_turns(recent_turns)
    if not earlier:
        return route, None
    last = earlier[0]
    last_product_intents = [i for i in last.get("intents", []) if i in PRODUCT_INTENTS]

    if (last.get("route") == "clarify" and last_product_intents
            and route.product_ids and not [i for i in route.intents if i in PRODUCT_INTENTS]):
        return (Route("database", f"{'/'.join(last_product_intents)} question about a known product",
                      intents=last_product_intents, product_ids=route.product_ids),
                "answered the earlier clarifying question: used its "
                f"{'/'.join(last_product_intents)} question with the product named now")

    asked = [i for i in route.intents if i in PRODUCT_INTENTS]
    points_back = (re.search(PRONOUN_PATTERN, message, flags=re.IGNORECASE)
                   or (route.route == "clarify" and not _names_something_else(message)))
    if asked and not route.product_ids and points_back:
        for info in earlier[:PRODUCT_LOOKBACK_TURNS]:
            if info.get("product_ids"):
                return (Route("database", f"{'/'.join(asked)} question about a known product",
                              intents=asked, product_ids=info["product_ids"]),
                        f"used the product from earlier in the chat ({', '.join(info['product_ids'])})")
            if _topic_changed(info):
                break

    if (route.product_ids and not asked and last.get("route") == "database"
            and last_product_intents
            and (re.search(FOLLOW_UP_PATTERN, message, flags=re.IGNORECASE)
                 or _is_bare_product_reply(message, products))):
        return (Route("database", f"{'/'.join(last_product_intents)} question about a known product",
                      intents=last_product_intents, product_ids=route.product_ids),
                f"follow-up: repeated the previous {'/'.join(last_product_intents)} "
                "question for the product named now")

    return route, None


def rows_for_turn(message: str, result: dict) -> list:
    """The two rows (customer message, assistant reply) to save for one turn, with audit details.
    """
    assistant_details = {
        "route_info": {
            "route": result["route"],
            "intents": result.get("intents", []),
            "product_ids": result.get("product_ids", []),
            "order_id": result.get("order_id"),
        },
        "reason": result.get("reason"),
        "memory": result.get("memory"),
        "escalation_reason": result.get("escalation_reason"),
        "audit": result.get("details"),
    }
    kb = result.get("details") if result.get("source") == "knowledge_base" else None
    return [
        {"sender": "customer", "content": message, "intent": None, "source": None,
         "answer_type": None, "details": None, "escalation_id": None},
        {"sender": "assistant", "content": result["answer"], "intent": result["route"],
         "source": result.get("source"),
         "answer_type": (kb or {}).get("answer_type") if kb else None,
         "details": assistant_details, "escalation_id": result.get("escalation_id")},
    ]
