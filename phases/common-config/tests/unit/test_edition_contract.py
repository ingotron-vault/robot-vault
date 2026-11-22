"""The core/Pro edition contract: core loads Pro only through ``load_pro``.

Ways it can fail, each pinned by a test below:

1. Edition ``pro`` with no Pro package silently runs as core (paid features
   vanish, no error).
2. A missing dependency inside Pro is misread as "Pro not installed", which
   hides the real cause.
3. Another error inside Pro's ``__init__`` or a component is swallowed.
4. A Pro build made for another core version or adapter API is loaded.
5. A Pro build without the version attributes is loaded.
6. Edition ``core`` imports Pro anyway (also when it is installed).
7. A typo in an explicit edition argument is guessed instead of refused.
8. ``CORE_VERSION`` drifts from the repository ``VERSION`` file.
9. ``python -m robot_vault_config.edition`` does not print ``identity()``.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from robot_vault_config import edition
from robot_vault_config.edition import (
    ADAPTER_API,
    CORE_VERSION,
    PRO_PACKAGE,
    identity,
    load_pro,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
GOOD_INIT = f"""
PRO_VERSION = "9.9.9"
BUILT_FOR_CORE = {CORE_VERSION!r}
ADAPTER_API = {ADAPTER_API!r}
CAPABILITIES = ("zeta", "alpha")
"""
POISON_INIT = 'raise AssertionError("core imported the Pro package")\n'


def write_pro(root: Path, init: str, **components: str) -> Path:
    package = root / PRO_PACKAGE
    package.mkdir()
    (package / "__init__.py").write_text(textwrap.dedent(init))
    for name, body in components.items():
        (package / f"{name}.py").write_text(textwrap.dedent(body))
    return root


@pytest.fixture
def fake_pro(tmp_path, monkeypatch):
    """Install a throwaway Pro package ahead of any real one."""

    for name in [m for m in sys.modules if m.split(".")[0] == PRO_PACKAGE]:
        monkeypatch.delitem(sys.modules, name)

    def install(init: str, **components: str) -> None:
        monkeypatch.syspath_prepend(str(write_pro(tmp_path, init, **components)))

    return install


def test_pro_absent_is_a_clear_refusal(monkeypatch):
    monkeypatch.setitem(sys.modules, PRO_PACKAGE, None)
    with pytest.raises(RuntimeError, match="Pro package is not installed"):
        load_pro("query", edition="pro")
    with pytest.raises(RuntimeError, match="Pro package is not installed"):
        identity("pro")


def test_missing_dependency_inside_pro_propagates(fake_pro):
    fake_pro("import robot_vault_missing_dep_for_test\n")
    with pytest.raises(ModuleNotFoundError) as caught:
        load_pro("query", edition="pro")
    assert caught.value.name == "robot_vault_missing_dep_for_test"


def test_other_error_inside_pro_init_propagates(fake_pro):
    fake_pro('raise ValueError("broken Pro build")\n')
    with pytest.raises(ValueError, match="broken Pro build"):
        load_pro("query", edition="pro")


def test_component_import_errors_propagate(fake_pro):
    fake_pro(GOOD_INIT, query="import robot_vault_missing_dep_for_test\n")
    with pytest.raises(ModuleNotFoundError) as caught:
        load_pro("query", edition="pro")
    assert caught.value.name == "robot_vault_missing_dep_for_test"
    with pytest.raises(ModuleNotFoundError):
        load_pro("no_such_component", edition="pro")


def test_adapter_api_mismatch_names_both_sides(fake_pro):
    fake_pro(GOOD_INIT.replace(f"ADAPTER_API = {ADAPTER_API!r}", "ADAPTER_API = 999"))
    with pytest.raises(RuntimeError) as caught:
        load_pro("query", edition="pro")
    assert f"{PRO_PACKAGE}.ADAPTER_API=999" in str(caught.value)
    assert f"core ADAPTER_API={ADAPTER_API!r}" in str(caught.value)


def test_core_version_mismatch_names_both_sides(fake_pro):
    fake_pro(GOOD_INIT.replace(f"BUILT_FOR_CORE = {CORE_VERSION!r}", 'BUILT_FOR_CORE = "0.0.1"'))
    with pytest.raises(RuntimeError) as caught:
        identity("pro")
    assert f"{PRO_PACKAGE}.BUILT_FOR_CORE='0.0.1'" in str(caught.value)
    assert f"core CORE_VERSION={CORE_VERSION!r}" in str(caught.value)


def test_pro_without_contract_attributes_is_refused(fake_pro):
    fake_pro('PRO_VERSION = "9.9.9"\n')
    with pytest.raises(RuntimeError, match="ADAPTER_API=None"):
        load_pro("query", edition="pro")


def test_matching_pro_loads_component_and_reports_identity(fake_pro):
    fake_pro(GOOD_INIT, query="MARK = 'pro query'\n")
    assert load_pro("query", edition="pro").MARK == "pro query"
    assert identity("pro") == {
        "edition": "pro",
        "core_version": CORE_VERSION,
        "adapter_api": ADAPTER_API,
        "pro_version": "9.9.9",
        "capabilities": ["alpha", "zeta"],
    }


def test_core_never_imports_pro_even_when_installed(fake_pro):
    fake_pro(POISON_INIT)
    assert load_pro("query", edition="core") is None
    assert identity("core")["pro_version"] is None
    assert PRO_PACKAGE not in sys.modules


def test_explicit_edition_typo_is_refused():
    with pytest.raises(ValueError, match="is not a known edition"):
        load_pro("query", edition="enterprise")


def test_core_version_equals_version_file():
    assert CORE_VERSION == (REPO_ROOT / "VERSION").read_text().strip()


def run_python(args, pythonpath: Path, edition_value: str):
    env = dict(os.environ, ROBOT_VAULT_EDITION=edition_value)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(pythonpath), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, *args], env=env, capture_output=True, text=True, timeout=60
    )


def test_fresh_core_process_never_imports_pro(tmp_path):
    # A poisoned Pro package first on the path: any import of it fails the run.
    root = write_pro(tmp_path, POISON_INIT)
    script = textwrap.dedent(
        f"""
        import sys
        import robot_vault_config
        from robot_vault_config.edition import identity, load_pro
        assert load_pro("query") is None
        identity()
        loaded = [m for m in sys.modules if m.split(".")[0] == {PRO_PACKAGE!r}]
        assert not loaded, loaded
        """
    )
    result = run_python(["-c", script], root, "core")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("edition_value", "init", "pro_version", "capabilities"),
    [
        ("core", POISON_INIT, None, []),
        ("pro", GOOD_INIT, "9.9.9", ["alpha", "zeta"]),
    ],
)
def test_module_command_prints_identity_json(
    tmp_path, edition_value, init, pro_version, capabilities
):
    root = write_pro(tmp_path, init)
    result = run_python(["-m", edition.__name__], root, edition_value)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "edition": edition_value,
        "core_version": CORE_VERSION,
        "adapter_api": ADAPTER_API,
        "pro_version": pro_version,
        "capabilities": capabilities,
    }


def test_module_command_fails_on_mismatched_pro(tmp_path):
    root = write_pro(tmp_path, GOOD_INIT.replace(f"ADAPTER_API = {ADAPTER_API!r}", "ADAPTER_API = 999"))
    result = run_python(["-m", edition.__name__], root, "pro")
    assert result.returncode != 0
    assert "does not match core ADAPTER_API" in result.stderr
    assert result.stdout == ""
