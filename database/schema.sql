-- GlowCare database schema (fictional demo data).
--
-- Holds everything the agent must never guess: products, prices, stock, orders.
-- The document knowledge base (policies, FAQ) lives in Chroma, not here.
--
--   createdb glowcare
--   psql -d glowcare -f database/schema.sql
--   psql -d glowcare -f database/seed_data.sql

CREATE TABLE IF NOT EXISTS customers (
    customer_id     SERIAL PRIMARY KEY,
    name            VARCHAR(150) NOT NULL,
    email           VARCHAR(150) UNIQUE NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Stock lives on the product row; a separate inventory table isn't needed
-- until a client has several warehouses.
CREATE TABLE IF NOT EXISTS products (
    product_id      VARCHAR(20) PRIMARY KEY,               -- e.g. GC-P001
    name            VARCHAR(150) NOT NULL,
    description     TEXT,
    price           NUMERIC(10, 2) NOT NULL,
    currency        VARCHAR(5) NOT NULL DEFAULT 'USD',
    stock_quantity  INTEGER,                                 -- null when only availability is known
    availability    VARCHAR(20) NOT NULL DEFAULT 'in_stock', -- in_stock | out_of_stock | unknown
    ingredients     TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        VARCHAR(20) PRIMARY KEY,               -- e.g. GC10241
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    status          VARCHAR(30) NOT NULL,                  -- placed | processing | shipped | delivered | cancelled
    total_amount    NUMERIC(10, 2) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id       SERIAL PRIMARY KEY,
    order_id            VARCHAR(20) NOT NULL REFERENCES orders(order_id),
    product_id          VARCHAR(20) NOT NULL REFERENCES products(product_id),
    quantity            INTEGER NOT NULL,
    price_at_purchase   NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id     SERIAL PRIMARY KEY,
    customer_id         INTEGER REFERENCES customers(customer_id),   -- null for anonymous chats
    started_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    status              VARCHAR(20) NOT NULL DEFAULT 'open',         -- open | closed | escalated
    channel             VARCHAR(20) NOT NULL DEFAULT 'web'           -- web | whatsapp | demo | test
);

-- escalation_id is added to messages after the escalations table exists (below).
CREATE TABLE IF NOT EXISTS messages (
    message_id          SERIAL PRIMARY KEY,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(conversation_id),
    sender              VARCHAR(10) NOT NULL,        -- customer | assistant
    content             TEXT NOT NULL,
    intent              VARCHAR(40),                 -- the route the router chose
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    source              VARCHAR(20),                 -- database | knowledge_base | fixed_reply
    answer_type         VARCHAR(20),                 -- llm | quoted_source | not_found
    details             JSONB,                       -- audit trail: route, database rows or chunks, checker result
    escalation_id       INTEGER
);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id       SERIAL PRIMARY KEY,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(conversation_id),
    reason              TEXT NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',      -- pending | in_progress | resolved
    priority            VARCHAR(10) NOT NULL DEFAULT 'normal',       -- high | normal | low
    customer_message    TEXT,
    staff_notes         TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMP
);

DO $$ BEGIN
    ALTER TABLE messages ADD CONSTRAINT messages_escalation_id_fkey
        FOREIGN KEY (escalation_id) REFERENCES escalations(escalation_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_channel ON conversations (channel);
