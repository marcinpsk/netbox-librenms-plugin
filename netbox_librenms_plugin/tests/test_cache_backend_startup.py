"""The plugin refuses to start on a cache backend that cannot carry the sync-tab snapshots."""

from copy import deepcopy

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings


def _plugin_config():
    return apps.get_app_config("netbox_librenms_plugin")


def _redis_caches(settings, **options):
    caches = deepcopy(settings.CACHES)
    caches["default"].setdefault("OPTIONS", {}).update(options)
    return caches


def test_the_configured_redis_cache_is_accepted():
    _plugin_config()._validate_cache_backend()


@pytest.mark.parametrize(
    "backend",
    ["django.core.cache.backends.locmem.LocMemCache", "django.core.cache.backends.dummy.DummyCache"],
)
def test_a_cache_without_ttl_and_pattern_deletes_is_refused(backend):
    with override_settings(CACHES={"default": {"BACKEND": backend}}):
        with pytest.raises(ImproperlyConfigured, match="django-redis"):
            _plugin_config()._validate_cache_backend()


def test_a_redis_cache_that_ignores_its_errors_is_refused(settings):
    # A swallowed Redis error reads as a cache miss, so a failed snapshot write would look like no snapshot.
    with override_settings(CACHES=_redis_caches(settings, IGNORE_EXCEPTIONS=True)):
        with pytest.raises(ImproperlyConfigured, match="IGNORE_EXCEPTIONS"):
            _plugin_config()._validate_cache_backend()


def test_the_global_ignore_exceptions_switch_is_refused():
    with override_settings(DJANGO_REDIS_IGNORE_EXCEPTIONS=True):
        with pytest.raises(ImproperlyConfigured, match="IGNORE_EXCEPTIONS"):
            _plugin_config()._validate_cache_backend()
