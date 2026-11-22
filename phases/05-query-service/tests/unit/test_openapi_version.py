import importlib
import sys

import pytest

from query_service import settings


def _import_main_without_built_spa():
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]

    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    original = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original


main = _import_main_without_built_spa()


def test_release_version_is_strict_semver_and_used_by_openapi():
    assert settings.resolve_service_version(
        " 2.4.1 ", component_version="0.1.0"
    ) == "2.4.1"
    assert main.app.openapi()["info"]["version"] == settings.SERVICE_VERSION
    assert main.app.version == settings.SERVICE_VERSION


@pytest.mark.parametrize(
    "invalid",
    ["", "latest", "v1.2.3", "1.2", "1.2.3-rc1", "01.2.3"],
)
def test_invalid_release_version_is_rejected(invalid):
    with pytest.raises(ValueError, match="ROBOT_VAULT_VERSION"):
        settings.resolve_service_version(invalid, component_version="0.1.0")


def test_development_version_names_the_component_scope():
    assert settings.resolve_service_version(
        None, component_version="0.1.0"
    ) == "0.1.0+query-service.dev"
