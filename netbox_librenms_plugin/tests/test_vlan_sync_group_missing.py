"""Real-DB e2e: a VLAN whose chosen group no longer exists must not report as 'unchanged'.

The token of a row names the group the user chose. When that group is gone at apply time, the
rebuild refuses the row as stale (``target.group``): it never creates the VLAN in the wrong scope,
and the skip is never folded into "N unchanged" in a success message. This drives the real view
end-to-end (real Device, real cache, real message framework, real DB).
"""

import pytest

from netbox_librenms_plugin import source_cache
from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.confirmation_intent import FIELD
from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import make_request, messages_on, post
from netbox_librenms_plugin.tests.vlan_intent_helpers import assessed_tokens


def _superuser(name):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username=name, password="x", is_superuser=True)


def _drive_sync(device, user, token):
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    request = make_request(
        data={"action": "create_vlans", FIELD: [token], "server_key": "default"}, user=user, path="/sync/vlans/"
    )
    try:
        post(SyncVLANsView(), request, object_type="device", object_id=device.pk)
    finally:
        source_cache.delete(device, Resource.VLANS, "default")
    return messages_on(request)


@pytest.mark.django_db
class TestVlanSyncGroupMissing:
    def test_missing_group_not_reported_as_unchanged(self):
        from ipam.models import VLAN, VLANGroup

        device = make_device("vlm-dev-1")
        user = _superuser("vlm-user")
        group = VLANGroup.objects.create(name="VLM group", slug="vlm-group")
        seed_source_snapshot(device, Resource.VLANS, "default", vlans=[{"vlan_vlan": 10, "vlan_name": "Management"}])
        token = assessed_tokens(device, user, choices={10: group.pk})[10]
        group.delete()
        before = VLAN.objects.count()

        messages = _drive_sync(device, user, token)

        joined = " || ".join(msg for _, msg in messages)
        # The per-VID refusal names the missing group...
        assert any(msg.startswith("VLAN 10: ") and "target.group" in msg for _, msg in messages), joined
        # ...the skip is NOT reported as "unchanged"...
        assert "unchanged" not in joined
        # ...and nothing claims a successful sync.
        assert not any(level == "success" for level, _ in messages), joined
        # ...and no VLAN was created in the wrong (global) scope.
        assert VLAN.objects.count() == before

    def test_valid_global_vlan_still_syncs(self):
        """Positive control: a row with no group choice still creates a real global VLAN and reports success."""
        from ipam.models import VLAN

        device = make_device("vlm-dev-2")
        user = _superuser("vlm-user2")
        seed_source_snapshot(device, Resource.VLANS, "default", vlans=[{"vlan_vlan": 20, "vlan_name": "Users"}])

        messages = _drive_sync(device, user, assessed_tokens(device, user)[20])

        assert any(level == "success" and "created" in msg for level, msg in messages), messages
        assert VLAN.objects.filter(vid=20, group__isnull=True).exists()
