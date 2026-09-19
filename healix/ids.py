"""Stable-ID normalization.

Many frameworks emit element ids with volatile segments: run indices
(``user-4471``), UUIDs, and colon-delimited positional prefixes such as Oracle
ADF's ``pt1:r1:0:soc1::content``. ``normalize_id`` rewrites those segments to
placeholders so two runs of the same page produce the same normalized id.

The rules are generic pattern rules — nothing here is vendor-specific.
"""

from __future__ import annotations

import re

UUID_PLACEHOLDER = "{uuid}"
HEX_PLACEHOLDER = "{hex}"
NUMBER_PLACEHOLDER = "{n}"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Long hex-looking token (>= 8 chars) with at least one digit and one a-f letter,
# delimited by non-alphanumerics, e.g. the "a3f9c2d81b" in "btn_a3f9c2d81b".
_HEX_FRAGMENT = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_DIGIT_RUN = re.compile(r"\d+")


def normalize_id(raw_id: str | None) -> str | None:
    """Return ``raw_id`` with volatile segments replaced by placeholders.

    ``None``/empty ids stay ``None``. An id with nothing volatile in it is
    returned unchanged, so callers can always compare normalized forms directly.
    """
    if not raw_id:
        return None
    normalized = _UUID.sub(UUID_PLACEHOLDER, raw_id)
    normalized = _HEX_FRAGMENT.sub(HEX_PLACEHOLDER, normalized)
    # Digit runs cover both plain run indices and ADF-style positional prefixes.
    return _DIGIT_RUN.sub(NUMBER_PLACEHOLDER, normalized)
