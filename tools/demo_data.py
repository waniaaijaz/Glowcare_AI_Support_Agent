"""Fills the dashboard with sample chats, so there is something to show in a demo.

Every chat goes through the real agent (router, database, memory, escalations,
and the LLM for policy questions), so the numbers are genuine. The chats are
then spread over the past week and use channel 'demo', so the dashboard can
show them separately and they can be removed completely.

    python -m tools.demo_data              # add the demo chats
    python -m tools.demo_data --no-ai      # add them without LLM calls (skips policy questions)
    python -m tools.demo_data --remove     # delete every demo chat, message and escalation
"""

import os

# The chats are saved in English, so skip the extra translation calls.
os.environ.setdefault("MATCH_CUSTOMER_LANGUAGE", "off")
os.environ.setdefault("TRANSLATE_FOR_SEARCH", "off")

import sys

from database import db
from agent.support_agent import handle_message

CHANNEL = "demo"

DEMO_CHATS = [
    (6, ["Hi", "What ingredients are in the Hydra Balance Moisturizer?", "How much is it?", "thanks"]),
    (6, ["Is the Niacinamide Serum in stock?", "And the sunscreen?"]),
    (5, ["AI:Can I return a product after opening it?"]),
    (5, ["Where is my order GC10241?"]),
    (4, ["I got a rash after using the cleanser"]),
    (4, ["How much does it cost?", "Barrier Repair Cream", "Is it in stock?"]),
    (3, ["AI:Do you sell products in the UK?"]),
    (3, ["AI:Do you have a loyalty rewards program?"]),
    (2, ["I want to talk to a human"]),
    (2, ["AI:How long do refunds take?", "AI:How do I cancel my order?"]),
    (1, ["AI:Do you offer gift cards?"]),
    (1, ["What's the status of order GC99999?", "Where is my order GC10242?"]),
    (0, ["This is terrible, my parcel is late, I want to complain"]),
    (0, ["Is the sunscreen available?", "What about the cleanser?",
         "AI:Are your products tested on animals?"]),
]

STAFF_ACTIONS = {
    "I got a rash after using the cleanser":
        ("resolved", "Called customer, advised to stop use, refund issued"),
    "I want to talk to a human":
        ("in_progress", "Emailed customer, waiting for a reply"),
}


def demo_chat_ids():
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM conversations WHERE channel = %s", (CHANNEL,))
        return [r["conversation_id"] for r in cur.fetchall()]


def remove():
    ids = demo_chat_ids()
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE conversation_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM escalations WHERE conversation_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM conversations WHERE conversation_id = ANY(%s)", (ids,))
        conn.commit()
    print(f"Removed {len(ids)} demo chats (and their messages and escalations).")


def shift_back(chat_id, days):
    """Move a chat's timestamps `days` into the past."""
    if not days:
        return
    with db.get_connection() as conn, conn.cursor() as cur:
        interval = f"{int(days)} days"
        cur.execute("UPDATE conversations SET started_at = started_at - %s::interval "
                    "WHERE conversation_id = %s", (interval, chat_id))
        cur.execute("UPDATE messages SET created_at = created_at - %s::interval "
                    "WHERE conversation_id = %s", (interval, chat_id))
        cur.execute("UPDATE escalations SET created_at = created_at - %s::interval, "
                    "resolved_at = resolved_at - %s::interval WHERE conversation_id = %s",
                    (interval, interval, chat_id))
        conn.commit()


def add(use_ai: bool):
    if demo_chat_ids():
        print("Demo chats are already in the database. To start fresh, run:\n"
              "    python -m tools.demo_data --remove\nthen run this again.")
        return 1
    chats = 0
    for days_ago, messages in DEMO_CHATS:
        texts = [m[3:] if m.startswith("AI:") else m for m in messages
                 if use_ai or not m.startswith("AI:")]
        if not texts:
            continue
        chat_id = db.create_conversation(channel=CHANNEL)
        for text in texts:
            result = handle_message(text, conversation_id=chat_id, verbose=False)
            print(f"  chat #{chat_id}: {text!r} -> {result['route']}"
                  + (f" (ESC-{result['escalation_id']})" if result.get("escalation_id") else ""))
        action = STAFF_ACTIONS.get(texts[0])
        if action:
            escalation = db.get_open_escalation(chat_id)
            if escalation:
                db.update_escalation(escalation["escalation_id"], action[0], action[1])
                if action[0] == "resolved":
                    with db.get_connection() as conn, conn.cursor() as cur:
                        cur.execute("UPDATE escalations SET resolved_at = created_at + INTERVAL "
                                    "'2 hours 40 minutes' WHERE escalation_id = %s",
                                    (escalation["escalation_id"],))
                        conn.commit()
        if days_ago == 6 or days_ago == 5:
            db.close_conversation(chat_id)
        shift_back(chat_id, days_ago)
        chats += 1
    print(f"\nAdded {chats} demo chats (channel 'demo'). Open the dashboard and pick "
          "'Demo data' in the Channel filter, or 'All channels'.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--remove" in args:
        remove()
        sys.exit(0)
    sys.exit(add(use_ai="--no-ai" not in args))
