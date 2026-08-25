from __future__ import annotations

from status.skills.client import _skill_version_label


class SkillVersionStub:
    """Older SDK shape: id only, no version field."""

    id = "skillver_01TEST"


class VersionCreateResponseStub:
    version = "1759178010641129"
    id = "skillver_01TEST"


def test_skill_version_label_prefers_version_field() -> None:
    assert _skill_version_label(VersionCreateResponseStub()) == "1759178010641129"


def test_skill_version_label_falls_back_to_id() -> None:
    assert _skill_version_label(SkillVersionStub()) == "skillver_01TEST"
