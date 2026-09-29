"""Rule-based intent router.

Decides which path answers a customer message: the database, the document
knowledge base, a clarifying question, a human, or a fixed safety reply. It uses
plain regexes (no LLM), so the safety-critical decisions are predictable and
easy to test.

Order matters. Safety and human hand-off are checked first, then rule-breaking
attempts and medical questions, then greetings, order lookups, and finally
price / stock / ingredient questions.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from agent.settings import BRAND, ORDER_ID_PATTERN, ORDER_NUMBER_HINT, PRODUCT_ALIASES

HUMAN_PATTERNS = [
    r"\bhuman\b", r"\breal person\b", r"\brepresentative\b", r"\blive agent\b",
    r"\b(talk|speak|chat|connect me) (to|with) (someone|somebody|a person|a team member|an agent|support staff|support)\b",
    r"\bput me through\b", r"\bcall me\b", r"\bmanager\b",
    r"\b(insaan|insan|banday|bande|admi|aadmi|agent) se baat\b",
]
# Skincare-specific on purpose. Adapting to another industry: replace these with that industry's
# real hazard words (a burn, a fire, an electric shock, food poisoning, ...), not just this list.
SAFETY_PATTERNS = [
    r"\brash\b", r"\bburn(ing|s|ed)?\b", r"\ballergic\b", r"\ballergy\b",
    r"\bswell(ing|ed)?\b", r"\bhives\b", r"\bitch(ing|y)?\b", r"\bblister",
    r"\bbroke out\b", r"\bbreak(ing)? out\b", r"\bhospital\b", r"\bdoctor\b",
    r"\bkharish\b", r"\bjalan\b", r"\bsojan\b", r"\bdaan[ea]\b", r"\ballergy ho\b",
]
COMPLAINT_PATTERNS = [
    r"\bcomplain", r"\bterrible\b", r"\bawful\b", r"\bscam\b", r"\bunacceptable\b",
    r"\bworst\b", r"\bangry\b", r"\bfurious\b",
    r"\bbakwas\b", r"\bghatiya\b",
    r"\bkaam nahi (kar|ki[ay])", r"\bkharab\b", r"\bfayda nahi\b", r"\basar nahi\b",
    r"\bkoi farq nahi\b", r"\breaction ho gaya\b",
    r"\bdidn'?t work\b", r"\bnot working\b", r"\bno effect\b", r"\bmade it worse\b",
]
# "roman me baat kro", "reply in urdu", "do you speak english"
LANGUAGE_REQUEST_PATTERNS = [
    r"\b(roman|urdu|english|hindi)\b.{0,15}\b(me|mein|main|men)\b.{0,15}\b(baat|bat|jawab|jawaab|reply|batao|bolo|likho|likhein)",
    r"\b(baat|bat|jawab|jawaab|reply|bolo|likho)\b.{0,25}\b(roman|urdu|english)\b",
    r"\b(reply|answer|respond|speak|talk|write|chat|type)\b.{0,12}\b(in|to me in)\b.{0,6}\b(roman|urdu|english)\b",
    r"\b(do you|can you|kya aap)\b.{0,12}\b(speak|know|understand|reply)\b.{0,12}\b(roman|urdu|english)\b",
    r"\b(kya aap|aap)\b.{0,12}\b(roman|urdu|english)\b.{0,15}\b(samajh\w*|bol\w*|jaan\w*|likh\w*)",
]
# Clearly unrelated to any business (weather, trivia, homework, "are you an AI") - a polite decline,
# not a support case. Kept narrow on purpose: a miss here just falls through to the knowledge base,
# which still answers honestly, so under-matching is safe and over-matching is the real risk.
# "Are you a bot/human?" asks about the assistant, not for one, so it must not trigger a hand-off
# (checked separately, before HUMAN_PATTERNS, since bare \bhuman\b would otherwise match it).
BOT_IDENTITY_PATTERNS = [
    r"\b(are you|r u)\b.{0,10}\b(a bot|human|an ai|a robot|chatgpt|gpt)\b",
    r"\bwho (made|built|created|trained) you\b", r"\bwhat model are you\b",
]
OFF_TOPIC_PATTERNS = [
    r"\bweather\b", r"\bforecast\b", r"\bdegrees? (celsius|fahrenheit)\b",
    r"\b(write|tell) me a (poem|joke|story|song)\b", r"\btell me a joke\b",
    r"\bcapital of\b", r"\bwho is the (president|prime minister)\b",
    r"\bwhat is \d+\s*[+\-*/]\s*\d+\b", r"\bsolve (this|for x)\b",
    r"\bwrite (me )?(a )?(python|javascript|code|program|function)\b",
] + BOT_IDENTITY_PATTERNS
GREETING_PATTERN = (
    r"^(hi|hello|hey|salam|assalam ?o ?alaikum|aoa|good (morning|afternoon|evening)|"
    r"thanks|thank you|thx|ok|okay|bye|shukriya|shukria|jazakallah|meherbani)[\s!.,?]*$"
)
PRICE_PATTERNS = [r"\bprice", r"\bcost", r"\bhow much\b", r"\$", r"\bexpensive\b", r"\bcheap",
                  r"\bkitn[aei]\b", r"\bqeemat\b", r"\bqimat\b"]
STOCK_PATTERNS = [
    r"\bin stock\b", r"\bout of stock\b", r"\bsold out\b", r"\brestock",
    r"\bstock\b", r"\bavailab",
    r"\bmojood\b", r"\bstock mein\b", r"\bhai ya nahi\b",
]
INGREDIENT_PATTERNS = [
    r"\bingredient", r"\bcontain", r"\bmade (of|with|from)\b",
    r"\bwhat('?s| is)? in\b", r"\bformula", r"\binci\b",
    r"\b(has|have|got)\b.*\bin it\b", r"\bdoes it have\b",
    r"\bkis se bana\b", r"\bandar kya hai\b", r"\bajza\b",
]
POLICY_TOPIC_PATTERNS = [
    r"\bship", r"\bdeliver", r"\bexpress\b", r"\breturn", r"\brefund",
    r"\bcancel", r"\bexchange\b",
    r"\bwapsi\b", r"\bwaapas\b", r"\bbhejna\b",
]
MANIPULATION_PATTERNS = [
    r"\bignore (all |any |your |the )?(previous |prior |above )?(instructions|rules|prompt)",
    r"\b(system|hidden|initial) prompt\b", r"\bdeveloper mode\b", r"\bjailbreak",
    r"\b(reveal|show|print|repeat) (me )?(your|the) (rules|instructions|prompt)",
    r"\bpretend (you are|to be|you're)\b", r"\bact as (a|an)\b", r"\byou are now\b",
    r"\bfrom now on you\b", r"\bprescribe\b",
]
MEDICAL_QUESTION_PATTERNS = [
    r"\b(cure|cures|curing|treat|treats|treating|heal|heals|healing|fix|fixes|get rid of|clear up|clears up)\b"
    r".*\b(acne|eczema|psoriasis|rosacea|dermatitis|melasma|pimples?|breakouts?|scars?|"
    r"infection|disease|condition|wrinkles|hyperpigmentation|spots)\b",
    r"\bdiagnos", r"\bwhich disease\b", r"\bwhat (disease|condition) (is|do i have)\b",
    r"\bis it (safe|ok|okay) (during|while|in) pregnan", r"\bmedicat(ion|e)\b",
]
MEDICAL_REPLY = ("I can't give medical advice or say that a product treats or cures a skin "
                 "condition. I can tell you what's in a product and what its ingredients "
                 "generally do. For a skin condition, a dermatologist or doctor is the right "
                 "person to ask.")
GUARDRAIL_REPLY = (f"I can only help with {BRAND}'s products, prices, stock, your orders, and our "
                   "shipping, return and refund policies, and I can't change how I work or give "
                   "medical advice. What can I help you with?")
HOW_IT_WORKS_PATTERNS = [
    r"\bhow (do|can|would|should) (i|we|you)\b", r"\bhow (long|quickly|fast|soon)\b",
    r"\bbe processed\b", r"\bprocessing time\b", r"\btracking number\b",
    r"\bwhat (is|'s) the (process|policy)\b",
]
ORDER_STATUS_PATTERNS = [
    r"\bwhere('s| is) my (order|package|parcel)\b", r"\btrack", r"\border status\b",
    r"\bstatus of my order\b", r"\bmy order\b.*\b(shipped|arrive|deliver|status)",
    r"\bhas my order\b", r"\bwhen will my order\b",
    r"\bmera (order|parcel)\b", r"\border kahan\b", r"\bparcel kahan\b",
]


def _which_product_question(products: list) -> str:
    """Ask which product, listing the names when the catalogue is small enough to read."""
    names = [p["name"] for p in products]
    if len(names) <= 8:
        return "Which product do you mean? We have: " + ", ".join(names) + "."
    return ("Which product do you mean? For example: " + ", ".join(names[:5])
            + ", and more. Tell me the product name and I'll check.")


@dataclass
class Route:
    """The outcome of routing one message.

    route is one of: escalate, guardrail, medical, greeting, clarify, database, knowledge.
    """
    route: str
    reason: str
    intents: list = field(default_factory=list)
    product_ids: list = field(default_factory=list)
    order_id: str = None
    clarify_question: str = None


def _any(patterns, text):
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


# High on purpose: it's better to ask which product than to answer for the wrong one.
FUZZY_MIN_SIMILARITY = 0.85


def _fuzzy_products(text: str, products: list) -> list:
    """Typo-tolerant product match (for example 'sunscren'). Only used when an exact match found nothing.
    """
    words = re.findall(r"[a-z0-9']+", text.lower())
    pieces = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    best = {}
    for p in products:
        names = [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], [])
        for name in names:
            if len(name) < 5:
                continue
            for piece in pieces:
                if abs(len(piece) - len(name)) > 3:
                    continue
                score = SequenceMatcher(None, piece, name).ratio()
                if score >= FUZZY_MIN_SIMILARITY and score > best.get(p["product_id"], 0):
                    best[p["product_id"]] = score
    return sorted(best, key=lambda pid: -best[pid])


def find_products(text: str, products: list, fuzzy: bool = False) -> list:
    """Ids of the products named in `text`, in the order they appear.

    Longer names match first, so 'niacinamide serum' wins over 'serum'.
    """
    candidates = []
    for p in products:
        names = [p["name"].lower()] + PRODUCT_ALIASES.get(p["product_id"], [])
        for n in names:
            candidates.append((n, p["product_id"]))
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    lowered = text.lower()
    # Bundle contents such as "(2 x Strips, 1 x Post Wax Oil)" describe one product; the items inside
    # them must not be matched as products of their own, whatever their name length or spacing.
    lowered = re.sub(r"\([^)]*\d\s*x\s[^)]*\)", lambda m: " " * len(m.group(0)), lowered)
    found = []
    for name, pid in candidates:
        m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", lowered)
        if m and pid not in [f[1] for f in found]:
            found.append((m.start(), pid))
            end = m.end()
            # A bracketed size or bundle list after the name, such as "(2 x Strips, 1 x Post Wax Oil)",
            # belongs to this product, so the items inside it must not match other products.
            bracket = re.match(r"\s*\([^)]*\)", lowered[end:])
            if bracket:
                end += bracket.end()
            lowered = lowered[:m.start()] + " " * (end - m.start()) + lowered[end:]
    if not found and fuzzy:
        return _fuzzy_products(text, products)
    return [pid for _, pid in sorted(found)]


def route_message(message: str, products: list) -> Route:
    """Choose the path for one customer message."""
    text = message.strip()

    # Safety and human hand-off come first so nothing else can swallow them.
    if _any(SAFETY_PATTERNS, text):
        return Route("escalate", "possible skin reaction / safety concern")
    # 'How do I contact you?' is a question about the process, not a request for a person.
    asks_about_process = (_any(HOW_IT_WORKS_PATTERNS, text)
                          and not re.search(r"\b(contact|reach|talk|speak)\b", text, re.IGNORECASE))
    if _any(HUMAN_PATTERNS, text) and not asks_about_process and not _any(BOT_IDENTITY_PATTERNS, text):
        return Route("escalate", "customer asked for a human")
    if _any(COMPLAINT_PATTERNS, text):
        return Route("escalate", "complaint")

    # Attempts to change the assistant's rules or role get a fixed reply.
    if _any(MANIPULATION_PATTERNS, text):
        return Route("guardrail", "attempt to change the assistant's rules or role")

    # Medical claims and diagnoses are never answered.
    if _any(MEDICAL_QUESTION_PATTERNS, text):
        return Route("medical", "asks for a medical claim or diagnosis (SYSTEM_RULES rule 6)")

    if re.match(GREETING_PATTERN, text, flags=re.IGNORECASE):
        return Route("greeting", "greeting / small talk")

    # An order number means an exact database lookup.
    order_match = re.search(ORDER_ID_PATTERN, text, flags=re.IGNORECASE)
    if order_match:
        order_id = order_match.group(0).upper().replace("-", "")
        return Route("database", "order ID found", intents=["order_status"], order_id=order_id)
    if _any(ORDER_STATUS_PATTERNS, text) and not _any(HOW_IT_WORKS_PATTERNS, text):
        return Route("clarify", "order status asked without an order ID",
                     intents=["order_status"],
                     clarify_question="Could you share your order number? " + ORDER_NUMBER_HINT)

    product_ids = find_products(text, products)
    intents = []
    # 'Is shipping available to France?' is about policy, not stock, so it stays out of the product lookup.
    is_policy_question = _any(POLICY_TOPIC_PATTERNS, text)
    if _any(PRICE_PATTERNS, text) and not is_policy_question:
        intents.append("price")
    strong_stock_patterns = [p for p in STOCK_PATTERNS if p != r"\bavailab"]
    if _any(strong_stock_patterns, text) or (_any(STOCK_PATTERNS, text) and not is_policy_question):
        intents.append("stock")
    if _any(INGREDIENT_PATTERNS, text):
        intents.append("ingredients")
    if intents and not product_ids:
        product_ids = find_products(text, products, fuzzy=True)

    if intents and product_ids:
        return Route("database", f"{'/'.join(intents)} question about a known product",
                     intents=intents, product_ids=product_ids)

    strong_stock = _any([p for p in STOCK_PATTERNS if p != r"\bavailab"], text)
    if "price" in intents or strong_stock:
        return Route("clarify", "price/stock question without a product",
                     intents=intents,
                     clarify_question=_which_product_question(products))

    # A request about the language itself is not a knowledge question. Checked last, so a real question
    # that also names a language ("roman me post wax oil ki price") is still answered.
    if _any(LANGUAGE_REQUEST_PATTERNS, text) and not is_policy_question:
        return Route("language", "asks which language the assistant replies in")

    if _any(OFF_TOPIC_PATTERNS, text):
        return Route("off_topic", "unrelated to the business")

    return Route("knowledge", "general question -> knowledge base",
                 intents=intents, product_ids=product_ids)
