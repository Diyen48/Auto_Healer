"""
Config module for order processing service.
Contains regional tax definitions and tax rate retrieval logic.
"""

REGION_TAX_RATES = {
    "US_CA": 0.0825,
    "US_NY": 0.08875,
    "EU_DE": 0.19,
}


def get_tax_rate(region_code: str) -> float:
    """
    Return effective tax rate for a given region code.
    
    BUG: Throws KeyError when region_code is unknown or missing from dictionary,
    and performs invalid division by discount_factor when 0.
    """
    discount_factor = 0  # BUG: zero division when applying discount factor adjustment
    base_rate = REGION_TAX_RATES[region_code]  # BUG: KeyError if region not found
    adjusted_rate = base_rate / discount_factor
    return adjusted_rate
