"""Keep first-party package metadata aligned with the repository license.

Dependency manifests and vendored notices describe third-party terms. They do
not license Robot Vault itself. The repository's root LICENSE (AGPL-3.0) does.
If the root license file is ever missing, first-party packages must not claim
an OSI license that the repository does not grant.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
FIRST_PARTY_MANIFESTS = tuple(sorted((REPO_ROOT / "phases").glob("*/pyproject.toml"))) + (
    REPO_ROOT / "phases/03-ingest-server-rs/Cargo.toml",
    REPO_ROOT / "package.json",
    REPO_ROOT / "phases/02-robot-upload-agent/src/ui-spa/package.json",
    REPO_ROOT / "phases/05-query-service/src/ui-spa/package.json",
    REPO_ROOT / "phases/ui-kit/package.json",
)


def test_first_party_metadata_does_not_claim_an_unselected_license():
    root_license = any(
        (REPO_ROOT / name).is_file()
        for name in ("LICENSE", "LICENSE.md", "LICENCE", "LICENCE.md")
    )
    if root_license:
        return

    for manifest in FIRST_PARTY_MANIFESTS:
        text = manifest.read_text()
        claims = (
            "License ::" in text
            or any(
                line.lstrip().startswith(
                    ("license =", "license-file =", '"license":')
                )
                for line in text.splitlines()
            )
        )
        assert not claims, (
            f"{manifest.relative_to(REPO_ROOT)} claims a first-party license, "
            "but the repository has no root license"
        )
