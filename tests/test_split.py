"""Cutting a long paste into parts, at boundaries a reader would choose."""

from lcf.ingest.split import split_text

PARA = "One sentence here. Another one follows it. " * 4  # ~172 chars


def test_text_within_the_upper_bound_is_one_part():
    assert split_text("short text", 100, 15) == ["short text"]
    assert split_text("x" * 115, 100, 15) == ["x" * 115]


def test_a_cut_prefers_a_blank_line_near_the_target():
    text = "\n\n".join([PARA.strip()] * 6)
    parts = split_text(text, 400, 25)
    assert len(parts) > 1
    assert all(p.endswith(".") for p in parts), "every part ends on a whole sentence"
    assert " ".join(" ".join(parts).split()) == " ".join(text.split()), "nothing lost"


def test_without_paragraphs_a_cut_falls_after_a_sentence():
    parts = split_text(PARA * 5, 300, 20)
    assert len(parts) > 1
    assert all(p.endswith(".") for p in parts)


def test_a_heading_is_not_cut_off_from_what_it_introduces():
    text = PARA.strip() + "\n\n## Containment\n\n" + PARA.strip() + "\n\n" + PARA.strip()
    for part in split_text(text, len(PARA) + 20, 30):
        assert not part.rstrip().endswith("## Containment")


def test_unbroken_text_is_cut_hard_rather_than_not_at_all():
    parts = split_text("x" * 1000, 300, 10)
    assert "".join(parts) == "x" * 1000
    assert len(parts) == 4
