"""
Tests for interface VLAN sync functionality (Phase 2).

Tests cover:
- VlanAssignmentMixin methods
- Port VLAN enrichment
- VLAN sync action
"""

from unittest.mock import MagicMock, patch

# Import the autouse fixture from helpers
pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]


class TestVlanAssignmentMixin:
    """Tests for VlanAssignmentMixin methods."""

    def test_get_vlan_groups_for_device_includes_site_scoped(self, mock_librenms_config):
        """Test that VLAN groups scoped to device's site are included."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        # Create mock device with site
        mock_device = MagicMock()
        mock_device.site = MagicMock()
        mock_device.site.pk = 1
        mock_device.site.region = None
        mock_device.site.group = None
        mock_device.location = None
        mock_device.rack = None

        # Mock the VLAN group query
        mock_site_group = MagicMock()
        mock_site_group.name = "Site VLANs"
        mock_site_group.pk = 10

        with patch.object(mixin, "_get_vlan_groups_for_scope") as mock_get_scope:
            mock_get_scope.return_value = [mock_site_group]
            with patch("ipam.models.VLANGroup") as mock_vlan_group_class:
                mock_vlan_group_class.objects.filter.return_value = []

                mixin.get_vlan_groups_for_device(mock_device)

                # Verify site scope was queried
                assert mock_get_scope.called

    def test_get_vlan_groups_for_device_includes_global(self, mock_librenms_config):
        """Test that global VLAN groups (no scope) are included."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        # Create mock device with no location context
        mock_device = MagicMock()
        mock_device.site = None
        mock_device.location = None
        mock_device.rack = None

        with patch.object(mixin, "_get_vlan_groups_for_scope") as mock_get_scope:
            mock_get_scope.return_value = []
            with patch("ipam.models.VLANGroup") as mock_vlan_group_class:
                mock_global_group = MagicMock()
                mock_global_group.name = "Global VLANs"
                mock_global_group.pk = 20
                mock_vlan_group_class.objects.filter.return_value = [mock_global_group]

                mixin.get_vlan_groups_for_device(mock_device)

                # Verify global scope was queried
                mock_vlan_group_class.objects.filter.assert_called_with(scope_type__isnull=True)

    def test_select_most_specific_group_prefers_rack(self, mock_librenms_config):
        """Test that rack-scoped groups are preferred over site-scoped."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        # Create mock device with rack
        mock_device = MagicMock()
        mock_device.rack = MagicMock()
        mock_device.rack.pk = 1
        mock_device.site = MagicMock()
        mock_device.site.pk = 2
        mock_device.site.region = None
        mock_device.site.group = None
        mock_device.location = None

        # Create mock groups with different scopes
        mock_rack_group = MagicMock()
        mock_rack_group.scope_type = MagicMock()
        mock_rack_group.scope_type.pk = 100  # Rack content type
        mock_rack_group.scope_id = 1

        mock_site_group = MagicMock()
        mock_site_group.scope_type = MagicMock()
        mock_site_group.scope_type.pk = 101  # Site content type
        mock_site_group.scope_id = 2

        with patch("django.contrib.contenttypes.models.ContentType") as mock_ct:
            # Mock ContentType lookups
            mock_ct.objects.get_for_model.side_effect = lambda model: MagicMock(pk=100 if "Rack" in str(model) else 101)

            result = mixin._select_most_specific_group([mock_rack_group, mock_site_group], mock_device)

            # Rack-scoped should be preferred
            assert result == mock_rack_group

    def test_select_most_specific_group_returns_none_for_ambiguous(self, mock_librenms_config):
        """Test that None is returned when multiple groups have same priority."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        # Create mock device
        mock_device = MagicMock()
        mock_device.site = MagicMock()
        mock_device.site.pk = 1
        mock_device.site.region = None
        mock_device.site.group = None
        mock_device.rack = None
        mock_device.location = None

        # Create two groups with same scope (both site-scoped to same site)
        mock_group1 = MagicMock()
        mock_group1.scope_type = MagicMock()
        mock_group1.scope_type.pk = 101
        mock_group1.scope_id = 1

        mock_group2 = MagicMock()
        mock_group2.scope_type = MagicMock()
        mock_group2.scope_type.pk = 101
        mock_group2.scope_id = 1

        with patch("django.contrib.contenttypes.models.ContentType") as mock_ct:
            mock_ct.objects.get_for_model.return_value = MagicMock(pk=101)

            result = mixin._select_most_specific_group([mock_group1, mock_group2], mock_device)

            # Ambiguous - should return None
            assert result is None

    def test_get_ancestors_returns_hierarchy(self, mock_librenms_config):
        """Test that _get_ancestors returns full parent chain."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        # Create mock location hierarchy
        mock_grandparent = MagicMock()
        mock_grandparent.parent = None

        mock_parent = MagicMock()
        mock_parent.parent = mock_grandparent

        mock_location = MagicMock()
        mock_location.parent = mock_parent

        ancestors = mixin._get_ancestors(mock_location)

        assert len(ancestors) == 3
        assert ancestors[0] == mock_location
        assert ancestors[1] == mock_parent
        assert ancestors[2] == mock_grandparent

    def test_find_vlan_in_group_prefers_specified_group(self, mock_librenms_config):
        """Test that _find_vlan_in_group prefers the specified group."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_vlan_in_group = MagicMock()
        mock_vlan_global = MagicMock()

        lookup_maps = {
            "vid_group_to_vlan": {
                (100, 5): mock_vlan_in_group,
                (100, None): mock_vlan_global,
            },
            "vid_to_vlans": {
                100: [mock_vlan_in_group, mock_vlan_global],
            },
        }

        result = mixin._find_vlan_in_group(100, 5, lookup_maps)

        assert result == mock_vlan_in_group

    def test_find_vlan_in_group_falls_back_to_global(self, mock_librenms_config):
        """Test that _find_vlan_in_group falls back to global VLAN."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_vlan_global = MagicMock()

        lookup_maps = {
            "vid_group_to_vlan": {
                (100, None): mock_vlan_global,
            },
            "vid_to_vlans": {
                100: [mock_vlan_global],
            },
        }

        # Request group 5 which doesn't have VLAN 100
        result = mixin._find_vlan_in_group(100, 5, lookup_maps)

        assert result == mock_vlan_global

    def test_find_vlan_in_group_returns_none_if_not_found(self, mock_librenms_config):
        """Test that _find_vlan_in_group returns None if VLAN not found."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        lookup_maps = {
            "vid_group_to_vlan": {},
            "vid_to_vlans": {},
        }

        result = mixin._find_vlan_in_group(999, None, lookup_maps)

        assert result is None


class TestPortVlanEnrichment:
    """Tests for port VLAN data enrichment."""

    pytest_plugins = ["tests.test_librenms_api_helpers"]

    @patch("requests.get")
    def test_parse_port_vlan_data_access_port(self, mock_get, mock_librenms_config):
        """Test parsing access port VLAN data."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "port_id": 1234,
            "ifName": "Gi1/0/1",
            "ifDescr": "GigabitEthernet1/0/1",
            "ifVlan": "100",
            "ifTrunk": None,
        }

        result = api.parse_port_vlan_data(port_data, "ifName")

        assert result["port_id"] == 1234
        assert result["interface_name"] == "Gi1/0/1"
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 100
        assert result["tagged_vlans"] == []

    @patch("requests.get")
    def test_parse_port_vlan_data_trunk_port(self, mock_get, mock_librenms_config):
        """Test parsing trunk port VLAN data."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "port_id": 5678,
            "ifName": "Te1/1/1",
            "ifDescr": "TenGigabitEthernet1/1/1",
            "ifVlan": "90",
            "ifTrunk": "dot1Q",
            "vlans": [
                {"vlan": 90, "untagged": 1, "state": "unknown"},
                {"vlan": 50, "untagged": 0, "state": "forwarding"},
                {"vlan": 60, "untagged": 0, "state": "forwarding"},
            ],
        }

        result = api.parse_port_vlan_data(port_data, "ifName")

        assert result["port_id"] == 5678
        assert result["interface_name"] == "Te1/1/1"
        assert result["mode"] == "tagged"
        assert result["untagged_vlan"] == 90
        assert sorted(result["tagged_vlans"]) == [50, 60]

    @patch("requests.get")
    def test_parse_port_vlan_data_uses_interface_name_field(self, mock_get, mock_librenms_config):
        """Test that parse_port_vlan_data respects interface_name_field parameter."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "port_id": 1234,
            "ifName": "Gi1/0/1",
            "ifDescr": "GigabitEthernet1/0/1",
            "ifVlan": "100",
            "ifTrunk": None,
        }

        result = api.parse_port_vlan_data(port_data, "ifDescr")

        assert result["interface_name"] == "GigabitEthernet1/0/1"


class TestInterfaceVlanSync:
    """Tests for interface VLAN sync action."""

    pytest_plugins = ["tests.test_librenms_api_helpers"]

    def test_update_interface_vlan_assignment_access_mode(self, mock_librenms_config):
        """Test that access mode is set correctly for untagged-only ports."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_interface = MagicMock()
        mock_interface.tagged_vlans = MagicMock()

        mock_vlan = MagicMock()
        mock_vlan.vid = 100

        lookup_maps = {
            "vid_group_to_vlan": {(100, None): mock_vlan},
            "vid_to_vlans": {100: [mock_vlan]},
        }

        vlan_data = {
            "untagged_vlan": 100,
            "tagged_vlans": [],
        }

        mixin._update_interface_vlan_assignment(mock_interface, vlan_data, None, lookup_maps)

        assert mock_interface.mode == "access"
        assert mock_interface.untagged_vlan == mock_vlan
        mock_interface.tagged_vlans.clear.assert_called_once()

    def test_update_interface_vlan_assignment_tagged_mode(self, mock_librenms_config):
        """Test that tagged mode is set for trunk ports."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_interface = MagicMock()
        mock_interface.tagged_vlans = MagicMock()

        mock_vlan_100 = MagicMock()
        mock_vlan_100.vid = 100
        mock_vlan_200 = MagicMock()
        mock_vlan_200.vid = 200
        mock_vlan_300 = MagicMock()
        mock_vlan_300.vid = 300

        lookup_maps = {
            "vid_group_to_vlan": {
                (100, None): mock_vlan_100,
                (200, None): mock_vlan_200,
                (300, None): mock_vlan_300,
            },
            "vid_to_vlans": {
                100: [mock_vlan_100],
                200: [mock_vlan_200],
                300: [mock_vlan_300],
            },
        }

        vlan_data = {
            "untagged_vlan": 100,
            "tagged_vlans": [200, 300],
        }

        mixin._update_interface_vlan_assignment(mock_interface, vlan_data, None, lookup_maps)

        assert mock_interface.mode == "tagged"
        assert mock_interface.untagged_vlan == mock_vlan_100
        mock_interface.tagged_vlans.set.assert_called_once_with([mock_vlan_200, mock_vlan_300])

    def test_update_interface_vlan_assignment_missing_vlans(self, mock_librenms_config):
        """Test that missing VLANs are tracked in result."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_interface = MagicMock()
        mock_interface.tagged_vlans = MagicMock()

        # Empty lookup maps - no VLANs exist in NetBox
        lookup_maps = {
            "vid_group_to_vlan": {},
            "vid_to_vlans": {},
        }

        vlan_data = {
            "untagged_vlan": 100,
            "tagged_vlans": [200, 300],
        }

        result = mixin._update_interface_vlan_assignment(mock_interface, vlan_data, None, lookup_maps)

        assert result["missing_vlans"] == [100, 200, 300]
        assert mock_interface.untagged_vlan is None
        mock_interface.tagged_vlans.set.assert_called_once_with([])

    def test_update_interface_vlan_assignment_respects_group_selection(self, mock_librenms_config):
        """Test that VLAN group selection is respected."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()

        mock_interface = MagicMock()
        mock_interface.tagged_vlans = MagicMock()

        mock_vlan_group1 = MagicMock()
        mock_vlan_group1.vid = 100
        mock_vlan_global = MagicMock()
        mock_vlan_global.vid = 100

        lookup_maps = {
            "vid_group_to_vlan": {
                (100, 5): mock_vlan_group1,
                (100, None): mock_vlan_global,
            },
            "vid_to_vlans": {
                100: [mock_vlan_group1, mock_vlan_global],
            },
        }

        vlan_data = {
            "untagged_vlan": 100,
            "tagged_vlans": [],
        }

        # Request VLAN from group 5
        mixin._update_interface_vlan_assignment(mock_interface, vlan_data, 5, lookup_maps)

        # Should use group-specific VLAN
        assert mock_interface.untagged_vlan == mock_vlan_group1
