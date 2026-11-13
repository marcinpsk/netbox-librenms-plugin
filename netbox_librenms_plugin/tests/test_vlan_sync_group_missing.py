"""Real-DB e2e: a VLAN whose selected group no longer exists must not report as 'unchanged'.

SyncVLANsView._handle_create_vlans fails closed when the group that the user approved for a row is gone
when the writer resolves it: it skips that VID (never creating it in the wrong scope) and emits its own
error. Previously that skip was folded into the shared ``skipped_count`` and surfaced as "N unchanged"
in a *success* message, implying the VID synced fine. This drives the real view end-to-end (real Device,
real cache, real draft, real message framework, real DB) to pin that the group-missing skip is counted
and messaged separately and never claims success.
"""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import submit_with_draft


def _sync(client, device, vid, name, *, choices=()):
    """Select *vid* through the VLAN tab's draft, then submit it; return the recorded messages."""
    seed_source_snapshot(device, Resource.VLANS, "default", vlans=[{"vlan_vlan": vid, "vlan_name": name}])
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans", kwargs={"object_type": "device", "object_id": device.pk}
    )
    response = submit_with_draft(client, device, "vlans", url, [vid], choices=choices, action="create_vlans")
    return [(message.level_tag, message.message) for message in get_messages(response.wsgi_request)]


@pytest.fixture
def device(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("vlm-user"))
    return make_device("vlm-dev", librenms_cf={"default": {"id": 42}})


@pytest.mark.django_db
class TestVlanSyncGroupMissing:
    def test_missing_group_not_reported_as_unchanged(self, client, device, monkeypatch):
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        group = VLANGroup.objects.create(name="vlm-group", slug="vlm-group")
        approved_rows = SyncVLANsView._approved_rows

        def approve_then_delete_the_group(view, *args):
            # Another user deletes the group after the evidence check approved the row.
            approved = approved_rows(view, *args)
            VLANGroup.objects.filter(pk=group.pk).delete()
            return approved

        monkeypatch.setattr(SyncVLANsView, "_approved_rows", approve_then_delete_the_group)
        before = VLAN.objects.count()

        messages = _sync(
            client,
            device,
            10,
            "Management",
            choices=[("data-choice-url", {"row": "10", "field": "group", "value": str(group.pk)})],
        )

        joined = " || ".join(msg for _, msg in messages)
        # The per-VID fail-closed error is shown...
        assert any("no longer exists" in msg for _, msg in messages), joined
        # ...the skip is attributed to the missing group, NOT to "unchanged"...
        assert "VLAN group missing" in joined
        assert "unchanged" not in joined
        # ...and nothing claims a successful sync.
        assert not any(level == "success" for level, _ in messages), joined
        # ...and no VLAN was created in the wrong (global) scope.
        assert VLAN.objects.count() == before

    def test_valid_global_vlan_still_syncs(self, client, device):
        """Positive control: a row with no group selection still creates a real global VLAN and reports success."""
        from ipam.models import VLAN

        messages = _sync(client, device, 20, "Users")

        assert any(level == "success" and "created" in msg for level, msg in messages), messages
        assert VLAN.objects.filter(vid=20, group__isnull=True).exists()
