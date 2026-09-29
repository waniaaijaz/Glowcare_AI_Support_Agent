"""Read-only queries behind the admin dashboard.

Each function takes optional `days` and `channel` filters and returns plain
dicts or lists ready to display.
"""

from database.db import get_connection


def _filters(days, channel, time_column, channel_column="c.channel"):
    """SQL conditions and parameters for the optional days / channel filters."""
    clauses, params = [], []
    if days:
        clauses.append(f"{time_column} >= NOW() - make_interval(days => %s)")
        params.append(int(days))
    if isinstance(channel, (list, tuple)):
        clauses.append(f"{channel_column} = ANY(%s)")
        params.append(list(channel))
    elif channel:
        clauses.append(f"{channel_column} = %s")
        params.append(channel)
    return ("".join(f" AND {c}" for c in clauses), params)


def _all(query, params):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]


def _one(query, params):
    rows = _all(query, params)
    return rows[0] if rows else {}


def message_stats(days=None, channel=None) -> dict:
    """Chat and reply counts, split by where the answers came from."""
    where, params = _filters(days, channel, "m.created_at")
    return _one(f"""
        SELECT
            COUNT(DISTINCT m.conversation_id)                                        AS chats,
            COUNT(*) FILTER (WHERE m.sender = 'customer')                            AS questions,
            COUNT(*) FILTER (WHERE m.sender = 'assistant')                           AS replies,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.source = 'database')       AS from_database,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.source = 'knowledge_base') AS from_documents,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.source = 'fixed_reply')    AS fixed_replies,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.escalation_id IS NOT NULL) AS handed_to_human,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.intent = 'clarify')        AS clarifying,
            COUNT(*) FILTER (WHERE m.sender = 'assistant' AND m.details->>'memory' IS NOT NULL) AS memory_used,
            COUNT(*) FILTER (WHERE m.answer_type = 'llm')                            AS ai_accepted,
            COUNT(*) FILTER (WHERE m.answer_type = 'quoted_source')                  AS ai_quoted,
            COUNT(*) FILTER (WHERE m.answer_type = 'not_found')                      AS not_found
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE TRUE {where};
    """, params)


def escalation_stats(days=None, channel=None) -> dict:
    """Escalations created, open now, open high-priority, and average time to resolve.
    """
    where_created, p1 = _filters(days, channel, "e.created_at")
    where_channel, p2 = _filters(None, channel, "e.created_at")
    where_resolved, p3 = _filters(days, channel, "e.resolved_at")
    return _one(f"""
        SELECT
            (SELECT COUNT(*) FROM escalations e JOIN conversations c USING (conversation_id)
              WHERE TRUE {where_created})                                           AS created,
            (SELECT COUNT(*) FROM escalations e JOIN conversations c USING (conversation_id)
              WHERE e.status <> 'resolved' {where_channel})                         AS open_now,
            (SELECT COUNT(*) FROM escalations e JOIN conversations c USING (conversation_id)
              WHERE e.status <> 'resolved' AND e.priority = 'high' {where_channel}) AS open_high,
            (SELECT ROUND(AVG(EXTRACT(EPOCH FROM (e.resolved_at - e.created_at)) / 3600)::numeric, 1)
               FROM escalations e JOIN conversations c USING (conversation_id)
              WHERE e.status = 'resolved' AND e.resolved_at IS NOT NULL {where_resolved}) AS avg_resolve_hours;
    """, p1 + p2 + p2 + p3)


def daily_activity(days=None, channel=None) -> list:
    """Questions per day."""
    where, params = _filters(days, channel, "m.created_at")
    return _all(f"""
        SELECT m.created_at::date                               AS day,
               COUNT(DISTINCT m.conversation_id)                AS chats,
               COUNT(*) FILTER (WHERE m.sender = 'customer')    AS questions
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE TRUE {where}
        GROUP BY day ORDER BY day;
    """, params)


def top_products(days=None, channel=None, limit=10) -> list:
    """Products most often mentioned in replies."""
    where, params = _filters(days, channel, "m.created_at")
    return _all(f"""
        SELECT p.product_id, p.name, COUNT(*) AS mentions
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(m.details->'route_info'->'product_ids', '[]'::jsonb)) AS pid(product_id)
        JOIN products p ON p.product_id = pid.product_id
        WHERE m.sender = 'assistant' {where}
        GROUP BY p.product_id, p.name
        ORDER BY mentions DESC, p.name
        LIMIT %s;
    """, params + [limit])


def route_breakdown(days=None, channel=None) -> list:
    """How many replies each route produced."""
    where, params = _filters(days, channel, "m.created_at")
    return _all(f"""
        SELECT m.intent AS route, COUNT(*) AS replies
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE m.sender = 'assistant' AND m.intent IS NOT NULL {where}
        GROUP BY m.intent ORDER BY replies DESC;
    """, params)


def escalation_reasons(days=None, channel=None) -> list:
    """Escalation counts by reason."""
    where, params = _filters(days, channel, "e.created_at")
    return _all(f"""
        SELECT SPLIT_PART(e.reason, ' + ', 1) AS reason, COUNT(*) AS cases
        FROM escalations e
        JOIN conversations c ON c.conversation_id = e.conversation_id
        WHERE TRUE {where}
        GROUP BY 1 ORDER BY cases DESC, reason;
    """, params)


_PAIRED = """
    SELECT a.message_id, a.conversation_id, a.created_at, a.content AS answer,
           a.answer_type, a.details, q.content AS question
    FROM messages a
    JOIN conversations c ON c.conversation_id = a.conversation_id
    JOIN LATERAL (
        SELECT content FROM messages q
        WHERE q.conversation_id = a.conversation_id AND q.sender = 'customer'
          AND q.message_id < a.message_id
        ORDER BY q.message_id DESC LIMIT 1
    ) q ON TRUE
    WHERE a.sender = 'assistant' AND a.answer_type = %s {where}
"""


def unanswered_questions(days=None, channel=None, limit=50) -> list:
    """Questions the documents couldn't answer, grouped by wording, most frequent first.
    """
    where, params = _filters(days, channel, "a.created_at")
    return _all(f"""
        SELECT MIN(question) AS question, COUNT(*) AS times_asked,
               MAX(created_at) AS last_asked,
               ARRAY_AGG(DISTINCT conversation_id ORDER BY conversation_id) AS chats
        FROM ({_PAIRED.format(where=where)}) paired
        GROUP BY LOWER(TRIM(question))
        ORDER BY times_asked DESC, last_asked DESC
        LIMIT %s;
    """, ["not_found"] + params + [limit])


def rejected_ai_answers(days=None, channel=None, limit=50) -> list:
    """LLM answers the fact-checker rejected (the customer saw the quoted source instead).
    """
    where, params = _filters(days, channel, "a.created_at")
    rows = _all(f"""
        {_PAIRED.format(where=where)}
        ORDER BY a.created_at DESC
        LIMIT %s;
    """, ["quoted_source"] + params + [limit])
    for r in rows:
        audit = (r.pop("details") or {}).get("audit") or {}
        r["ai_wrote"] = audit.get("llm_answer")
        r["checker_reasons"] = audit.get("problems") or []
    return rows


def list_conversations(days=None, channel=None, status=None, search=None, limit=100) -> list:
    """Chats with their first question, status and latest escalation, optionally filtered or searched.
    """
    where, params = _filters(days, channel, "m.created_at")
    extra, extra_params = "", []
    if status:
        extra += " AND c.status = %s"
        extra_params.append(status)
    if search:
        extra += (" AND EXISTS (SELECT 1 FROM messages s WHERE s.conversation_id = c.conversation_id"
                  " AND s.content ILIKE %s)")
        extra_params.append(f"%{search}%")
    return _all(f"""
        SELECT c.conversation_id, c.channel, c.status, c.started_at,
               COUNT(m.message_id)                                   AS messages,
               MAX(m.created_at)                                     AS last_message_at,
               (ARRAY_AGG(m.content ORDER BY m.message_id)
                    FILTER (WHERE m.sender = 'customer'))[1]         AS first_question,
               (SELECT MAX(e.escalation_id) FROM escalations e
                 WHERE e.conversation_id = c.conversation_id)        AS escalation_id
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.conversation_id
        WHERE TRUE {where} {extra}
        GROUP BY c.conversation_id
        ORDER BY MAX(m.created_at) DESC
        LIMIT %s;
    """, params + extra_params + [limit])


def list_escalations(which="open", channel=None) -> list:
    """Escalations with their channel and how long they have been (or were) open."""
    where, params = _filters(None, channel, "e.created_at")
    if which == "open":
        where += " AND e.status <> 'resolved'"
    elif which == "resolved":
        where += " AND e.status = 'resolved'"
    return _all(f"""
        SELECT e.*, c.channel,
               ROUND((EXTRACT(EPOCH FROM (COALESCE(e.resolved_at, NOW()) - e.created_at)) / 3600)::numeric, 1)
                   AS hours_waiting
        FROM escalations e
        JOIN conversations c ON c.conversation_id = e.conversation_id
        WHERE TRUE {where}
        ORDER BY CASE e.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                 e.created_at;
    """, params)


def channels() -> list:
    """Channels that have at least one chat."""
    return [r["channel"] for r in _all(
        "SELECT DISTINCT channel FROM conversations ORDER BY channel;", [])]
