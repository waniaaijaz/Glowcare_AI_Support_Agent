"""Answers questions from the document knowledge base (policies, FAQ, product guides).

Pipeline: retrieve relevant chunks, ask the LLM to answer using only those
chunks, then fact-check the answer. If the check fails, the customer gets the
quoted source text or an honest 'I don't have that information'. Prices, stock
and orders never come through here; they are answered from the database.
"""

import json
import re

from rag.retriever import retrieve
from agent.llm_client import (call_llm, LLM_PROVIDER, MODEL_NAME, LLMUnavailableError,
                              TRANSLATE_FOR_SEARCH, TRANSLATE_RETRIES, TRANSLATE_TIMEOUT)
from agent.verifier import clean_answer, verify
from agent.settings import BRAND

SYSTEM_RULES_PATH = "SYSTEM_RULES.md"


def _load_system_rules() -> str:
    """Read SYSTEM_RULES.md on every call, so edits apply without a restart."""
    with open(SYSTEM_RULES_PATH, "r", encoding="utf-8") as f:
        return f.read()


# Fixed text rather than LLM output, so 'I don't know' can never turn into a guess.
NOT_FOUND_REPLY = (
    "I'm sorry, I don't have that information. "
    f"A {BRAND} team member can help you further."
)


def _build_prompt(question: str, context: str) -> str:
    """Prompt sent with the retrieved excerpts.

    The model replies with JSON: the answer in the customer's language, plus an
    English copy that is used only for fact-checking.
    """
    return (
        f"Knowledge base excerpts (some may be irrelevant):\n\n"
        f"{context}\n\n"
        f"Instructions:\n"
        f"1. Use only the excerpts that contain the answer to the "
        f"customer's question. An excerpt can answer a question without "
        f"using the customer's exact words (e.g. a rule about which "
        f"countries we ship to answers \"do you sell in France?\"). "
        f"Ignore excerpts about other topics.\n"
        f"2. Answer in 1-3 plain sentences using only facts from those "
        f"excerpts. No greeting, no preamble, no dialogue, no advice that "
        f"isn't in the excerpts.\n"
        f"3. Only say a product contains an ingredient or has a feature if "
        f"an excerpt about THAT product says so. The Ingredients Guide "
        f"describes ingredients used across all products — it never says "
        f"which product contains them.\n"
        f"4. Only if NONE of the excerpts contain the answer, reply with "
        f"exactly: \"{NOT_FOUND_REPLY}\"\n"
        f"5. Reply in the SAME language and script the customer used. If "
        f"they wrote in Roman Urdu (Urdu words spelled with English "
        f"letters, e.g. \"kitna hai\"), your \"answer\" must be in Roman "
        f"Urdu too, in the same friendly, plain-sentence style. If they "
        f"wrote in English, \"answer\" stays in English.\n"
        f"6. Respond with ONLY a JSON object, no markdown fences, no text "
        f"before or after it, in exactly this shape:\n"
        f'{{"answer": "<your reply in the customer\'s language, per rule 5>", '
        f'"answer_english": "<the exact same reply translated into plain '
        f'English, so it can be double-checked against the excerpts above; '
        f'if your answer is already in English, repeat it here unchanged>"}}\n\n'
        f"Customer question: {question}\n"
        f"JSON:"
    )


def _parse_llm_reply(raw: str) -> tuple:
    """Split the model's JSON into (customer_answer, english_answer).

    Falls back to the raw text for both if the reply isn't valid JSON (for example
    from a small local model).
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text.strip()).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            answer = str(data.get("answer", "")).strip()
            answer_english = str(data.get("answer_english", "")).strip()
            if answer:
                return answer, (answer_english or answer)
        except (json.JSONDecodeError, AttributeError):
            pass
    return raw.strip(), raw.strip()


def _translate_for_search(question: str) -> str:
    """Restate the question in English, only for the vector search (the documents are in English).

    Falls back to the original text on any failure.
    """
    if not TRANSLATE_FOR_SEARCH:
        return question
    try:
        translated = call_llm(
            prompt=f"Restate the following customer question in plain "
                   f"English, as a single search query. If it's already "
                   f"in English, repeat it unchanged. Output ONLY the "
                   f"restated question, nothing else:\n\n{question}",
            system_prompt="You are a translation and search-query tool. "
                           "Output only the restated question.",
            timeout=10,
            patient=False,
        )
        translated = translated.strip()
        if not translated:
            print(f"[SEARCH] Empty translation for {question!r}, searching with the original text")
            return question
        return translated
    except Exception as e:
        print(f"[SEARCH] Translation failed for {question!r}, searching with the original text: {e}")
        return question


# A chunk is only quoted if it is a close match (lower distance = closer).
QUOTE_MAX_DISTANCE = 0.75
# Stricter bar for overriding a refusal by the model.
REFUSAL_OVERRIDE_DISTANCE = 0.5


def _quote_best_chunk(chunks: list):
    """Fallback for a rejected answer: the best-matching source text, word for word.

    Returns None when nothing matches well enough.
    """
    if not chunks or chunks[0]["distance"] > QUOTE_MAX_DISTANCE:
        return None
    lines = chunks[0]["text"].splitlines()
    title, body = lines[0], "\n".join(lines[1:]).strip()
    section = title.split(" > ")[-1]
    document = title.split(" > ")[0].replace(f"{BRAND} ", "")
    if body.startswith("**Q:"):
        answer_part = body.split("\n", 1)[-1].strip()
        answer_part = answer_part[2:].strip() if answer_part.startswith("A:") else answer_part
        return f"From our FAQ: {answer_part}"
    return f'Here is what our {document} says under "{section}":\n\n{body}'


def answer_question(question: str, products: list = None, verbose: bool = True) -> dict:
    """Answer one question from the knowledge base.

    Returns a dict with the answer, its type (llm | quoted_source | not_found), the
    chunks used, the exact prompt and any problems the checker found, so every
    answer can be audited.
    """
    products = products or []
    search_query = _translate_for_search(question)
    if verbose and search_query != question:
        print(f"[SEARCH] Translated for search: {search_query}")
    retrieval = retrieve(search_query, verbose=verbose)
    context = retrieval["context"]
    chunks_used = retrieval["chunks"]

    # Nothing relevant retrieved: don't ask the LLM at all.
    if not chunks_used:
        if verbose:
            print(f"[LLM] Skipped — no relevant chunks. Answer: {NOT_FOUND_REPLY}\n")
        return {
            "question": question,
            "chunks_used": [],
            "prompt_sent_to_llm": None,
            "llm_answer": None,
            "problems": [],
            "answer": NOT_FOUND_REPLY,
            "answer_type": "not_found",
            "llm_error": None,
        }

    system_rules = _load_system_rules()
    prompt = _build_prompt(question, context)

    if verbose:
        print(f"\n[LLM] Sending prompt (system rules + {len(chunks_used)} "
              f"chunk(s) of context) to {LLM_PROVIDER} ({MODEL_NAME})...")

    try:
        llm_answer = call_llm(prompt=prompt, system_prompt=system_rules)
    # LLM service down: answer from the documents instead.
    except LLMUnavailableError as e:
        quoted = _quote_best_chunk(chunks_used)
        answer = quoted or NOT_FOUND_REPLY
        answer_type = "quoted_source" if quoted else "not_found"
        problems = [f"AI service unavailable, answered from the documents instead ({e})"]
        if verbose:
            print(f"[LLM] UNAVAILABLE: {e}")
            print(f"[CHECK] Using fallback: {answer_type}")
            print(f"[ANSWER] {answer}\n")
        return {
            "question": question,
            "chunks_used": chunks_used,
            "prompt_sent_to_llm": prompt,
            "llm_answer": None,
            "problems": problems,
            "answer": answer,
            "answer_type": answer_type,
            "llm_error": str(e),
        }

    native_answer, english_answer = _parse_llm_reply(llm_answer)
    answer = clean_answer(native_answer)
    answer_english = clean_answer(english_answer)
    if verbose:
        print(f"[LLM] Raw answer: {llm_answer}")
        if answer != answer_english:
            print(f"[LLM] English twin (used for fact-checking only): {answer_english}")

    # The model refused. If the top chunk is a very close match, quote it instead.
    if NOT_FOUND_REPLY.lower()[:30] in answer_english.lower():
        problems = []
        strong = chunks_used[0]["distance"] <= REFUSAL_OVERRIDE_DISTANCE
        quoted = _quote_best_chunk(chunks_used) if strong else None
        if quoted:
            problems = ["model refused, but the top chunk is a very strong match"]
        answer_type = "quoted_source" if quoted else "not_found"
        answer = quoted or NOT_FOUND_REPLY
    else:
        # Check the English copy, even when the customer sees another language.
        problems = verify(answer_english, context, products)
        if not problems:
            answer_type = "llm"
        else:
            quoted = _quote_best_chunk(chunks_used)
            answer_type = "quoted_source" if quoted else "not_found"
            answer = quoted or NOT_FOUND_REPLY

    if verbose:
        if problems:
            print(f"[CHECK] LLM answer REJECTED: {'; '.join(problems)}")
            print(f"[CHECK] Using fallback: {answer_type}")
        else:
            print("[CHECK] Passed all checks.")
        print(f"[ANSWER] {answer}\n")

    return {
        "question": question,
        "chunks_used": chunks_used,
        "prompt_sent_to_llm": prompt,
        "llm_answer": llm_answer,
        "problems": problems,
        "answer": answer,
        "answer_type": answer_type,
        "llm_error": None,
    }


if __name__ == "__main__":
    test_questions = [
        "Can I return a product after opening it?",
        "Do you sell products in the UK?",
        "What is your policy on returning shoes?",
    ]
    for q in test_questions:
        print("=" * 70)
        answer_question(q)
