"""Guard: no 8-digit hex colours in holler's Qt stylesheets.

Qt parses `#XXXXXXXX` as #AARRGGBB, not CSS's #RRGGBBAA — so a "20% blue"
written as `#1f6feb33` renders as an opaque-ish green, and `#f5a52422` (meant
as a soft amber wash) renders brick red. Translucent colours must be written as
`rgba(r, g, b, a)` instead.
"""
from __future__ import annotations

import re
from pathlib import Path

_HOLLER = Path(__file__).resolve().parent.parent / "holler"
# `&#12345678;` HTML entities and identifiers ending in a hex run are excluded.
_ARGB_HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{8}(?![0-9a-fA-F])")


def test_no_eight_digit_hex_colours_in_holler_sources():
    files = sorted(_HOLLER.rglob("*.py"))
    assert files, f"no sources found under {_HOLLER}"
    hits = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _ARGB_HEX.finditer(line):
                hits.append(f"{path.relative_to(_HOLLER.parent)}:{lineno}: {m.group(0)}")
    assert not hits, (
        "8-digit hex colours found (Qt reads them as #AARRGGBB, not "
        "#RRGGBBAA) — use rgba(r,g,b,a) instead:\n" + "\n".join(hits)
    )


def test_pattern_catches_the_original_bug():
    assert _ARGB_HEX.search('_BG_ROW_SELECTED = "#1f6feb33"')
    assert _ARGB_HEX.search("background: #f5a52422;")
    assert not _ARGB_HEX.search("color: #1f6feb;")
    assert not _ARGB_HEX.search("&#12345678;")
