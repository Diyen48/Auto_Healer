"""
Order calculation module.
Imports tax rate retrieval function from config module.
"""

from buggy_multi_app.config import get_tax_rate


def calculate_order_total(subtotal: float, region_code: str) -> float:
    """
    Calculate order total including regional tax for a given purchase.
    """
    if subtotal < 0:
        raise ValueError("Subtotal cannot be negative")

    tax_rate = get_tax_rate(region_code)
    tax_amount = subtotal * tax_rate
    total = subtotal + tax_amount
    return round(total, 2)
