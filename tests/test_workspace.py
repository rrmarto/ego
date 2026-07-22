from pathlib import Path

from ego.models import Evidence, EvidenceStatus
from ego.workspace import resolve_workspace, revalidate_evidence, validate_evidence


def evidence(path: str = "source.py") -> Evidence:
    return Evidence(
        path=path,
        line_start=1,
        line_end=1,
        explanation="The first line establishes the behavior.",
        critical=True,
    )


def test_citation_is_verified_and_detects_change(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("first\nsecond\n", encoding="utf-8")

    validated = validate_evidence(tmp_path, evidence())
    assert validated.status is EvidenceStatus.CITATION_VERIFIED
    assert validated.file_sha256
    assert validated.fragment_sha256

    source.write_text("changed\nsecond\n", encoding="utf-8")
    assert revalidate_evidence(tmp_path, validated).status is EvidenceStatus.STALE


def test_evidence_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-ego.txt"
    outside.write_text("secret", encoding="utf-8")
    checked = validate_evidence(tmp_path, evidence("../outside-ego.txt"))
    assert checked.status is EvidenceStatus.INVALID


def test_verified_citation_does_not_relabel_the_model_explanation_as_true(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("except ValueError, IndexError:\n    pass\n", encoding="utf-8")
    claim = Evidence(
        path="source.py",
        line_start=1,
        line_end=2,
        explanation="This syntax is invalid.",
        critical=True,
    )

    checked = validate_evidence(tmp_path, claim)

    assert checked.status is EvidenceStatus.CITATION_VERIFIED
    assert checked.explanation == "This syntax is invalid."


def test_workspace_must_be_directory(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    try:
        resolve_workspace(file)
    except ValueError as error:
        assert "not a directory" in str(error)
    else:
        raise AssertionError("expected ValueError")
