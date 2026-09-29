"""Review saved chats from the command line.

    python -m tools.conversations            # the 20 most recent chats
    python -m tools.conversations 7          # every message of chat #7
    python -m tools.conversations 7 --why    # plus why each reply was given
"""

import json
import sys

from database import db


def list_chats():
    rows = db.list_conversations(20)
    if not rows:
        print("No saved chats yet.")
        return
    print(f"{'chat':<6}{'status':<11}{'msgs':<6}{'last message':<18}first question")
    for r in rows:
        print(f"#{r['conversation_id']:<5}{r['status']:<11}{r['message_count']:<6}"
              f"{r['last_message_at']:%Y-%m-%d %H:%M}  {(r['first_question'] or '')[:60]!r}")
    print("\nOpen one with: python -m tools.conversations <chat number>")


def show_chat(chat_id: int, why: bool):
    msgs = db.get_conversation_messages(chat_id)
    if not msgs:
        print(f"No messages found for chat #{chat_id}.")
        return 1
    print(f"Chat #{chat_id}: {len(msgs)} messages\n")
    for m in msgs:
        who = "Customer " if m["sender"] == "customer" else "Assistant"
        print(f"[{m['created_at']:%H:%M:%S}] {who}: {m['content']}")
        if m["sender"] == "assistant":
            d = m.get("details") or {}
            tags = [f"route={m['intent']}", f"source={m['source']}"]
            if m.get("answer_type"):
                tags.append(f"answer={m['answer_type']}")
            if m.get("escalation_id"):
                tags.append(f"escalation=ESC-{m['escalation_id']}")
            print(f"           ({', '.join(tags)})")
            if d.get("memory"):
                print(f"           memory: {d['memory']}")
            if why:
                print(f"           reason: {d.get('reason')}")
                audit = d.get("audit")
                if isinstance(audit, dict) and audit.get("chunks_used") is not None:
                    for c in audit["chunks_used"]:
                        print(f"           doc: {c.get('section') or c.get('source')} (distance {c.get('distance')})")
                    if audit.get("problems"):
                        print(f"           checker rejected AI answer: {'; '.join(audit['problems'])}")
                elif audit:
                    print(f"           data: {json.dumps(audit, default=str)[:300]}")
        print()
    return 0


def main(args):
    if not args:
        list_chats()
        return 0
    try:
        chat_id = int(args[0].lstrip("#"))
    except ValueError:
        print(__doc__)
        return 1
    return show_chat(chat_id, why="--why" in args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
