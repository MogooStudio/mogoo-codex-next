"""Local performance metadata, deliberately separate from billable token usage."""
from __future__ import annotations

import math


MILLISECONDS = {
    "headers_ms", "first_text_ms", "first_suggestion_ms", "completed_ms", "request_finished_ms",
    "generation_ms", "verification_queue_ms", "verification_read_ms", "verification_ms",
    "displayed_ms", "display_after_ready_ms",
    "preview_ready_ms", "preview_verification_ms", "display_after_preview_ms",
    "all_displayed_ms",
}
CHOICES = {"response_mode": {"sse", "json"}, "display_stage": {"preview", "final"},
           "service_tier": {"auto", "default", "standard", "priority", "fast", "flex", "scale"}}


def clean_performance(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in MILLISECONDS:
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 86_400_000:
            result[key] = round(number, 3)
    for key, choices in CHOICES.items():
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    return result
