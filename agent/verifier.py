"""Fact-checks an LLM answer against the retrieved source text before a customer sees it.

verify() returns a list of problems; an empty list means the answer passed.
Checks: invented dialogue, non-answers, medical claims, words the sources don't
support, numbers that aren't in the sources, and ingredient claims that
contradict the product data.
"""

import re

from agent.router import find_products
from agent.settings import BRAND_WORDS

DIALOGUE_PATTERNS = [
    r"^\s*\**(customer|agent|user|assistant|client|support)\**\s*:",
    r"\bQ:",
]

NON_ANSWER_PATTERNS = [
    r"\bask (me )?your question\b", r"\bhow (can|may) i (help|assist)\b",
    r"\bwhat would you like to know\b", r"\bgo ahead and ask\b",
]

STOPWORDS = set("""
about above after again also been before being below between both could does doing
during each from further have having here into itself just more most must once only
other over same should some such than that their them then there these they this
those through under until very were what when where which while will with would your
yours please thank thanks help sorry customer customers product products
""".split()) | set(BRAND_WORDS)

# At least this share of the answer's content words must appear in the source text.
MIN_GROUNDED_SHARE = 0.6

# Claims a customer would rely on. They are only allowed if the source text says the same word.
RISKY_CLAIM_WORDS = ["free", "unlimited", "lifetime", "guaranteed", "always", "anytime",
                     "any time", "no questions asked", "100%"]

MEDICAL_PATTERNS = [
    r"\bcures?\b", r"\btreats?\b", r"\btreating\b", r"\bheals?\b",
    r"\bguarantee[sd]?\b", r"\bprescription\b", r"\bdiagnos", r"\beczema\b",
    r"\bpsoriasis\b", r"\brosacea\b", r"\bacne\b", r"\bdermatitis\b",
]

PREAMBLE_PATTERN = re.compile(
    r"^\s*(sure|certainly|of course|absolutely|great question|hi there|hello|dear customer)"
    r"[^.!?\n]*[.!?:,]?\s*(here('s| is)[^:\n]*:\s*)?",
    flags=re.IGNORECASE,
)


def clean_answer(answer: str) -> str:
    """Strip chatty openers ('Sure! Here is...') and 'A:' prefixes the model sometimes adds.
    """
    cleaned = answer.strip()
    for _ in range(3):
        new = PREAMBLE_PATTERN.sub("", cleaned, count=1).strip()
        new = re.sub(r"^\**A:\**\s*", "", new)
        if new == cleaned or not new:
            break
        cleaned = new
    return cleaned


def _numbers(text: str) -> set:
    """Numbers in `text`, ignoring digits inside words (such as 'B5')."""
    return set(re.findall(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![A-Za-z\d])", text))


def check_numbers(answer: str, context: str) -> list:
    """Every number in the answer must also appear in the source text."""
    missing = sorted(_numbers(answer) - _numbers(context))
    return [f"number(s) not in the source text: {', '.join(missing)}"] if missing else []


def check_dialogue(answer: str) -> list:
    """Reject answers that contain invented Q:/A: or 'Customer:' dialogue."""
    for p in DIALOGUE_PATTERNS:
        if re.search(p, answer, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
            return ["answer contains invented dialogue or Q:/A: formatting"]
    return []


def check_non_answer(answer: str) -> list:
    """Reject replies that only invite another question instead of answering."""
    for p in NON_ANSWER_PATTERNS:
        if re.search(p, answer, flags=re.IGNORECASE):
            return ["answer doesn't answer the question"]
    return []


def _content_words(text: str) -> list:
    """Lowercase words of four or more letters that are not stopwords."""
    return [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS]


def grounded_share(answer: str, context: str) -> float:
    """Share (0-1) of the answer's content words that appear in the sources.

    Words are compared by their first five letters so plurals and suffixes still match.
    """
    answer_words = _content_words(answer)
    if not answer_words:
        return 1.0
    context_stems = {w[:5] for w in _content_words(context)}
    return sum(w[:5] in context_stems for w in answer_words) / len(answer_words)


def check_grounding(answer: str, context: str) -> list:
    """Reject answers that mostly use words not found in the source text."""
    answer_words = _content_words(answer)
    if not answer_words:
        return []
    context_stems = {w[:5] for w in _content_words(context)}
    share = grounded_share(answer, context)
    if share < MIN_GROUNDED_SHARE:
        unsupported = sorted({w for w in answer_words if w[:5] not in context_stems})
        return [f"only {share:.0%} of the answer is backed by the source text "
                f"(unsupported words: {', '.join(unsupported[:8])})"]
    return []


def check_risky_claims(answer: str, context: str) -> list:
    """Words like 'free' or 'unlimited' change what a customer expects, so the sources must use them too.

    The grounding check alone can miss these in a short answer ('Shipping is free on all orders').
    """
    problems = []
    for word in RISKY_CLAIM_WORDS:
        pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
        if re.search(pattern, answer, flags=re.IGNORECASE) and not re.search(pattern, context, flags=re.IGNORECASE):
            problems.append(f"claims '{word}', which the source text doesn't say")
    return problems


def check_medical(answer: str) -> list:
    """Reject answers that claim a product treats, cures or diagnoses anything."""
    hits = [p for p in MEDICAL_PATTERNS if re.search(p, answer, flags=re.IGNORECASE)]
    return ["answer makes a medical claim (SYSTEM_RULES rule 6)"] if hits else []


def _ingredient_vocabulary(products: list) -> set:
    """Ingredient names across all products, used to spot a wrong ingredient claim."""
    vocab = set()
    for p in products:
        for ing in p.get("ingredients") or []:
            base = re.sub(r"\s*\d+%?", "", ing.lower()).strip()
            vocab.add(base)
            first = base.split()[0]
            if first not in {"water", "fatty", "shea", "zinc", "titanium"}:
                vocab.add(first)
                vocab.add(first + "s")
    return {v for v in vocab if len(v) > 3 and v != "water"}


def check_product_ingredients(answer: str, products: list) -> list:
    """If the answer is about one product, it must not name an ingredient that product lacks.
    """
    mentioned = find_products(answer, products)
    if len(mentioned) != 1:
        return []
    product = next(p for p in products if p["product_id"] == mentioned[0])
    real = " | ".join(i.lower() for i in (product.get("ingredients") or []))

    problems = []
    for word in sorted(_ingredient_vocabulary(products)):
        if re.search(r"\b" + re.escape(word) + r"\b", answer, flags=re.IGNORECASE):
            stem = word.rstrip("s")
            if stem not in real:
                problems.append(f"claims {product['name']} contains '{word}', "
                                f"which is not in its ingredient list")
    return problems


def _without_product_names(text: str, products: list) -> str:
    """Remove product names, so a name like 'Anti-Acne Cleanser' isn't read as a medical claim."""
    for p in sorted(products, key=lambda p: -len(p["name"])):
        text = re.sub(re.escape(p["name"]), " ", text, flags=re.IGNORECASE)
    return text


def verify(answer: str, context: str, products: list) -> list:
    """All checks on one answer. Returns a list of problems (empty = passed)."""
    if not answer or not answer.strip():
        return ["empty answer"]
    return (check_dialogue(answer)
            + check_non_answer(answer)
            + check_medical(_without_product_names(answer, products))
            + check_grounding(answer, context)
            + check_risky_claims(answer, context)
            + check_numbers(answer, context)
            + check_product_ingredients(answer, products))
