"""When a dataset is next checked."""

from datetime import datetime, timedelta

import pytest

from geodata_sync.schedule import next_check

NOW = datetime(2026, 11, 10, 2, 0)


@pytest.mark.parametrize("update, last, expected", [
    ("manual", NOW, None),
    ("daily", None, NOW),  # never checked: due now
    ("weekly", NOW - timedelta(days=1), NOW - timedelta(days=1) + timedelta(days=7) - timedelta(hours=1)),
    ("weekly", NOW - timedelta(days=8), NOW),
    ("0 3 * * *", NOW - timedelta(minutes=30), datetime(2026, 11, 10, 3, 0)),
])
def test_next_check(update, last, expected):
    assert next_check(update, last, NOW) == expected
