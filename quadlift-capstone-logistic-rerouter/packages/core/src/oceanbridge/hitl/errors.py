"""Errors raised when an action would bypass a business rule or the human-in-the-loop gate."""


class PolicyViolation(Exception):
    """Raised when an action would bypass the HITL approval gate or another commit rule."""
