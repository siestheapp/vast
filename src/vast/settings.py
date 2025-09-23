"""Runtime feature flags for VAST."""

from __future__ import annotations

# Enforce strict identifier validation: do not auto-replan when the user
# references unknown tables or columns. Defaults to True for demo accuracy.
STRICT_IDENTIFIER_MODE: bool = True
