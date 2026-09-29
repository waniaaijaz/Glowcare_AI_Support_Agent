"""Business settings: the only Python file to edit when adapting the agent to another business.

Also replace the files under data/ (products, policies, FAQ), the database seed,
and PRODUCT_ALIASES below. Nothing else in the code is specific to one brand.
"""

BRAND = "GlowCare"

# Shown in the chat and dashboard sidebars.
DEMO_NOTE = "Fictional demo company. No real products or customers."

# Matches the order numbers customers type (used to decide a lookup is needed).
ORDER_ID_PATTERN = r"\bGC-?\d{5}\b"
ORDER_NUMBER_HINT = "It looks like GC10241 and is in your confirmation email."

# Prices are shown as "$24.99" for USD and "Rs. 1,999" for PKR; other currencies as "12.50 EUR".
DEFAULT_CURRENCY = "USD"

# Other names customers use for each product, matched in addition to the full name.
PRODUCT_ALIASES = {
    "GC-P001": ["hydra balance", "hydra", "moisturizer", "moisturiser"],
    "GC-P002": ["barrier repair", "barrier cream", "repair cream"],
    "GC-P003": ["niacinamide serum", "serum"],
    "GC-P004": ["gentle daily cleanser", "gentle cleanser", "cleanser", "face wash"],
    "GC-P005": ["sunscreen", "spf 50", "spf", "sun cream", "sunblock"],
}

# Words in the brand name aren't evidence that an answer is grounded (see agent/verifier.py).
BRAND_WORDS = [w.lower() for w in BRAND.split()]

# Shown in the example questions of the chat pages.
EXAMPLE_ORDER_ID = "GC10241"
EXAMPLE_PRODUCT = "Niacinamide Serum"
EXAMPLE_INGREDIENT_PRODUCT = "Hydra Balance Moisturizer"
