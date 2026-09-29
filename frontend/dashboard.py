"""Admin dashboard for the business team.

    python -m streamlit run frontend/dashboard.py --server.port 8502

Four tabs: overview, escalations (work the queue), conversations (search and
read transcripts) and answer quality (questions the documents couldn't answer,
LLM answers the checker rejected). Set DASHBOARD_PASSWORD in .env before
sharing it, since it shows customer chats.
"""

import hmac
import json
import os
import sys
from datetime import date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import altair as alt
import pandas as pd
import streamlit as st

from agent.settings import BRAND, EXAMPLE_ORDER_ID
from database import db
from database import dashboard_queries as q

st.set_page_config(page_title=f"{BRAND} Admin", layout="wide")

PERIODS = {"Today": 1, "Last 7 days": 7, "Last 30 days": 30, "All time": None}
CHANNEL_LABELS = {"web": "Website chat", "demo": "Demo data", "test": "Test data",
                  "whatsapp": "WhatsApp"}
PRIORITY_BADGE = {
    "high": ("High priority", ":material/warning:", "red"),
    "normal": ("Normal priority", ":material/person:", "blue"),
    "low": ("Low priority", ":material/help:", "gray"),
}
STATUS_BADGE = {
    "pending": ("Pending", ":material/schedule:", "orange"),
    "in_progress": ("In progress", ":material/autorenew:", "blue"),
    "resolved": ("Resolved", ":material/check_circle:", "green"),
}
STATUS_LABELS = {"pending": "Pending", "in_progress": "In progress", "resolved": "Resolved"}
REASON_LABELS = {
    "possible skin reaction / safety concern": "Possible skin reaction",
    "customer asked for a human": "Asked for a person",
    "complaint": "Complaint",
    "information not available": "Info not in documents",
}


def require_password():
    """Ask for DASHBOARD_PASSWORD, if one is set."""
    expected = os.getenv("DASHBOARD_PASSWORD", "")
    if not expected:
        return
    if st.session_state.get("admin_ok"):
        return
    st.title(f"{BRAND} Admin")
    with st.form("login"):
        entered = st.text_input("Dashboard password", type="password")
        if st.form_submit_button("Open dashboard"):
            if hmac.compare_digest(entered.encode(), expected.encode()):
                st.session_state.admin_ok = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def is_dark_mode() -> bool:
    """True when the page is using a dark theme."""
    configured = st.get_option("theme.base")
    if configured in ("light", "dark"):
        return configured == "dark"
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


def palette():
    """Chart colours for the current theme."""
    if is_dark_mode():
        return {"series": "#3987e5", "text": "#c3c2b7"}
    return {"series": "#2a78d6", "text": "#52514e"}


def hbar(df, label_col, value_col, value_title):
    """Horizontal bar chart with value labels."""
    colors = palette()
    height = max(90, 36 * len(df) + 30)
    base = alt.Chart(df).encode(
        y=alt.Y(f"{label_col}:N", sort="-x", title=None,
                axis=alt.Axis(labelLimit=260, ticks=False, domain=False)),
        x=alt.X(f"{value_col}:Q", title=value_title,
                scale=alt.Scale(domain=[0, max(1, float(df[value_col].max()) * 1.15)], nice=False),
                axis=alt.Axis(tickMinStep=1, format="d", grid=True, domain=False)),
        tooltip=[alt.Tooltip(f"{label_col}:N", title=""), alt.Tooltip(f"{value_col}:Q", title=value_title)],
    )
    bars = base.mark_bar(color=colors["series"], cornerRadiusEnd=4, size=16)
    labels = base.mark_text(align="left", dx=6, color=colors["text"]).encode(text=f"{value_col}:Q")
    st.altair_chart((bars + labels).properties(height=height), width="stretch")


def daily_columns(rows, days):
    """Column chart of questions per day, including days with none."""
    colors = palette()
    df = pd.DataFrame(rows)
    if df.empty:
        return
    df["day"] = pd.to_datetime(df["day"])
    start = (pd.Timestamp(date.today() - timedelta(days=days - 1)) if days
             else df["day"].min())
    all_days = pd.DataFrame({"day": pd.date_range(start, pd.Timestamp(date.today()), freq="D")})
    df = all_days.merge(df, on="day", how="left").fillna({"chats": 0, "questions": 0})
    df[["chats", "questions"]] = df[["chats", "questions"]].astype(int)
    df["label"] = df["day"].dt.strftime("%d %b")
    step = max(1, len(df) // 8)
    keep = set(df["label"].iloc[::-1][::step])
    chart = alt.Chart(df).mark_bar(color=colors["series"], cornerRadiusEnd=4).encode(
        x=alt.X("label:O", title=None, sort=list(df["label"]),
                scale=alt.Scale(paddingInner=0.25),
                axis=alt.Axis(labelAngle=0, ticks=False,
                              labelExpr="indexof(" + str(sorted(keep)).replace("'", '"')
                                        + ", datum.value) >= 0 ? datum.value : ''")),
        y=alt.Y("questions:Q", title="Customer questions",
                axis=alt.Axis(tickMinStep=1, format="d", domain=False)),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%a %d %b %Y"),
                 alt.Tooltip("questions:Q", title="Questions"),
                 alt.Tooltip("chats:Q", title="Chats")],
    ).properties(height=240)
    st.altair_chart(chart, width="stretch")
    with st.expander("Show as table"):
        st.dataframe(df.drop(columns="label").assign(day=df["day"].dt.date).rename(
            columns={"day": "Day", "chats": "Chats", "questions": "Questions"}),
            hide_index=True, width="stretch")


def pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "–"


def fmt_time(ts):
    return ts.strftime("%d %b %Y, %H:%M") if ts else "–"


def duration(hours):
    h = float(hours or 0)
    if h < 1:
        return "less than an hour"
    if h < 48:
        return f"{round(h)} hour{'s' if round(h) != 1 else ''}"
    return f"{round(h / 24)} days"


def badge(spec):
    label, icon, color = spec
    st.badge(label, icon=icon, color=color)


def render_transcript(chat_id: int, show_why: bool = True):
    """Show one chat; with show_why, add the route and evidence behind each reply."""
    messages = db.get_conversation_messages(chat_id)
    if not messages:
        st.info("This chat has no messages.")
        return
    for m in messages:
        role = "user" if m["sender"] == "customer" else "assistant"
        with st.chat_message(role):
            st.markdown(m["content"])
            if m["sender"] != "assistant":
                st.caption(fmt_time(m["created_at"]))
                continue
            d = m.get("details") or {}
            tags = [fmt_time(m["created_at"]), f"route: {m['intent']}", f"source: {m['source']}"]
            if m.get("answer_type"):
                tags.append(f"result: {m['answer_type']}")
            if m.get("escalation_id"):
                tags.append(f"escalation ESC-{m['escalation_id']}")
            st.caption(" · ".join(tags))
            if d.get("memory"):
                st.caption(f"Used earlier messages: {d['memory']}")
            if not show_why:
                st.caption(f"Reason: {d.get('reason') or '–'}")
                continue
            with st.expander("Why this reply"):
                st.markdown(f"**Reason:** {d.get('reason') or '–'}")
                audit = d.get("audit")
                if isinstance(audit, dict) and "chunks_used" in audit:
                    for c in audit.get("chunks_used") or []:
                        st.markdown(f"- {c.get('section') or c.get('source')} · distance {c.get('distance')}")
                    if audit.get("llm_answer"):
                        st.markdown("**What the AI wrote:**")
                        st.text(audit["llm_answer"])
                    for p in audit.get("problems") or []:
                        st.markdown(f"- {'' if audit.get('llm_error') else 'Checker: '}{p}")
                elif audit:
                    st.json(json.loads(json.dumps(audit, default=str)), expanded=False)
                else:
                    st.markdown("No database or document lookup was needed.")


def tab_overview(days, channel):
    """Headline numbers and charts."""
    s = q.message_stats(days, channel)
    e = q.escalation_stats(days, channel)
    if not s.get("questions"):
        st.info("No chats in this period yet. Chat on the customer page, or add sample chats "
                "with `python -m tools.demo_data`.", icon=":material/info:")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Chats", s.get("chats", 0), border=True)
    c2.metric("Customer questions", s.get("questions", 0), border=True)
    replies = s.get("replies", 0)
    c3.metric("Handled without a human", pct(replies - s.get("handed_to_human", 0), replies),
              help="Share of replies that did not hand the customer to a person.", border=True)
    c4.metric("Open escalations", e.get("open_now", 0),
              delta=f"{e.get('open_high', 0)} high priority" if e.get("open_high") else None,
              delta_color="inverse", delta_arrow="off",
              help="Cases waiting for the team right now, whatever the period.", border=True)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Escalations opened", e.get("created", 0), border=True)
    c6.metric("Avg. time to resolve",
              f"{e['avg_resolve_hours']} h" if e.get("avg_resolve_hours") is not None else "–",
              border=True)
    c7.metric("Follow-ups understood", s.get("memory_used", 0),
              help="Replies where the agent used earlier messages (e.g. 'is it in stock?').",
              border=True)
    c8.metric("Clarifying questions asked", s.get("clarifying", 0), border=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Questions per day")
        rows = q.daily_activity(days, channel)
        if rows:
            daily_columns(rows, days)
        else:
            st.caption("No activity in this period.")
    with right:
        st.subheader("Where answers came from")
        src = pd.DataFrame([
            {"Source": "Database (exact facts)", "Replies": s.get("from_database", 0)},
            {"Source": "Company documents (AI + checker)", "Replies": s.get("from_documents", 0)},
            {"Source": "Standard replies", "Replies": s.get("fixed_replies", 0)},
        ])
        if src["Replies"].sum():
            hbar(src, "Source", "Replies", "Replies")
            st.caption("Standard replies = greetings, clarifying questions and hand-off messages.")
            with st.expander("Show as table"):
                st.dataframe(src, hide_index=True, width="stretch")
        else:
            st.caption("No replies in this period.")

    left, right = st.columns(2)
    with left:
        st.subheader("Most-asked-about products")
        products = pd.DataFrame(q.top_products(days, channel))
        if not products.empty:
            hbar(products.rename(columns={"name": "Product", "mentions": "Questions"}),
                 "Product", "Questions", "Questions")
            with st.expander("Show as table"):
                st.dataframe(products.rename(columns={"product_id": "ID", "name": "Product",
                                                      "mentions": "Questions"}),
                             hide_index=True, width="stretch")
        else:
            st.caption("No product questions in this period.")
    with right:
        st.subheader("Why customers were handed to a human")
        reasons = pd.DataFrame(q.escalation_reasons(days, channel))
        if not reasons.empty:
            reasons["reason"] = reasons["reason"].map(REASON_LABELS).fillna(reasons["reason"])
            hbar(reasons.rename(columns={"reason": "Reason", "cases": "Cases"}),
                 "Reason", "Cases", "Cases")
            with st.expander("Show as table"):
                st.dataframe(reasons.rename(columns={"reason": "Reason", "cases": "Cases"}),
                             hide_index=True, width="stretch")
        else:
            st.caption("No hand-offs in this period.")


def tab_escalations(channel):
    """Escalation queue: open cases first, with status and staff notes."""
    which = st.segmented_control("Show", ["Open", "Resolved", "All"], default="Open",
                                 key="esc_filter") or "Open"
    rows = q.list_escalations(which.lower(), channel)
    if not rows:
        st.success("Nothing here: no escalations match this filter.", icon=":material/check:")
        return
    st.caption(f"{len(rows)} case(s). Most urgent first, then longest waiting.")
    for r in rows:
        eid = r["escalation_id"]
        with st.container(border=True):
            head = st.columns([1.2, 1.2, 1.3, 4])
            with head[0]:
                st.markdown(f"**ESC-{eid}**")
            with head[1]:
                badge(PRIORITY_BADGE.get(r["priority"], (r["priority"], None, "gray")))
            with head[2]:
                badge(STATUS_BADGE.get(r["status"], (r["status"], None, "gray")))
            waited = "open for" if r["status"] != "resolved" else "resolved after"
            head[3].caption(f"Chat #{r['conversation_id']} · "
                            f"{CHANNEL_LABELS.get(r['channel'], r['channel'])} · "
                            f"created {fmt_time(r['created_at'])} · {waited} {duration(r['hours_waiting'])}")
            st.markdown(f"**Reason:** {r['reason']}")
            if r.get("customer_message"):
                for line in r["customer_message"].split("\n---\n"):
                    st.markdown(f"> {line}")
            if r.get("staff_notes"):
                st.markdown(f"**Staff notes:** {r['staff_notes']}")
            with st.expander("Show the conversation"):
                render_transcript(r["conversation_id"], show_why=False)
            with st.form(f"update-{eid}", border=False):
                cols = st.columns([1.5, 4, 1])
                statuses = list(db.ESCALATION_STATUSES)
                new_status = cols[0].selectbox("Status", statuses, index=statuses.index(r["status"]),
                                               format_func=STATUS_LABELS.get, key=f"st-{eid}")
                note = cols[1].text_input("Add a note (optional)", key=f"note-{eid}",
                                          placeholder="e.g. Called customer, sent a replacement")
                cols[2].markdown("&nbsp;")
                if cols[2].form_submit_button("Save", width="stretch"):
                    notes = r.get("staff_notes") or ""
                    if note.strip():
                        stamp = date.today().strftime("%d %b")
                        notes = (notes + "\n" if notes else "") + f"[{stamp}] {note.strip()}"
                    db.update_escalation(eid, new_status, notes or None)
                    st.toast(f"ESC-{eid} saved: {STATUS_LABELS[new_status]}", icon=":material/check:")
                    st.rerun()


def tab_conversations(days, channel):
    """Search and browse saved chats."""
    f1, f2 = st.columns([1, 3])
    status = f1.selectbox("Status", ["Any", "open", "closed", "escalated"], key="conv_status")
    search = f2.text_input("Search messages", placeholder=f"e.g. rash, {EXAMPLE_ORDER_ID}, refund",
                           key="conv_search")
    rows = q.list_conversations(days, channel, None if status == "Any" else status,
                                search.strip() or None)
    if not rows:
        st.info("No chats match these filters.", icon=":material/search_off:")
        return
    df = pd.DataFrame([{
        "Chat": f"#{r['conversation_id']}",
        "Last message": r["last_message_at"],
        "Status": r["status"],
        "Channel": CHANNEL_LABELS.get(r["channel"], r["channel"]),
        "Messages": r["messages"],
        "First question": r["first_question"] or "",
        "Escalation": f"ESC-{r['escalation_id']}" if r["escalation_id"] else "",
    } for r in rows])
    st.caption(f"{len(rows)} chat(s). Click a row to open it.")
    picked = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                          selection_mode="single-row", key="conv_table",
                          column_config={"Last message": st.column_config.DatetimeColumn(
                              format="D MMM YYYY, HH:mm")})
    selected = picked.selection.rows if picked and picked.selection else []
    chat_id = rows[selected[0]]["conversation_id"] if selected else None
    if chat_id is None:
        st.caption("Select a chat above to read it.")
        return
    st.subheader(f"Chat #{chat_id}")
    render_transcript(chat_id)


def tab_quality(days, channel):
    """Questions the documents couldn't answer, and LLM answers the checker rejected."""
    s = q.message_stats(days, channel)
    kb_total = s.get("ai_accepted", 0) + s.get("ai_quoted", 0) + s.get("not_found", 0)
    st.caption("Only questions answered from company documents are counted here; database "
               "answers never use the AI.")
    c1, c2, c3 = st.columns(3)
    c1.metric("AI answer accepted", s.get("ai_accepted", 0), help="Passed every check.",
              delta=pct(s.get("ai_accepted", 0), kb_total), delta_color="off", delta_arrow="off",
              border=True)
    c2.metric("Policy text quoted instead", s.get("ai_quoted", 0),
              help="The AI answer failed a check (or the AI service was down), so the customer "
                   "got the source text word for word.",
              delta=pct(s.get("ai_quoted", 0), kb_total), delta_color="off", delta_arrow="off",
              border=True)
    c3.metric("Not in the documents", s.get("not_found", 0),
              help="Nothing relevant was found; the customer was offered a human.",
              delta=pct(s.get("not_found", 0), kb_total), delta_color="off", delta_arrow="off",
              border=True)

    st.subheader("Questions the documents couldn't answer")
    st.caption("This is what the knowledge base is missing. Add the answers to a file in "
               "`data/`, then run `python rag/ingest.py`.")
    un = q.unanswered_questions(days, channel)
    if un:
        st.dataframe(pd.DataFrame([{
            "Question": r["question"].strip(), "Times asked": r["times_asked"],
            "Last asked": r["last_asked"], "Chats": ", ".join(f"#{c}" for c in r["chats"]),
        } for r in un]), hide_index=True, width="stretch",
            column_config={"Last asked": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})
    else:
        st.success("Every document question in this period was answered.", icon=":material/check:")

    st.subheader("AI answers the checker rejected")
    st.caption("The customer got the policy text instead, so nothing wrong reached them. "
               "Many rejections on one topic can mean the documents or the prompt need work.")
    rej = q.rejected_ai_answers(days, channel)
    if not rej:
        st.success("No AI answers were rejected in this period.", icon=":material/check:")
    for r in rej:
        with st.expander(f"{fmt_time(r['created_at'])} · chat #{r['conversation_id']} · {r['question']}"):
            if r["ai_wrote"]:
                st.markdown("**What the AI wrote (not shown to the customer):**")
                st.text(r["ai_wrote"])
            st.markdown("**Why it was rejected:**" if r["ai_wrote"] else
                        "**Why no AI answer was used:**")
            for reason in r["checker_reasons"]:
                st.markdown(f"- {reason}")
            st.markdown("**What the customer got instead:**")
            st.markdown(r["answer"])


def main():
    """Page layout: sidebar filters plus the four tabs."""
    require_password()
    try:
        channel_options = [None] + q.channels()
    except Exception as e:
        st.error("Can't reach the database. Check that PostgreSQL is running and `.env` is set up, "
                 f"and that database/schema.sql has been loaded.\n\nDetails: {type(e).__name__}: {e}")
        st.stop()

    with st.sidebar:
        st.subheader(f"{BRAND} Admin")
        st.caption("Team view, not for customers.")
        period = st.radio("Period", list(PERIODS), index=2)
        channel = st.selectbox("Channel", channel_options,
                               format_func=lambda c: "All channels" if c is None
                               else CHANNEL_LABELS.get(c, c))
        if st.button("Refresh", icon=":material/refresh:", width="stretch"):
            st.rerun()
        st.caption("Numbers are live from the database.")
        if not os.getenv("DASHBOARD_PASSWORD"):
            st.caption(":material/lock_open: No password set. Fine on your own computer; "
                       "add DASHBOARD_PASSWORD to .env before sharing this page.")
    days = PERIODS[period]

    st.title(f"{BRAND} Admin")
    open_now = q.escalation_stats(None, channel).get("open_now", 0)
    tabs = st.tabs(["Overview", f"Escalations ({open_now} open)", "Conversations", "Answer quality"])
    with tabs[0]:
        tab_overview(days, channel)
    with tabs[1]:
        tab_escalations(channel)
    with tabs[2]:
        tab_conversations(days, channel)
    with tabs[3]:
        tab_quality(days, channel)


main()
