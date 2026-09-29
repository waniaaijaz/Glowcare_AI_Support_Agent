"""Demo server for the WhatsApp-style chat page.

    python -m uvicorn whatsapp_demo:app --port 8000

then open whatsapp_ui.html in a browser.

  POST /chat      {"message": "...", "session": "..."} -> {"reply": "..."}   used by whatsapp_ui.html
                  Each session id is its own saved chat; the page makes a new one on every reload.
  POST /whatsapp  Twilio-format webhook (form fields Body, From) returning TwiML.
                  Written for Twilio's WhatsApp sandbox; not tested on a live number.

Escalations also send an email if GMAIL_ADDRESS, GMAIL_APP_PASSWORD and ALERT_TO
are set in .env; without them the case is still saved and only the email is skipped.

Demo only: no authentication or rate limiting. Don't expose it to the internet.
"""

import os
import smtplib
from email.message import EmailMessage
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from agent.settings import BRAND
from agent.support_agent import handle_message
from database import db

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
ALERT_TO = os.getenv("ALERT_TO", "").strip() or GMAIL_ADDRESS

app = FastAPI(title=f"{BRAND} WhatsApp demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_conversations = {}


def get_conversation_id(sender: str) -> int:
    """Saved-chat id for a sender, created on first message (kept in memory, so it resets on restart)."""
    if sender not in _conversations:
        _conversations[sender] = db.create_conversation(channel="whatsapp")
    return _conversations[sender]


def send_escalation_email(customer_message: str, reason: str) -> None:
    """Email the team that a customer needs a human. Skipped if email isn't configured."""
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_TO):
        print("EMAIL SKIPPED: set GMAIL_ADDRESS, GMAIL_APP_PASSWORD and ALERT_TO in .env")
        return
    msg = EmailMessage()
    msg["Subject"] = f"[{BRAND}] Human needed - {reason}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_TO
    msg.set_content(
        f"A customer needs a human.\n\nReason: {reason}\nMessage: {customer_message}"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def _alert_if_escalated(message: str, result: dict) -> None:
    """Email the team for cases that need a person now. Low-priority cases ("not in the documents")
    are saved and shown in the dashboard, but not emailed."""
    if not result.get("escalate") or result.get("escalation_priority") == "low":
        return
    try:
        send_escalation_email(message, result.get("escalation_reason") or "escalation")
    except Exception as e:
        print("EMAIL FAILED:", e)


class ChatRequest(BaseModel):
    message: str
    session: str = "demo-client"


@app.post("/chat")
async def chat(request: ChatRequest):
    result = handle_message(request.message, conversation_id=get_conversation_id(request.session))
    _alert_if_escalated(request.message, result)
    return {"reply": result["answer"]}


@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    result = handle_message(Body, conversation_id=get_conversation_id(From))
    _alert_if_escalated(Body, result)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(result['answer'])}</Message></Response>"
    )
    return Response(content=twiml, media_type="application/xml")
