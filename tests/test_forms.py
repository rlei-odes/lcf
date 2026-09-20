"""Form and paste parsing.

The boolean-column tests are regressions for a real defect: a yes/no select
always submits a value, so blank rows looked filled, spare rows accumulated on
every save, and rows could not be deleted.
"""

import pytest

from lcf.spec.models import Block
from lcf.web.forms import parse_block_value, parse_pasted_table

TEAM = Block.model_validate(
    {
        "key": "members",
        "kind": "table",
        "label": "Team members",
        "columns": [
            {"key": "name", "label": "Name"},
            {"key": "role", "label": "Role"},
            {"key": "is_champion", "label": "Champion", "type": "boolean"},
        ],
    }
)

ACTIONS = Block.model_validate(
    {
        "key": "actions",
        "kind": "table",
        "label": "Actions",
        "columns": [
            {"key": "action", "label": "Action"},
            {"key": "owner", "label": "Owner"},
            {"key": "due", "label": "Due", "type": "date"},
            {"key": "count", "label": "Count", "type": "number"},
            {"key": "kind", "label": "Kind", "type": "enum", "values": ["a", "b"]},
        ],
    }
)


def test_blank_rows_with_a_boolean_are_dropped():
    """The defect: a blank row still submits is_champion=false, which is not content."""
    form = {
        "r0.name": "Sabine Vogt",
        "r0.role": "Champion",
        "r0.is_champion": "true",
        "r1.name": "",
        "r1.role": "",
        "r1.is_champion": "false",
        "r2.name": "",
        "r2.role": "",
        "r2.is_champion": "false",
    }
    rows = parse_block_value(TEAM, form)
    assert len(rows) == 1
    assert rows[0]["name"] == "Sabine Vogt"


def test_saving_twice_does_not_accumulate_rows():
    """Re-saving what was rendered back must be a fixed point, spare rows and all."""
    first = parse_block_value(
        TEAM,
        {
            "r0.name": "Sabine Vogt",
            "r0.role": "Champion",
            "r0.is_champion": "true",
            "r1.name": "",
            "r1.role": "",
            "r1.is_champion": "false",
        },
    )

    # The page re-renders those rows plus two fresh blanks; the user saves again.
    def as_field(value):
        return {True: "true", False: "false"}.get(value, value)

    form = {}
    for i, row in enumerate(first):
        for key, value in row.items():
            form[f"r{i}.{key}"] = as_field(value)
    for i in (len(first), len(first) + 1):
        form.update({f"r{i}.name": "", f"r{i}.role": "", f"r{i}.is_champion": "false"})

    assert parse_block_value(TEAM, form) == first


def test_clearing_a_row_deletes_it():
    form = {
        "r0.name": "Sabine Vogt",
        "r0.role": "Champion",
        "r0.is_champion": "true",
        "r1.name": "Tomas Reiner",
        "r1.role": "Process",
        "r1.is_champion": "false",
    }
    assert len(parse_block_value(TEAM, form)) == 2

    form.update({"r0.name": "", "r0.role": ""})  # the user clears the first row
    remaining = parse_block_value(TEAM, form)
    assert len(remaining) == 1
    assert remaining[0]["name"] == "Tomas Reiner"


def test_a_boolean_alone_is_not_a_row():
    assert parse_block_value(TEAM, {"r0.name": "", "r0.role": "", "r0.is_champion": "true"}) == []


def test_boolean_values_are_preserved_on_real_rows():
    rows = parse_block_value(
        TEAM,
        {
            "r0.name": "Sabine",
            "r0.role": "Champion",
            "r0.is_champion": "true",
            "r1.name": "Tomas",
            "r1.role": "Process",
            "r1.is_champion": "false",
        },
    )
    assert [r["is_champion"] for r in rows] == [True, False]


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [("08.09.2026", "2026-09-08"), ("2026-09-08", "2026-09-08"), ("8/9/2026", "2026-09-08")],
)
def test_dates_normalise_to_iso(raw, expected):
    rows = parse_block_value(ACTIONS, {"r0.action": "x", "r0.due": raw})
    assert rows[0]["due"] == expected


def test_an_unparseable_date_is_left_alone_for_the_checker():
    rows = parse_block_value(ACTIONS, {"r0.action": "x", "r0.due": "next Tuesday"})
    assert rows[0]["due"] == "next Tuesday"


@pytest.mark.parametrize(
    "raw,expected", [("1.234,56", 1234.56), ("12,5", 12.5), ("1450", 1450), ("12.5", 12.5)]
)
def test_numbers_accept_german_formatting(raw, expected):
    rows = parse_block_value(ACTIONS, {"r0.action": "x", "r0.count": raw})
    assert rows[0]["count"] == expected


def test_enum_tolerates_case_from_a_spreadsheet():
    rows = parse_block_value(ACTIONS, {"r0.action": "x", "r0.kind": "A"})
    assert rows[0]["kind"] == "a"


# --------------------------------------------------------------------------- #
# pasting
# --------------------------------------------------------------------------- #


def test_paste_matches_a_header_by_label():
    pasted = "Action\tOwner\tDue\nBlocked stock\tSabine\t08.09.2026\n"
    rows = parse_pasted_table(ACTIONS, pasted)
    assert rows == [
        {"action": "Blocked stock", "owner": "Sabine", "due": "2026-09-08", "count": "", "kind": ""}
    ]


def test_paste_without_a_header_is_positional():
    rows = parse_pasted_table(ACTIONS, "Blocked stock\tSabine\t2026-09-08\n")
    assert rows[0]["action"] == "Blocked stock"
    assert rows[0]["owner"] == "Sabine"


def test_paste_accepts_csv():
    rows = parse_pasted_table(ACTIONS, "Action,Owner\nSorted,Lena\n")
    assert rows[0] == {"action": "Sorted", "owner": "Lena", "due": "", "count": "", "kind": ""}


def test_paste_ignores_blank_lines():
    rows = parse_pasted_table(ACTIONS, "Action\tOwner\nSorted\tLena\n\n\n")
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# other block kinds
# --------------------------------------------------------------------------- #


def test_prose_is_trimmed():
    block = Block.model_validate({"key": "t", "kind": "prose", "label": "T"})
    assert parse_block_value(block, {"value": "  hello  "}) == "hello"


def test_list_splits_lines_and_drops_blanks():
    block = Block.model_validate({"key": "l", "kind": "list", "label": "L"})
    assert parse_block_value(block, {"value": "one\n\n  two  \n"}) == ["one", "two"]
