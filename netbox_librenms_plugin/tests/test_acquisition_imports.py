"""
Source acquisition and assessment run without a request, session, user or shared cache; their own imports show it.

The check reads the direct imports of acquisition/ and assessment/ only. A module they import, such as
librenms_api, can still import django.core.cache for its own reads.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
CHECKED = sorted([*(PACKAGE / "acquisition").glob("*.py"), *(PACKAGE / "assessment").glob("*.py")])
# The web adapter owns these: a background run has no request, no messages and no shared snapshot cache.
FORBIDDEN = (
    "django.core.cache",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.http",
    "django.shortcuts",
    "netbox_librenms_plugin.source_cache",
    "netbox_librenms_plugin.sync_cache",
    "netbox_librenms_plugin.views",
)


def _imported_modules(source):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def _forbidden(source):
    return sorted(
        {
            name
            for name in _imported_modules(source)
            if any(name == bad or name.startswith(f"{bad}.") for bad in FORBIDDEN)
        }
    )


@pytest.mark.parametrize("path", CHECKED, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_acquisition_and_assessment_import_no_web_module(path):
    assert _forbidden(path.read_text()) == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("from django.core.cache import cache", ["django.core.cache", "django.core.cache.cache"]),
        ("from django.contrib import messages", ["django.contrib.messages"]),
        ("from netbox_librenms_plugin import source_cache", ["netbox_librenms_plugin.source_cache"]),
        ("import netbox_librenms_plugin.views.mixins", ["netbox_librenms_plugin.views.mixins"]),
        ("from django.utils import timezone", []),
        ("from netbox_librenms_plugin.server_mappings import read_mapping", []),
    ],
)
def test_the_check_finds_a_web_import(source, found):
    assert _forbidden(source) == found
