from client.components.jalali_date import (
    _adjacent_month,
    _calendar_cells,
    _month_label,
    _month_number,
)


def test_adjacent_jalali_month_rolls_across_years():
    assert _adjacent_month(1405, 1, -1) == (1404, 12)
    assert _adjacent_month(1405, 12, 1) == (1406, 1)


def test_calendar_grid_contains_every_day_once_and_complete_weeks():
    cells = _calendar_cells(1405, 6)
    current_month_days = [day for year, month, day, outside in cells if not outside]

    assert len(cells) % 7 == 0
    assert len(cells) >= 35
    assert current_month_days == list(range(1, 32))


def test_month_label_round_trips_with_persian_digits():
    label = _month_label(6)

    assert label == "۰۶ - شهریور"
    assert _month_number(label, 1) == 6
