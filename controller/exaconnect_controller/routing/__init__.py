"""AI SLA routing (CLAUDE.md §4.3): scoring, Holt forecasting, hysteresis, reasons, shadow mode. M4.

The forecaster sits behind an interface so a trained model can replace it, with a
rules-only baseline engine kept for comparison in tests.
"""
