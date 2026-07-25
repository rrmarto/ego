from ego.tui.portrait import halfcell_portrait


def test_halfcell_portrait_uses_two_colored_pixels_per_cell() -> None:
    portrait = halfcell_portrait()

    assert portrait.plain.splitlines() == ["▀" * 44] * 21
    assert len(portrait.spans) == 44 * 21
    assert all(span.style.color is not None for span in portrait.spans)
    assert all(span.style.bgcolor is not None for span in portrait.spans)
