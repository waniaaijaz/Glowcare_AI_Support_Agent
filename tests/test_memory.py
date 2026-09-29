"""Checks follow-up memory. Part A: short follow-ups are resolved from earlier turns.
Part B: chats are saved to the real database and memory works from the saved history.
"""

import os

# Replies stay in English so the checks below can compare exact text.
os.environ.setdefault("MATCH_CUSTOMER_LANGUAGE", "off")
os.environ.setdefault("TRANSLATE_FOR_SEARCH", "off")

import sys

from database import db
from agent.support_agent import handle_message
from agent.memory import rows_for_turn

checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"\n      got: {detail}" if detail and not condition else ""))


def chat(questions):
    history, results = [], []
    for q in questions:
        r = handle_message(q, verbose=False, history=history)
        history += rows_for_turn(q, r)
        results.append(r)
    return results


def part_a():
    print("Part A: memory")
    r = chat(["How much does it cost?", "Hydra Balance Moisturizer"])
    check("clarify -> product name gives the price", "$24.99" in r[1]["answer"], r[1]["answer"])

    r = chat(["Is it in stock?", "the serum please"])
    check("clarify (stock) -> 'the serum please' gives stock", "out of stock" in r[1]["answer"], r[1]["answer"])

    r = chat(["Tell me the price of the Niacinamide Serum", "Is it in stock?"])
    check("'is it in stock?' uses the serum from before", "Niacinamide Serum is currently out of stock" in r[1]["answer"],
          r[1]["answer"])

    r = chat(["How much is the serum?", "And the sunscreen?"])
    check("'and the sunscreen?' repeats the price question", "$21.00" in r[1]["answer"], r[1]["answer"])

    r = chat(["Is the sunscreen in stock?", "what about the moisturizer"])
    check("'what about the moisturizer' repeats the stock question",
          "Hydra Balance Moisturizer is in stock" in r[1]["answer"], r[1]["answer"])

    r = chat(["How much is the serum?", "What ingredients does it have?"])
    check("'what ingredients does it have?' -> serum ingredients from the DB",
          "Zinc PCA" in r[1]["answer"] and "Hyaluronic" not in r[1]["answer"], r[1]["answer"])

    r = chat(["How much is the serum?", "I got a rash from it"])
    check("memory never blocks a safety escalation", r[1]["route"] == "escalate" and r[1]["escalate"],
          r[1]["route"])

    r = chat(["How much is the serum?", "hello"])
    check("greeting after a question is still a greeting", r[1]["route"] == "greeting", r[1]["route"])

    r = chat(["How much does it cost?"])
    check("no history -> still asks which product", r[0]["route"] == "clarify", r[0]["route"])

    r = chat(["How much is the serum?", "Where is my order GC10241?", "Is the sunscreen in stock?",
              "How much is the cleanser?", "Is the moisturizer in stock?", "How much is it?"])
    check("'how much is it?' uses the MOST RECENT product (moisturizer)",
          "Hydra Balance Moisturizer costs $24.99" in r[5]["answer"], r[5]["answer"])

    r = chat(["Where is my order GC10241?", "How much is it?"])
    check("no product mentioned before -> asks which product (no guessing)",
          r[1]["route"] == "clarify", r[1]["answer"])

    r = chat(["How much is the cleanser?", "Where is my order GC10241?", "How much is it?"])
    check("product -> order -> 'how much is it?' asks which product (topic changed)",
          r[2]["route"] == "clarify" and r[2]["memory"] is None, r[2]["answer"])

    r = chat(["How much is the cleanser?", "Can I return a product after opening it?", "Is it in stock?"])
    check("product -> policy question -> 'is it in stock?' asks which product",
          r[2]["route"] == "clarify", r[2]["answer"])

    r = chat(["How much is the cleanser?", "hello", "Is it in stock?"])
    check("a greeting in between doesn't change the topic", "Cleanser is in stock" in r[2]["answer"],
          r[2]["answer"])

    r = chat(["How much does it cost?", "the serum", "Is it in stock?"])
    check("clarify -> product -> 'is it in stock?' uses that product",
          "Serum is currently out of stock" in r[2]["answer"], r[2]["answer"])

    r = chat(["Where is my order?", "GC10241"])
    check("clarify (order) -> order number gives the status", "shipped" in r[1]["answer"], r[1]["answer"])

    r = chat(["How much is the serum?", "What's in the cleanser?"])
    check("a full new question isn't changed by memory",
          "Coco-Glucoside" in r[1]["answer"] and r[1]["memory"] is None, r[1]["answer"])

    r = chat(["How much is the serum?", "And the sunscreen, can I return it?"])
    check("'and the sunscreen, can I return it?' is a return-policy question, not a price",
          r[1]["route"] == "knowledge" and r[1]["memory"] is None, (r[1]["route"], r[1]["answer"]))

    r = chat(["How much does it cost?", "Hydra Balance, can I return it if opened?"])
    check("answering 'which product?' with a return question goes to the return policy",
          r[1]["route"] == "knowledge" and r[1]["memory"] is None, (r[1]["route"], r[1]["answer"]))

    r = chat(["How much is the serum?", "Is that available for international shipping?"])
    check("'is that available for international shipping?' is a shipping question, not stock",
          r[1]["route"] == "knowledge" and r[1]["memory"] is None, (r[1]["route"], r[1]["answer"]))

    r = chat(["Is the serum available for international shipping?"])
    check("'is the serum available for international shipping?' (no memory) is a shipping question",
          r[0]["route"] == "knowledge", (r[0]["route"], r[0]["answer"]))

    r = chat(["Is the sunscreen available?"])
    check("'is the sunscreen available?' is still a stock question", "8 left" in r[0]["answer"], r[0]["answer"])

    r = chat(["How much does it cost?", "Hydra Balance Moisturizer"])
    check("memory is explained in the result for the logs", r[1]["memory"] and "clarifying" in r[1]["memory"],
          r[1]["memory"])


def part_b():
    print("\nPart B: logging (real database)")
    chat_id = db.create_conversation()
    try:
        handle_message("How much does it cost?", conversation_id=chat_id, verbose=False)
        r2 = handle_message("Hydra Balance Moisturizer", conversation_id=chat_id, verbose=False)
        r3 = handle_message("I want to talk to a human", conversation_id=chat_id, verbose=False)
        msgs = db.get_conversation_messages(chat_id)

        check("memory works through the database log", "$24.99" in r2["answer"], r2["answer"])
        check("6 messages logged (3 customer + 3 assistant)", len(msgs) == 6, len(msgs))
        check("customer and assistant messages alternate, in order",
              [m["sender"] for m in msgs] == ["customer", "assistant"] * 3)
        check("assistant row stores route, source and the DB facts used",
              msgs[3]["intent"] == "database" and msgs[3]["source"] == "database"
              and msgs[3]["details"]["audit"][0]["name"] == "Hydra Balance Moisturizer",
              msgs[3]["details"])
        check("memory note is saved in the log",
              "clarifying" in (msgs[3]["details"].get("memory") or ""), msgs[3]["details"].get("memory"))
        check("escalation reply is linked to its escalation record",
              msgs[5]["escalation_id"] == r3["escalation_id"] is not None, (msgs[5]["escalation_id"], r3["escalation_id"]))
        listed = [c for c in db.list_conversations(50) if c["conversation_id"] == chat_id]
        check("conversation appears in the list with 6 messages and its first question",
              listed and listed[0]["message_count"] == 6
              and listed[0]["first_question"] == "How much does it cost?", listed)

        db.close_conversation(chat_id)
        status = [c for c in db.list_conversations(50) if c["conversation_id"] == chat_id][0]["status"]
        check("closing an escalated chat keeps it 'escalated'", status == "escalated", status)
        plain = db.create_conversation()
        try:
            handle_message("How much is the serum?", conversation_id=plain, verbose=False)
            db.close_conversation(plain)
            status = [c for c in db.list_conversations(50) if c["conversation_id"] == plain][0]["status"]
            check("closing a normal chat marks it 'closed'", status == "closed", status)
        finally:
            with db.get_connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE conversation_id = %s", (plain,))
                cur.execute("DELETE FROM conversations WHERE conversation_id = %s", (plain,))
                conn.commit()

        before = len(db.get_conversation_messages(chat_id))
        handle_message("How much is the serum?", verbose=False)
        check("without conversation_id nothing is logged",
              len(db.get_conversation_messages(chat_id)) == before)
    finally:
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE conversation_id = %s", (chat_id,))
            cur.execute("DELETE FROM escalations WHERE conversation_id = %s", (chat_id,))
            cur.execute("DELETE FROM conversations WHERE conversation_id = %s", (chat_id,))
            conn.commit()
        print("(cleaned up the test conversation)")


def main():
    part_a()
    part_b()
    passed = sum(checks)
    print("-" * 60)
    print(f"{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
