"""Small helpers for the live-call webhook: identity check and quote vs. budget."""

import re

IDENTITY_PREFIX = "Am I speaking with"  # keep in sync with engine_agent.calling_agent
_NEG_RE = re.compile(
    r"^\W*(no|nope|nah)\b(?!\s+(problem|worries))|wrong (number|place)|(this|that) (is not|isn'?t)|"
    r"you (have|'ve) (the )?wrong|different (place|restaurant|hotel)|not (a|the) (hotel|restaurant)", re.I)
_MULT = {"hundred": 100, "k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
         "crore": 1e7, "million": 1e6}
_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_WORDS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
               "seventy": 70, "eighty": 80, "ninety": 90})


def denies_identity(speech: str) -> bool:
    """True when the vendor says we've reached the wrong place."""
    return bool(_NEG_RE.search(speech or ""))


def parse_amount(text: str) -> float | None:
    """First money amount in spoken/typed text: '5,000', '5000 rupees', '5 thousand',
    'five thousand', '2 lakh', 'twelve hundred'. None if no amount is found."""
    t = (text or "").lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|hundred|thousand|lakhs?|lacs?|crore|million)?\b", t)
    if m:
        return float(m.group(1)) * _MULT.get(m.group(2) or "", 1)
    total, current, seen = 0.0, 0.0, False
    for tok in re.findall(r"[a-z]+", t):
        if tok in _WORDS:
            current += _WORDS[tok]; seen = True
        elif tok == "hundred" and seen:
            current = (current or 1) * 100
        elif tok in _MULT and seen:
            total += (current or 1) * _MULT[tok]; current = 0
        elif seen and tok not in ("and", "rupees", "rupee", "rs", "dollars", "only"):
            break
    return (total + current) if seen else None


def quote_total(st: dict, speech: str) -> float | None:
    """Total cost implied by the vendor's quote, or None if it can't be worked out."""
    amount = parse_amount(speech)
    if not amount:
        return None
    if st.get("mode") == "hotel":  # per-night, per-room rate
        return amount * (st.get("nights") or 1) * (st.get("rooms") or 1)
    per_person = re.search(r"per (person|head|plate|guest)|a head|each|\bpp\b", speech, re.I)
    return amount * (st.get("guests") or 1) if per_person else amount


def discount_question(st: dict, total: float) -> str:
    if st.get("mode") == "hotel":
        basis = f"for {st.get('rooms') or 1} room(s) over {st.get('nights') or 1} night(s)"
    else:
        basis = "in total"
    return (f"Thanks. That comes to about {total:,.0f} {basis}, which is a bit over our budget. "
            f"Could you offer some discount to make it work?")
