from decimal import Decimal, InvalidOperation


def number(value, *, positive=False):
    """Reject non-finite, negative, boolean, or unparseable risk inputs."""
    if isinstance(value, bool):
        raise ValueError("boolean risk input")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid risk input") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("out-of-range risk input")
    return result
