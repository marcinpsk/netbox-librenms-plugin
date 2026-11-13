"""Behavior tests for VLAN parsing, presentation, and the real sync template."""

from copy import deepcopy

import pytest

from netbox_librenms_plugin.assessment.interfaces import parse_port_vlan_data


def _api(settings):
    """Build a real client from a local-only configuration."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {
            "librenms_url": "http://127.0.0.1:9",
            "api_token": "test-token",
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config
    return LibreNMSAPI(server_key="default")


class TestVLANModeDetection:
    """Exercise the production parser with representative LibreNMS rows."""

    @pytest.mark.parametrize(
        ("port", "expected"),
        [
            (
                {"port_id": 1, "ifName": "Gi1/0/1", "ifVlan": "50", "ifTrunk": None},
                {"mode": "access", "untagged_vlan": 50, "tagged_vlans": []},
            ),
            (
                {
                    "port_id": 2,
                    "ifName": "Te1/1/1",
                    "ifVlan": "90",
                    "ifTrunk": "dot1Q",
                    "vlans": [
                        {"vlan": 90, "untagged": 1},
                        {"vlan": 50, "untagged": 0},
                        {"vlan": 60, "untagged": 0},
                    ],
                },
                {"mode": "tagged", "untagged_vlan": 90, "tagged_vlans": [50, 60]},
            ),
            (
                {"port_id": 3, "ifName": "Gi1/0/48", "ifVlan": "", "ifTrunk": None},
                {"mode": None, "untagged_vlan": None, "tagged_vlans": []},
            ),
        ],
    )
    def test_parse_port_vlan_data(self, port, expected):
        result = parse_port_vlan_data(port)

        assert result["mode"] == expected["mode"]
        assert result["untagged_vlan"] == expected["untagged_vlan"]
        assert result["tagged_vlans"] == expected["tagged_vlans"]


class TestGetVlanSyncCssClass:
    @pytest.mark.parametrize(
        ("exists", "name_matches", "expected"),
        [
            (False, True, "text-danger"),
            (False, False, "text-danger"),
            (True, True, "text-success"),
            (True, False, "text-warning"),
        ],
    )
    def test_status_class(self, exists, name_matches, expected):
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=exists, name_matches=name_matches) == expected


@pytest.mark.django_db
class TestVLANErrorContextServerKey:
    """The error fragment keeps an explicit server scope."""

    @staticmethod
    def _view(settings):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        view = DeviceVLANTableView()
        view._librenms_api = _api(settings)
        return view

    def test_explicit_none_is_preserved(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device

        context = self._view(settings)._get_error_context(make_device("vlan-error-none"), "error", server_key=None)

        assert context["server_key"] is None

    def test_omitted_key_uses_active_server(self, settings):
        from netbox_librenms_plugin.tests.conftest import make_device

        context = self._view(settings)._get_error_context(make_device("vlan-error-default"), "error")

        assert context["server_key"] == "default"


@pytest.mark.django_db
class TestVlanSyncContentTemplateMigratedMode:
    """Render the real VLAN sync fragment in live and migrated modes."""

    @staticmethod
    def _render(*, migrated, server_key="default"):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.sync_filters import parse_sync_filters
        from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device(f"vlan-template-{server_key}-{bool(migrated)}")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        table = LibreNMSVLANTable([{"vlan_id": 10, "name": "v10", "type": "tagged", "state": "active"}])
        RequestConfig(request).configure(table)
        filters = parse_sync_filters(request.GET, table.filter_spec())
        return render_to_string(
            "netbox_librenms_plugin/_vlan_sync_content.html",
            {
                "vlan_sync": {
                    "object": device,
                    "vlan_table": table,
                    "filters": filters,
                    "server_key": server_key,
                    "error_message": None,
                    "cache_expiry": None,
                },
                "migrated_to_marker": migrated,
            },
            request=request,
        )

    def test_migrated_mode_removes_sync_form(self):
        html = self._render(migrated={"server_key": "prod", "device_id": 1, "at": "now"}, server_key="prod")

        assert '<form method="post"' not in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html
        assert 'value="prod"' in html
        assert 'name="action"' not in html

    def test_live_mode_renders_sync_form(self):
        html = self._render(migrated=False)

        assert '<form method="post"' in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="action"' in html
        assert 'name="server_key"' in html


@pytest.mark.django_db
@pytest.mark.parametrize("tab", ["interfaces", "vlans"])
@pytest.mark.parametrize("group_id", ["True", "False", "1.5", "0", "-1", str(10**40), "[]", "{}", "not-an-id"])
def test_a_vlan_group_choice_rejects_malformed_group_ids(client, settings, tab, group_id):
    """A malformed group value must not become a different group or an implicit Global choice."""
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import sync_port
    from netbox_librenms_plugin.tests.source_snapshot_helpers import (
        port_vlans,
        seed_interfaces_snapshot,
        seed_source_snapshot,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import draft_command, draft_tab

    configure_default_librenms_server(settings)
    client.force_login(make_superuser())
    device = make_device("invalid-vlan-group", librenms_cf={"default": {"id": 42}})
    if tab == "interfaces":
        seed_interfaces_snapshot(device, [sync_port(41, "eth0", **port_vlans("access", 100))], server_key="default")
        query = {"interface_name_field": "ifName"}
        command = ("data-vlan-groups-url", {"row": "41", "group_100": group_id})
    else:
        seed_source_snapshot(device, Resource.VLANS, "default", vlans=[{"vlan_vlan": 100, "vlan_name": "v100"}])
        query = {}
        command = ("data-choice-url", {"row": "100", "field": "group", "value": group_id})
    shown = draft_tab(client, device, tab, **query)

    response = draft_command(client, shown, command[0], **command[1])

    assert response.status_code == 400
    assert draft_tab(client, device, tab, **query).table["data-draft-revision"] == shown.table["data-draft-revision"]
