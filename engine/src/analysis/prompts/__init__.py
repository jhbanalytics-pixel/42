"""Prompt templates for the Phase 2 analysis layer.

Each module in this package builds a single Gemini prompt + matching
response schema for a specific brief shape. Keep prompts small and
single-purpose so they compose: one for trend synthesis (description +
rationale + activation idea), additional ones can layer on later
without forcing a giant monolithic call.
"""
