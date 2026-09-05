"""Every workbook view must render every edge case, report honest metadata, and show what it claims to show."""

import pytest

import serialize
from conftest import EDGE_IDS

VIEWS = serialize.available()
REQUIRED_META = {"digest", "tokens", "chars", "answer_range_in_window", "answer_rows_total", "answer_rows_shown"}


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("tid", EDGE_IDS)
def test_every_view_renders_every_edge_case(tasks, view, tid):
    r = serialize.render(view, tasks[tid]["init_xlsx"], tasks[tid], None)
    assert r.text.strip(), f"{view} produced empty text for {tid}"
    assert REQUIRED_META <= set(r.meta), f"{view} meta missing {REQUIRED_META - set(r.meta)}"
    assert r.meta["tokens"] > 0 and r.meta["chars"] == len(r.text)
    assert r.meta["digest"] == view


@pytest.mark.parametrize("view", VIEWS)
def test_small_sheet_answer_range_is_visible(tasks, view):
    """12307 is 13x9 with the graded cells inside: no view may claim it hid the answer range."""
    r = serialize.render(view, tasks["12307"]["init_xlsx"], tasks["12307"], None)
    assert r.meta["answer_range_in_window"] is True
    assert r.meta["answer_rows_shown"] == r.meta["answer_rows_total"] == 2


@pytest.mark.parametrize("view", ["grid", "compact", "schema", "tsv"])
def test_answer_range_beyond_used_area_counts_as_visible(tasks, view):
    """560-12 grades I2:J7 on a sheet whose data ends at column D: those cells are empty, so visible."""
    r = serialize.render(view, tasks["560-12"]["init_xlsx"], tasks["560-12"], None)
    assert r.meta["answer_range_in_window"] is True


def test_huge_answer_range_is_reported_as_not_fully_visible(tasks):
    r = serialize.render("grid", tasks["17-35"]["init_xlsx"], tasks["17-35"], None)  # I6:M295
    assert r.meta["answer_range_in_window"] is False
    assert 0 < r.meta["answer_rows_shown"] < r.meta["answer_rows_total"] == 290


def test_grid_shows_formulas_values_and_array_formulas(tasks):
    text = serialize.render("grid", tasks["17-35"]["init_xlsx"], tasks["17-35"], None).text
    assert "_xlws.SORT(_xlfn.UNIQUE(B6:B315)) {array G2:G22} -> " in text
    assert "object at 0x" not in text  # never a Python repr
    assert '=">="&I2 -> >=45007' in text


def test_grid_shows_defined_names_and_types(tasks):
    text = serialize.render("grid", tasks["15380"]["init_xlsx"], tasks["15380"], None).text
    assert "codes2 = Sheet3!$D$3:$D$14" in text
    assert "[text" in text and "(date)" not in text  # no dates in this sheet, typed headers present


def test_non_ascii_sheet_names_survive(tasks):
    for tid, name in (("516-46", "ورقة1"), ("560-12", "工作表1")):
        text = serialize.render("grid", tasks[tid]["init_xlsx"], tasks[tid], None).text
        assert name in text


def test_budget_trims_droppable_rows_but_keeps_header_and_answer(tasks):
    full = serialize.render("grid", tasks["17-35"]["init_xlsx"], tasks["17-35"], None)
    tight = serialize.render("grid", tasks["17-35"]["init_xlsx"], tasks["17-35"], 800)
    assert tight.meta["tokens"] < full.meta["tokens"]
    assert "rows dropped for the token budget" in tight.text
    assert "\n5\t" in tight.text            # the header row of the table (row 5) survives
    assert "\n6\t" in tight.text            # first graded row I6 survives
    assert tight.meta["answer_rows_shown"] == full.meta["answer_rows_shown"]


def test_compact_collapses_homogeneous_runs(tasks):
    text = serialize.render("compact", tasks["41-47"]["init_xlsx"], tasks["41-47"], None).text
    assert "same pattern as row" in text
    assert "Columns:" in text and "min " in text


def test_layouts_share_content_with_grid(tasks):
    """Same shown cells, different shape: the graded-range preamble and a data value appear in every layout."""
    for view in ("markdown", "html", "json", "addressed"):
        text = serialize.render(view, tasks["12307"]["init_xlsx"], tasks["12307"], None).text
        assert "Graded answer range: I12:I13" in text
        assert "ABC" in text and "No of countries" in text


def test_unknown_view_raises():
    with pytest.raises(KeyError):
        serialize.render("nope", "x.xlsx", {}, None)
