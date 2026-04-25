"""
convolearn/sim_conditions.py

Condition registry for multi-condition simulation.
Adding a new condition or topic: one dict entry each.
"""

# condition_id → (prompt_level, include_domain_map)
CONDITIONS: dict[str, tuple[str, bool]] = {
    "socratic":       ("socratic", True),
    "instructed-map": ("instructed", True),
    "bare-map":       ("bare", True),
}

# prompt_id → short slug used in session IDs
TOPIC_SLUGS: dict[str, str] = {
    "i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d": "topo-map",
}
