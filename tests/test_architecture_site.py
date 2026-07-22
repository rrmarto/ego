from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from ego.tui.state import PHASE_LABELS


class ArchitecturePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.add(element_id)
        attribute = "href" if tag in {"a", "link"} else "src" if tag == "script" else None
        if attribute and (reference := values.get(attribute)):
            self.references.append(reference)


def test_architecture_page_is_self_contained_and_tracks_protocol_phases() -> None:
    repository = Path(__file__).parents[1]
    page = repository / "docs" / "index.html"
    source = page.read_text(encoding="utf-8")
    parser = ArchitecturePageParser()
    parser.feed(source)

    for reference in parser.references:
        parsed = urlparse(reference)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.path:
            assert (page.parent / parsed.path).is_file(), reference
        if parsed.fragment:
            assert parsed.fragment in parser.ids, reference

    for phase_label in PHASE_LABELS.values():
        assert phase_label in source
