"""Formatting helpers for NorthBorneo's displayed currency."""

CURRENCY_EMOJI = "<:nbr:1566914537343426590>"


def format_currency(amount: int) -> str:
    """Format an integer amount with separators and the custom NBR emoji."""
    return f"{amount:,} {CURRENCY_EMOJI}"
