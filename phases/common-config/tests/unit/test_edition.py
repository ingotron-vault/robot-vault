"""The edition vocabulary is one fail-closed cross-service contract."""

import pytest

from robot_vault_config.edition import DEFAULT_EDITION, EDITIONS, read_edition


def test_unset_and_empty_default_to_core():
    assert DEFAULT_EDITION == "core"
    assert read_edition({}) == "core"
    assert read_edition({"ROBOT_VAULT_EDITION": ""}) == "core"
    assert read_edition({"ROBOT_VAULT_EDITION": "   "}) == "core"


@pytest.mark.parametrize("edition", EDITIONS)
def test_known_editions_are_case_and_whitespace_insensitive(edition):
    assert read_edition({"ROBOT_VAULT_EDITION": f"  {edition.upper()}  "}) == edition


@pytest.mark.parametrize("bad", ["community", "enterprise", "unknown", "1"])
def test_unknown_edition_fails_instead_of_guessing(bad):
    with pytest.raises(ValueError, match="is not a known edition"):
        read_edition({"ROBOT_VAULT_EDITION": bad})
