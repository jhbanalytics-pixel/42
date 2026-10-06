"""System observability: a queryable event history + a fatal-event marker.

``events.record_event`` lands one append-only row in the system_events table for
any notable system event; ``events.track`` wraps a risky block so the cause of a
failure is both logged and recorded instead of being swallowed. Both are
non-fatal by contract: observability must never break what it observes.
"""

from src.observability.events import record_event, track

__all__ = ["record_event", "track"]
