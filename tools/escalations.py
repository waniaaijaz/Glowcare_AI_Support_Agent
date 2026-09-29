"""Work through escalations from the command line.

    python -m tools.escalations                      # open cases, most urgent first
    python -m tools.escalations all                  # every case, including resolved
    python -m tools.escalations update 3 in_progress
    python -m tools.escalations update 3 resolved "Called customer, sent a replacement"
"""

import sys

from database import db

PRIORITY_MARK = {"high": "!!! HIGH", "normal": "normal", "low": "low"}


def show(rows):
    if not rows:
        print("No escalations to show.")
        return
    for r in rows:
        print(f"ESC-{r['escalation_id']:<4} {PRIORITY_MARK.get(r['priority'], r['priority']):<9} "
              f"{r['status']:<12} {r['created_at']:%Y-%m-%d %H:%M}  chat #{r['conversation_id']}")
        print(f"          reason : {r['reason']}")
        if r.get("customer_message"):
            print(f"          message: {r['customer_message'][:150]!r}")
        if r.get("staff_notes"):
            print(f"          notes  : {r['staff_notes']}")
        print()


def main(args):
    if not args:
        rows = [r for r in db.list_escalations() if r["status"] != "resolved"]
        print(f"Open escalations: {len(rows)}\n")
        show(rows)
    elif args[0] == "all":
        show(db.list_escalations())
    elif args[0] == "update" and len(args) >= 3:
        escalation_id = int(args[1].upper().replace("ESC-", ""))
        notes = " ".join(args[3:]) or None
        try:
            row = db.update_escalation(escalation_id, args[2], notes)
        except ValueError:
            print(f"'{args[2]}' isn't a valid status. Use: pending, in_progress or resolved.")
            return 1
        if row is None:
            print(f"No escalation ESC-{escalation_id} found.")
            return 1
        print(f"ESC-{escalation_id} is now '{row['status']}'.")
        show([row])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
