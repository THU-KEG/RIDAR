"""The verbatim-number check shared by the offline and online detectors."""

from __future__ import annotations

import re

# Numbers whose precision matters: decimals (including dotted tokens such as
# versions and addresses) and exponents. Plain integers are left to the judge;
# requiring them verbatim mostly produced rejections.
NUMBER = re.compile(r"\d+(?:\.\d+)+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+")


def number_in(number: str, record: str) -> bool:
    """True when ``number`` occurs in ``record`` as a whole token: not a prefix or
    suffix of a longer number. A sentence period after it is not a decimal point."""
    return re.search(r"(?<![\d.])" + re.escape(number) + r"(?!\.?\d)", record) is not None
