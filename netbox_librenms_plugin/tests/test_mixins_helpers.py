"""
Real tests for the shared mixin helpers consolidated during the develop-hardening pass.

- ``LibreNMSAPIMixin.resolve_requested_server_key`` centralises the "configured-string-key-or
  -fallback" rule the device verify/render views each open-coded (B12).

These exercise the real functions against the real Django cache / real plugin config — no mocks.
"""

from copy import deepcopy

import pytest


def _configured_servers(*keys):
    from django.conf import settings
    from django.test import override_settings

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {"librenms_url": f"https://{key}.example.test", "api_token": "token"} for key in keys
    }
    return override_settings(PLUGINS_CONFIG=plugin_config)


@pytest.mark.django_db
class TestResolveRequestedServerKey:
    """
    resolve_requested_server_key honours only a configured string key, else degrades to _render_server_key.

    The configured-server set (LibreNMSAPI.get_available_servers) is the external plugin-config
    boundary; it's pinned per test so the assertion is deterministic (a session-wide autouse fixture
    in the API-test helpers otherwise leaves get_plugin_config mocked to a default-only config). The
    helper's own logic — isinstance guard, membership check, degrading fallback — runs for real.
    """

    def _view(self):
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        view.request = RequestFactory().get("/")
        return view

    def test_configured_string_key_is_honoured(self):
        view = self._view()
        with _configured_servers("prod", "default"):
            assert view.resolve_requested_server_key({"server_key": "prod"}) == "prod"

    def test_unconfigured_key_falls_back_to_render_resolver(self):
        view = self._view()
        with _configured_servers("default"):
            # "prod" is not among the configured servers → degrade, don't address its namespace.
            assert view.resolve_requested_server_key({"server_key": "prod"}) == view._render_server_key()

    def test_non_string_key_falls_back_without_crashing(self):
        view = self._view()
        with _configured_servers("prod"):
            # A JSON list is unhashable; the membership check must be skipped, not TypeError.
            assert view.resolve_requested_server_key({"server_key": ["forged"]}) == view._render_server_key()

    def test_missing_key_falls_back(self):
        view = self._view()
        assert view.resolve_requested_server_key({}) == view._render_server_key()


class TestResolvePostedServerKey:
    """
    resolve_posted_server_key honours only a configured key; a blank key falls back to the ACTIVE server.

    A non-blank key that names no configured server, or a repeated key, resolves to None, so the
    action fails closed instead of running against another server.
    """

    def _view(self, active="active-server"):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        view._librenms_api = LibreNMSAPI(server_key=active)
        return view

    def test_configured_key_is_honoured(self):
        with _configured_servers("active-server", "prod", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({"server_key": "prod"}) == "prod"

    def test_forged_nonblank_key_resolves_to_none(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({"server_key": "evil"}) is None

    def test_repeated_key_resolves_to_none(self):
        from django.http import QueryDict

        with _configured_servers("active-server", "prod", "default"):
            view = self._view()
            assert view.resolve_posted_server_key(QueryDict("server_key=evil&server_key=prod")) is None

    def test_blank_key_falls_back_to_active(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({"server_key": "   "}) == "active-server"

    def test_missing_key_falls_back_to_active(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({}) == "active-server"


class TestGetLiveDeviceInfo:
    """get_live_device_info centralizes the write-path rule: always fetch uncached (use_cache=False)."""

    def test_reads_the_device_from_librenms_past_the_render_cache(self, live_librenms):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        live_librenms.server.device_info_response(device_id=42, hostname="sw1-before")
        assert live_librenms.api.get_device_info(42)[1]["hostname"] == "sw1-before"
        live_librenms.server.device_info_response(device_id=42, hostname="sw1-now")
        view = DeviceInterfaceTableView()
        view._librenms_api = live_librenms.api

        success, device_info = view.get_live_device_info(42)

        # A write path reads LibreNMS again instead of the cached render answer.
        assert success is True
        assert device_info["hostname"] == "sw1-now"
        assert [request["path"] for request in live_librenms.server.requests] == ["/api/v0/devices/42"] * 2
