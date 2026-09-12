"""Small shared helpers (identical behaviour to the JS code nodes)."""
import hashlib

FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"   # Persian digits
AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"   # Arabic digits


def to_english_digits(value) -> str:
    out = str(value)
    for i, d in enumerate(FA_DIGITS):
        out = out.replace(d, str(i))
    for i, d in enumerate(AR_DIGITS):
        out = out.replace(d, str(i))
    return out


def to_persian_digits(value) -> str:
    """ASCII (and Arabic-Indic) digits -> Persian digits. Used for prose text;
    math spans must keep English digits (the reverse conversion)."""
    out = str(value)
    for i, d in enumerate(AR_DIGITS):
        out = out.replace(d, FA_DIGITS[i])
    for i in range(9, -1, -1):
        out = out.replace(str(i), FA_DIGITS[i])
    return out


def norm_text(value) -> str:
    """Collapse all whitespace runs into single spaces (JS normText)."""
    return " ".join(str(value or "").split()).strip()


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def parse_number(value):
    """'12' or '۱۲' -> 12, '12.5' -> 12.5, anything else -> None."""
    text = to_english_digits(value).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
