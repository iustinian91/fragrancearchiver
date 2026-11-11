"""The levels of each vote chart, in display order. One copy, used by the scraper, clean.py and
the smoke test, so they cannot drift apart.

Longevity and sillage are stored under the original site's names (the redesigned site calls the
levels very weak..eternal and intimate..enormous; the scraper maps them by position)."""

LONGEVITY_KEYS = ("poor", "weak", "moderate", "long lasting", "very long lasting")
SILLAGE_KEYS = ("soft", "moderate", "heavy", "enormous")
GENDER_KEYS = ("female", "more female", "unisex", "more male", "male")
WHEN_KEYS = ("winter", "spring", "summer", "fall", "day", "night")

CHART_KEYS = {
    "longevity": LONGEVITY_KEYS,
    "sillage": SILLAGE_KEYS,
    "gender": GENDER_KEYS,
    "when_to_wear": WHEN_KEYS,
}
