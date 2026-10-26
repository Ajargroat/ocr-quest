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


# Persian grade names -> the smallint digit both grade columns store.
GRADE_WORDS = {"دهم": 10, "یازدهم": 11, "دوازدهم": 12}


def parse_grade(value):
    """'12', '۱۲' or 'دوازدهم' -> 12; a value that is not a grade -> None.

    The sources/questions/answers grade columns are smallint, and a Persian
    grade name is what a profile carries by default, so every writer and
    reader reasons about the same number through this one helper.
    """
    text = to_english_digits(norm_text(value))
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if 1 <= number <= 12 else None
    return GRADE_WORDS.get(text)


def norm_scalar(v):
    """Normalized comparable form: numbers collapse 12/12.0/'۱۲' -> '12'.

    Lives here rather than in a feature module because both the OCR lane's
    question-number matching and the answer-pairing code compare the same way.
    """
    if v is None:
        return None
    n = parse_number(to_english_digits(str(v)))
    if n is not None:
        f = float(n)
        return str(int(f)) if f.is_integer() else str(f)
    t = norm_text(to_english_digits(str(v))).lower()
    return t or None
