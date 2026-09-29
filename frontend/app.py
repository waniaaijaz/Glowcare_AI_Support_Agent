"""Customer chat page.

    python -m streamlit run frontend/app.py

Every reply comes from handle_message(). The caption under an answer shows where
it came from, and 'How this answer was found' shows the route, database rows or
document sections behind it.
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import streamlit as st

from agent import llm_client
from agent.support_agent import handle_message, get_products
from agent.settings import BRAND, DEMO_NOTE, EXAMPLE_INGREDIENT_PRODUCT, EXAMPLE_ORDER_ID, EXAMPLE_PRODUCT
from database import db

st.set_page_config(page_title=f"{BRAND} Assistant", layout="centered")

EXAMPLE_QUESTIONS = [
    f"What ingredients are in the {EXAMPLE_INGREDIENT_PRODUCT}?",
    f"How much is the {EXAMPLE_PRODUCT} and is it in stock?",
    f"Where is my order {EXAMPLE_ORDER_ID}?",
    "Can I return a product after opening it?",
    "Do you sell products in the UK?",
    "I got a rash after using the cleanser",
]

SOURCE_LABELS = {
    "database": "Answered from the database",
    "knowledge_base": "Answered from company documents",
    "fixed_reply": "Standard reply",
}
ANSWER_TYPE_LABELS = {
    "llm": "AI-written answer, passed all checks",
    "quoted_source": "AI answer rejected, so the policy text is quoted instead",
    "not_found": "Information not available",
}


@st.cache_resource(show_spinner="Starting the assistant (first load can take ~30 seconds)...")
def warm_up():
    """Load the products and the search model once, so the first question isn't slow.

    Returns an error message on failure, otherwise None.
    """
    try:
        get_products()
        from rag.retriever import retrieve
        retrieve("warm up", verbose=False)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


startup_error = warm_up()

if "messages" not in st.session_state:
    st.session_state.messages = []


def to_json(value):
    """Make a value safe to show as JSON (Decimals and dates become strings)."""
    return json.loads(json.dumps(value, default=str))


def show_details(result: dict):
    """Caption, escalation notice and 'how this answer was found' panel for one reply.
    """
    source = SOURCE_LABELS.get(result.get("source"), result.get("source"))
    st.caption(f"{source} · route: {result['route']}")
    if result.get("memory"):
        st.caption(f"Used earlier messages: {result['memory']}")
    if result.get("logging_error"):
        st.caption(f"Note: this exchange couldn't be saved ({result['logging_error']})")
    if result.get("escalate"):
        if result.get("escalation_id"):
            state = "created" if result.get("escalation_new") else "already open, same case"
            st.warning(f"Handed to a human team member: escalation "
                       f"ESC-{result['escalation_id']} {state} · priority "
                       f"{result.get('escalation_priority')}")
        elif result.get("escalation_error"):
            st.error("Flagged for a human, but the escalation couldn't be saved: "
                     f"{result['escalation_error']}")
        else:
            st.warning("Flagged for a human team member (not saved: this chat "
                       "has no conversation record).")

    with st.expander("How this answer was found"):
        st.markdown(f"**Route:** `{result['route']}` ({result['reason']})")
        details = result.get("details")

        if result["route"] == "database" and details:
            st.markdown("**Database rows used:**")
            st.json(to_json(details), expanded=False)

        elif result["route"] == "knowledge" and details:
            answer_type = details.get("answer_type")
            label = ANSWER_TYPE_LABELS.get(answer_type, answer_type)
            if details.get("llm_error"):
                label = ("AI service unavailable, so the policy text is quoted instead"
                         if answer_type == "quoted_source" else
                         "AI service unavailable and no strong match in the documents")
            st.markdown(f"**Result:** {label}")
            chunks = details.get("chunks_used") or []
            if chunks:
                st.markdown("**Document sections retrieved** (lower distance = closer match):")
                for i, c in enumerate(chunks, 1):
                    section = c.get("section") or c["text"].splitlines()[0]
                    st.markdown(f"{i}. {section} · `{c['source']}` · distance {c['distance']}")
            if details.get("llm_answer"):
                st.markdown("**What the AI wrote:**")
                st.text(details["llm_answer"])
            if details.get("problems"):
                st.markdown("**Why the AI answer wasn't used:**")
                for p in details["problems"]:
                    st.markdown(f"- {p}")
        else:
            st.markdown("No database or document lookup was needed.")


def current_conversation_id():
    """Saved-chat id for this browser session, created on first use (None if the database is down).
    """
    if st.session_state.get("conversation_id") is None:
        try:
            st.session_state.conversation_id = db.create_conversation()
        except Exception:
            return None
    return st.session_state.conversation_id


def ask(question: str):
    """Send one question through the agent and add the exchange to the session."""
    st.session_state.messages.append({"role": "user", "content": question, "result": None})
    try:
        result = handle_message(question, conversation_id=current_conversation_id(),
                                verbose=False)
        reply = result["answer"]
    except Exception as e:
        result = None
        reply = ("Sorry, something went wrong on our side and I couldn't answer. "
                 f"(Technical detail: {type(e).__name__}: {e})")
    st.session_state.messages.append({"role": "assistant", "content": reply, "result": result})


with st.sidebar:
    st.subheader(f"{BRAND} Assistant")
    st.caption(DEMO_NOTE)

    st.markdown(f"**AI model:** {llm_client.LLM_PROVIDER} · `{llm_client.MODEL_NAME}`")
    cost = llm_client.estimated_cost_usd()
    calls = llm_client.usage_totals.get("calls", 0)
    if cost is not None:
        note = " at paid prices ($0 on Gemini's free tier)" if llm_client.LLM_PROVIDER == "gemini" else ""
        st.caption(f"AI calls since the app started: {calls} · est. cost ${cost:.4f}{note}")
    elif llm_client.LLM_PROVIDER == "ollama":
        st.caption("Local model: free.")

    st.divider()
    st.markdown("**Try asking**")
    for q in EXAMPLE_QUESTIONS:
        if st.button(q, key=f"example-{q}", width="stretch"):
            ask(q)
            st.rerun()

    st.divider()
    if st.session_state.get("conversation_id"):
        st.caption(f"Conversation #{st.session_state.conversation_id}")
    if st.button("Clear chat", width="stretch"):
        if st.session_state.get("conversation_id"):
            try:
                db.close_conversation(st.session_state.conversation_id)
            except Exception:
                pass
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.rerun()


st.title(f"{BRAND} Assistant")
st.caption("Ask about our products, prices, stock, your order, or our shipping, "
           "return and refund policies.")

if startup_error:
    st.error("The assistant couldn't start. Check that PostgreSQL is running, "
             "`python rag/ingest.py` has been run, and your `.env` is set up.\n\n"
             f"Details: {startup_error}")
    st.stop()

if not st.session_state.messages:
    with st.chat_message("assistant"):
        st.markdown(f"Hi! I'm the {BRAND} assistant. How can I help you today?")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg["result"]:
            show_details(msg["result"])

if question := st.chat_input("Type your question..."):
    with st.chat_message("user"):
        st.markdown(question)
    with st.spinner("Thinking..."):
        ask(question)
    st.rerun()
