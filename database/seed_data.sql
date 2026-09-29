-- GlowCare demo data (fictional). Safe to run more than once.
-- The product rows mirror data/products/products.json.

INSERT INTO products (product_id, name, description, price, currency, stock_quantity, availability, ingredients) VALUES
('GC-P001', 'Hydra Balance Moisturizer', 'A lightweight, oil-free daily moisturizer designed to hydrate without clogging pores.', 24.99, 'USD', 42, 'in_stock',
    ARRAY['Water', 'Glycerin', 'Hyaluronic Acid', 'Niacinamide', 'Dimethicone', 'Phenoxyethanol']),
('GC-P002', 'Barrier Repair Cream', 'A rich, ceramide-based cream formulated to support and restore the skin''s natural moisture barrier.', 32.50, 'USD', 17, 'in_stock',
    ARRAY['Water', 'Ceramide NP', 'Cholesterol', 'Fatty Acids', 'Shea Butter', 'Squalane']),
('GC-P003', 'Niacinamide Serum', 'A 10% niacinamide serum aimed at improving the appearance of uneven texture and visible pores.', 19.99, 'USD', 0, 'out_of_stock',
    ARRAY['Water', 'Niacinamide 10%', 'Zinc PCA', 'Glycerin', 'Panthenol']),
('GC-P004', 'Gentle Daily Cleanser', 'A sulfate-free, low-pH cleanser that removes impurities without stripping the skin.', 16.00, 'USD', 65, 'in_stock',
    ARRAY['Water', 'Coco-Glucoside', 'Glycerin', 'Panthenol', 'Allantoin']),
('GC-P005', 'Lightweight Sunscreen SPF 50', 'A broad-spectrum SPF 50 sunscreen with a light, non-greasy finish.', 21.00, 'USD', 8, 'in_stock',
    ARRAY['Water', 'Zinc Oxide', 'Titanium Dioxide', 'Glycerin', 'Dimethicone'])
ON CONFLICT (product_id) DO NOTHING;

INSERT INTO customers (name, email) VALUES
('Sara Ahmed', 'sara.ahmed@example.com'),
('Bilal Khan', 'bilal.khan@example.com'),
('Ayesha Raza', 'ayesha.raza@example.com')
ON CONFLICT (email) DO NOTHING;

-- Orders look customers up by email, so the script works whatever ids they got.
INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10241', customer_id, 'shipped', 24.99 FROM customers WHERE email = 'sara.ahmed@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10242', customer_id, 'processing', 32.50 FROM customers WHERE email = 'bilal.khan@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10243', customer_id, 'delivered', 37.00 FROM customers WHERE email = 'ayesha.raza@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO order_items (order_id, product_id, quantity, price_at_purchase)
SELECT v.order_id, v.product_id, v.quantity, v.price
FROM (VALUES
    ('GC10241', 'GC-P001', 1, 24.99),
    ('GC10242', 'GC-P002', 1, 32.50),
    ('GC10243', 'GC-P004', 1, 16.00),
    ('GC10243', 'GC-P005', 1, 21.00)
) AS v(order_id, product_id, quantity, price)
WHERE NOT EXISTS (
    SELECT 1 FROM order_items oi WHERE oi.order_id = v.order_id AND oi.product_id = v.product_id
);
