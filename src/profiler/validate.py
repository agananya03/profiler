"""Validation module for Norwegian Organisation Numbers and Data Integrity.

Provides:
- normalize_orgnr(s): Tolerant of spaces and formatting, returns clean 9-digit string.
- valid_orgnr(s): Norwegian Mod-11 checksum validation.
"""

import re
from typing import Any, Sequence


def normalize_orgnr(s: str) -> str:
    """Normalize a Norwegian organisation number string by removing spaces and formatting.

    Raises:
        ValueError: If cleaned string is not exactly 9 numeric digits.
    """
    if not isinstance(s, str):
        raise ValueError(f"Organisation number must be a string, got {type(s).__name__}")
    cleaned = re.sub(r"\D", "", s)
    if len(cleaned) != 9:
        raise ValueError(f"Invalid organisation number '{s}': must contain exactly 9 digits.")
    return cleaned


# Alias for backward compatibility
clean_orgnr = normalize_orgnr


def valid_orgnr(s: str) -> bool:
    """Validate a Norwegian organisation number using the official Modulo-11 algorithm.

    Weights: 3, 2, 7, 6, 5, 4, 3, 2
    Check digit: 11 - (sum % 11). If remainder is 0 -> 0. If remainder is 1 -> invalid (10).

    Returns:
        bool: True if valid Mod-11 9-digit organisation number, False otherwise.
    """
    try:
        cleaned = normalize_orgnr(s)
    except ValueError:
        return False

    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    total_sum = sum(int(cleaned[i]) * weights[i] for i in range(8))
    remainder = total_sum % 11

    if remainder == 0:
        expected_check = 0
    elif remainder == 1:
        return False  # Remainder 1 yields check digit 10, which is invalid
    else:
        expected_check = 11 - remainder

    actual_check = int(cleaned[8])
    return actual_check == expected_check
