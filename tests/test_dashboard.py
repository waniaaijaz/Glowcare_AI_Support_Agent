"""Checks database/dashboard_queries.py against a fixture of chats in the 'test' channel.

The fixture is created and removed by the script. Needs PostgreSQL.
"""

import os

# Replies stay in English so the checks below can compare exact text.
os.environ.setdefault("MATCH_CUSTOMER_LANGUAGE", "off")
os.environ.setdefault("TRANSLATE_FOR_SEARCH", "off")

import sys

from database import db
from database import dashboard_queries as q
from agent.support_agent import handle_message

CHANNEL = "test"
checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"\n      got: {detail}" if detail and not condition else ""))


def run_chat(questions):
    chat_id = db.create_conversation(channel=CHANNEL)
    for text in questions:
        handle_message(text, conversation_id=chat_id, verbose=False)
    return chat_id


def log_kb(chat_id, question, answer, answer_type, problems=None, llm_answer=None):
    db.log_message(chat_id, "customer", question)
    db.log_message(chat_id, "assistant", answer, intent="knowledge", source="knowledge_base",
                   answer_type=answer_type,
                   details={"route_info": {"route": "knowledge", "intents": [], "product_ids": []},
                            "audit": {"answer_type": answer_type, "problems": problems or [],
                                      "llm_answer": llm_answer}})


def build_fixture():
    chats = {}
    chats["A"] = run_chat(["How much does it cost?", "Hydra Balance Moisturizer",
                           "Is the sunscreen in stock?"])
    chats["B"] = run_chat(["I want to talk to a human", "hello"])
    chats["C"] = db.create_conversation(channel=CHANNEL)
    log_kb(chats["C"], "Do you have a loyalty program?", "I'm sorry, I don't have that information.", "not_found")
    log_kb(chats["C"], "  do you have a LOYALTY program? ", "I'm sorry, I don't have that information.", "not_found")
    log_kb(chats["C"], "Can I return opened items?", "Here is what our Return Policy says ...", "quoted_source",
           problems=["only 40% of the answer is backed by the source text"],
           llm_answer="Sure! You can return anything anytime.")
    log_kb(chats["C"], "How long do refunds take?", "Refunds take 5-7 business days.", "llm")
    chats["D"] = run_chat(["I got a rash after using the cleanser"])
    db.update_escalation(db.get_open_escalation(chats["D"])["escalation_id"], "resolved", "test note")
    chats["E"] = run_chat(["How much is the serum?"])
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE messages SET created_at = NOW() - INTERVAL '40 days' WHERE conversation_id = %s",
                    (chats["E"],))
        conn.commit()
    return chats


def cleanup():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM conversations WHERE channel = %s", (CHANNEL,))
        ids = [r["conversation_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM messages WHERE conversation_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM escalations WHERE conversation_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM conversations WHERE conversation_id = ANY(%s)", (ids,))
        conn.commit()
    return len(ids)


def page_checks(chats):
    import os
    from streamlit.testing.v1 import AppTest
    print("\nDashboard page (headless):")
    saved_password = os.environ.pop("DASHBOARD_PASSWORD", None)
    try:
        page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "frontend", "dashboard.py")
        at = AppTest.from_file(page, default_timeout=60).run()
        check("page opens without errors", not at.exception, at.exception)
        at.sidebar.selectbox[0].set_value(CHANNEL).run()
        at.sidebar.radio[0].set_value("Last 30 days").run()
        metrics = {m.label: m.value for m in at.metric}
        check("page shows 4 chats and 10 questions",
              metrics.get("Chats") == "4" and metrics.get("Customer questions") == "10", metrics)
        check("page shows 80% handled without a human (8 of 10 replies)",
              metrics.get("Handled without a human") == "80%", metrics.get("Handled without a human"))
        check("page shows 1 open escalation", metrics.get("Open escalations") == "1",
              metrics.get("Open escalations"))
        check("quality tab shows 1 accepted, 1 quoted, 2 not found",
              (metrics.get("AI answer accepted"), metrics.get("Policy text quoted instead"),
               metrics.get("Not in the documents")) == ("1", "1", "2"), metrics)

        open_case = db.get_open_escalation(chats["B"])
        eid = open_case["escalation_id"]
        at.selectbox(key=f"st-{eid}").set_value("in_progress")
        at.text_input(key=f"note-{eid}").input("Called the customer")
        at.button(key=f"FormSubmitter:update-{eid}-Save").click().run()
        updated = db.get_open_escalation(chats["B"])
        check("saving the escalation form updates the database",
              not at.exception and updated["status"] == "in_progress"
              and "Called the customer" in (updated["staff_notes"] or ""), updated)

        at.text_input(key="conv_search").input("loyalty").run()
        check("conversation search on the page runs without errors", not at.exception, at.exception)
        for period in ["Today", "Last 7 days", "All time"]:
            at.sidebar.radio[0].set_value(period).run()
            check(f"period '{period}' renders without errors", not at.exception, at.exception)
    finally:
        if saved_password is not None:
            os.environ["DASHBOARD_PASSWORD"] = saved_password


def main():
    cleanup()
    try:
        chats = build_fixture()

        s = q.message_stats(days=30, channel=CHANNEL)
        expected = {"chats": 4, "questions": 10, "replies": 10, "from_database": 2,
                    "from_documents": 4, "fixed_replies": 4, "handed_to_human": 2,
                    "clarifying": 1, "memory_used": 1, "ai_accepted": 1, "ai_quoted": 1,
                    "not_found": 2}
        for key, value in expected.items():
            check(f"last 30 days: {key} = {value}", s.get(key) == value, s.get(key))

        s_all = q.message_stats(days=None, channel=CHANNEL)
        check("all time includes the 40-day-old chat (5 chats, 11 questions)",
              s_all["chats"] == 5 and s_all["questions"] == 11, s_all)
        check("channel filter: 'test' numbers don't include other channels",
              q.message_stats(days=None, channel="no-such-channel")["questions"] == 0)

        e = q.escalation_stats(days=30, channel=CHANNEL)
        check("escalations created = 2", e["created"] == 2, e)
        check("open now = 1 (the safety case was resolved)", e["open_now"] == 1, e)
        check("open high priority = 0", e["open_high"] == 0, e)
        check("average resolve time is calculated", e["avg_resolve_hours"] is not None, e)

        days = q.daily_activity(days=30, channel=CHANNEL)
        check("daily activity: 1 day, 4 chats, 10 questions",
              len(days) == 1 and days[0]["chats"] == 4 and days[0]["questions"] == 10, days)

        tp = {r["product_id"]: r["mentions"] for r in q.top_products(days=30, channel=CHANNEL)}
        check("top products: Hydra Balance 1, sunscreen 1 (clarify/hand-off replies don't count)",
              tp == {"GC-P001": 1, "GC-P005": 1}, tp)
        tp_all = {r["product_id"]: r["mentions"] for r in q.top_products(days=None, channel=CHANNEL)}
        check("top products all time adds the serum", tp_all.get("GC-P003") == 1, tp_all)

        routes = {r["route"]: r["replies"] for r in q.route_breakdown(days=30, channel=CHANNEL)}
        check("routes: database 2, knowledge 4, clarify 1, escalate 2, greeting 1",
              routes == {"database": 2, "knowledge": 4, "clarify": 1, "escalate": 2, "greeting": 1}, routes)

        reasons = {r["reason"]: r["cases"] for r in q.escalation_reasons(days=30, channel=CHANNEL)}
        check("hand-off reasons: 1 asked for a human, 1 safety concern",
              reasons == {"customer asked for a human": 1,
                          "possible skin reaction / safety concern": 1}, reasons)

        un = q.unanswered_questions(days=30, channel=CHANNEL)
        check("unanswered: the two loyalty questions are grouped as 1 question asked 2 times",
              len(un) == 1 and un[0]["times_asked"] == 2 and "loyalty" in un[0]["question"].lower(), un)

        rej = q.rejected_ai_answers(days=30, channel=CHANNEL)
        check("rejected AI answers: 1, with the question, what the AI wrote and why",
              len(rej) == 1 and rej[0]["question"] == "Can I return opened items?"
              and rej[0]["ai_wrote"].startswith("Sure!") and "40%" in rej[0]["checker_reasons"][0], rej)

        conv = q.list_conversations(days=30, channel=CHANNEL)
        check("conversation list: 4 chats in the last 30 days", len(conv) == 4, len(conv))
        check("conversation list shows each chat's first question",
              {c["conversation_id"]: c["first_question"] for c in conv}[chats["A"]] == "How much does it cost?")
        esc_chats = {c["conversation_id"] for c in q.list_conversations(days=30, channel=CHANNEL, status="escalated")}
        check("status filter 'escalated' finds chats B and D", esc_chats == {chats["B"], chats["D"]}, esc_chats)
        found = [c["conversation_id"] for c in q.list_conversations(channel=CHANNEL, search="LOYALTY")]
        check("search 'LOYALTY' (any case) finds only chat C", found == [chats["C"]], found)

        open_e = q.list_escalations("open", channel=CHANNEL)
        check("open escalations: 1 (chat B, normal priority)",
              len(open_e) == 1 and open_e[0]["conversation_id"] == chats["B"] and open_e[0]["priority"] == "normal",
              open_e)
        check("resolved escalations: 1 (chat D, high, with the staff note)",
              [(r["conversation_id"], r["priority"], r["staff_notes"])
               for r in q.list_escalations("resolved", channel=CHANNEL)] == [(chats["D"], "high", "test note")])
        check("all escalations: 2, high priority first",
              [r["priority"] for r in q.list_escalations("all", channel=CHANNEL)] == ["high", "normal"])
        check("channels list includes 'test'", CHANNEL in q.channels())

        page_checks(chats)
    finally:
        print(f"(cleaned up {cleanup()} test chats)")

    passed = sum(checks)
    print("-" * 60)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
