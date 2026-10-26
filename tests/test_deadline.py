from datetime import datetime

import pytest

from pipeline import parse_deadline

NOW = datetime(2026, 10, 26, 12, 0)


@pytest.mark.parametrize("item,expected", [
    ({"deadline_text": "10 Nov' 26"}, "2026-11-10T00:00:00"),           # Internshala
    ({"apply_by": "May 5th, 2026"}, "2026-05-05T00:00:00"),             # letsintern
    ({"deadline": "5 days left"}, "2026-10-31T00:00:00"),               # Unstop
    ({"deadline": "Apply by 15 Nov 2026"}, "2026-11-15T00:00:00"),
    ({"deadline_date": "2026-12-01T00:00:00"}, "2026-12-01T00:00:00"),  # already parsed
    ({"apply_by": "N/A"}, None),
    ({"deadline": "garbage"}, None),
    ({}, None),
])
def test_parse_deadline(item, expected):
    assert parse_deadline(item, NOW) == expected
