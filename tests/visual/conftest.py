from __future__ import annotations

import sys

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.platform == "darwin":
        return
    skip = pytest.mark.skip(reason="golden images are rendered with macOS fonts; darwin only")
    for item in items:
        if "tests/visual" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _restore_app_style_after_visual_session():
    yield
    from tests.visual import scenarios

    scenarios.restore_app_style()
