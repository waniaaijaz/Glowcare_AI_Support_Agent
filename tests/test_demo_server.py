"""Checks whatsapp_demo.py (the demo server) with FastAPI's test client.

Covers the /chat and /whatsapp endpoints, escalation emails (SMTP is faked, so
nothing is sent) and the case where email isn't configured. Needs PostgreSQL.
"""

import os

os.environ.setdefault("MATCH_CUSTOMER_LANGUAGE", "off")
os.environ.setdefault("TRANSLATE_FOR_SEARCH", "off")

import sys
from unittest import mock

from fastapi.testclient import TestClient

import whatsapp_demo
from database import db

checks = []


def check(name, condition, detail=""):
    checks.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"\n      got: {detail}" if detail and not condition else ""))


def cleanup(conversation_ids):
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE conversation_id = ANY(%s)", (conversation_ids,))
        cur.execute("DELETE FROM escalations WHERE conversation_id = ANY(%s)", (conversation_ids,))
        cur.execute("DELETE FROM conversations WHERE conversation_id = ANY(%s)", (conversation_ids,))
        conn.commit()


def main():
    client = TestClient(whatsapp_demo.app)
    whatsapp_demo._conversations.clear()
    configured = dict(GMAIL_ADDRESS="team@example.com", GMAIL_APP_PASSWORD="app-password",
                      ALERT_TO="alerts@example.com")
    try:
        r = client.post("/chat", json={"message": "How much is the Gentle Daily Cleanser?"})
        check("/chat answers a price question from the database",
              r.status_code == 200 and "$16.00" in r.json()["reply"], r.text)

        with mock.patch.multiple(whatsapp_demo, **configured), \
             mock.patch.object(whatsapp_demo.smtplib, "SMTP_SSL") as smtp:
            r = client.post("/chat", json={"message": "I got a rash after using the cleanser"})
            server = smtp.return_value.__enter__.return_value
            sent = server.send_message.call_args.args[0] if server.send_message.called else None
        check("a safety message escalates and returns a reference number",
              r.status_code == 200 and "ESC-" in r.json()["reply"], r.text)
        check("the team is emailed once, to the alert address",
              sent is not None and sent["To"] == "alerts@example.com"
              and "safety" in sent["Subject"].lower() and server.send_message.call_count == 1,
              sent["Subject"] if sent else "no email")
        check("email login uses the configured account", server.login.call_args.args == ("team@example.com", "app-password"))

        with mock.patch.multiple(whatsapp_demo, GMAIL_ADDRESS="", GMAIL_APP_PASSWORD="", ALERT_TO=""), \
             mock.patch.object(whatsapp_demo.smtplib, "SMTP_SSL") as smtp:
            r = client.post("/chat", json={"message": "I want to talk to a human"})
        check("email not configured: the customer still gets a reply and no email is attempted",
              r.status_code == 200 and "ESC-" in r.json()["reply"] and not smtp.called, r.text)

        with mock.patch.multiple(whatsapp_demo, **configured), \
             mock.patch.object(whatsapp_demo.smtplib, "SMTP_SSL", side_effect=OSError("smtp down")):
            r = client.post("/chat", json={"message": "This is terrible, I want to complain"})
        check("email failure never blocks the customer's reply",
              r.status_code == 200 and "ESC-" in r.json()["reply"], r.text)

        stub = {"answer": "I'm sorry, I don't have that information.", "answer_type": "not_found", "chunks_used": [],
                "problems": [], "llm_error": None}
        with mock.patch.multiple(whatsapp_demo, **configured), \
             mock.patch.object(whatsapp_demo.smtplib, "SMTP_SSL") as smtp, \
             mock.patch("agent.responder.answer_question", return_value=stub):
            r = client.post("/chat", json={"message": "do you sell lip balm", "session": "no-answer"})
        check("a question the documents can't answer is saved as a case but not emailed",
              r.status_code == 200 and "ESC-" in r.json()["reply"] and not smtp.called, r.text)

        r = client.post("/chat", json={"message": "hi"})
        check("greeting works", r.status_code == 200 and "assistant" in r.json()["reply"].lower(), r.text)

        r1 = client.post("/whatsapp", data={"Body": "Where is my order GC10241 & is it <ok>?", "From": "whatsapp:+10000000001"})
        check("/whatsapp returns TwiML", r1.status_code == 200 and r1.headers["content-type"].startswith("application/xml")
              and r1.text.startswith("<?xml") and "<Message>" in r1.text, r1.text)
        check("/whatsapp reply is XML-safe and has the order status", "shipped" in r1.text and "<ok>" not in r1.text, r1.text)

        client.post("/whatsapp", data={"Body": "How much does it cost?", "From": "whatsapp:+10000000002"})
        r2 = client.post("/whatsapp", data={"Body": "Hydra Balance Moisturizer", "From": "whatsapp:+10000000002"})
        check("each sender has their own chat, so memory works", "$24.99" in r2.text, r2.text)
        r3 = client.post("/whatsapp", data={"Body": "Is it in stock?", "From": "whatsapp:+10000000003"})
        check("a different sender doesn't inherit that memory", "Which product" in r3.text, r3.text)

        a = client.post("/chat", json={"message": "I want to talk to a human", "session": "page-1"}).json()["reply"]
        b = client.post("/chat", json={"message": "I want to talk to a human", "session": "page-2"}).json()["reply"]
        check("a reloaded page (new session) gets a new case, not 'already with our team'",
              "already with our team" not in b and a != b, f"{a!r} / {b!r}")
        r = client.post("/chat", json={})
        check("/chat without a message is rejected", r.status_code == 422, r.status_code)
    finally:
        ids = list(whatsapp_demo._conversations.values())
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT conversation_id FROM conversations WHERE channel = 'whatsapp'")
            ids += [row["conversation_id"] for row in cur.fetchall()]
        cleanup(sorted(set(ids)))
        print(f"(cleaned up {len(set(ids))} test conversations)")

    print("-" * 60)
    print(f"{sum(checks)}/{len(checks)} passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
