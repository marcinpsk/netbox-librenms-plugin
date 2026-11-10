"""
Concurrency tests for VLAN synchronization.

Global VIDs are serialized by an advisory lock. The rebuild locks the chosen group and the target
VLAN rows before it reads them, so a change that commits while it waits makes the approval stale.
"""

from functools import partial
from queue import Queue
from threading import Barrier, BrokenBarrierError
from time import monotonic, sleep

import pytest
from dcim.models import Device
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from netbox_librenms_plugin.acquisition.registry import Resource
from netbox_librenms_plugin.confirmation_intent import FIELD
from netbox_librenms_plugin.tests.conftest import bounded_executor, make_device, run_in_threads
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_source_snapshot
from netbox_librenms_plugin.tests.view_test_helpers import (
    assert_locked_before_update,
    make_request,
    make_user_with_perms,
    messages_on,
    post,
)
from netbox_librenms_plugin.tests.vlan_intent_helpers import assessed_tokens


class _CapturedQueries:
    """Expose synthetic SQL through the CaptureQueriesContext contract."""

    def __init__(self, *statements):
        self.captured_queries = [{"sql": statement} for statement in statements]


def test_lock_order_assertion_ignores_sibling_table_names():
    """A lock on ipam_vlangroup must not satisfy the ipam_vlan assertion."""
    captured = _CapturedQueries(
        'SELECT "ipam_vlangroup"."id" FROM "ipam_vlangroup" FOR UPDATE',
        'UPDATE "ipam_vlan" SET "name" = \'new\' WHERE "ipam_vlan"."id" = 1',
    )

    with pytest.raises(AssertionError, match="ipam_vlan was never locked"):
        assert_locked_before_update(captured, "ipam_vlan")


def test_lock_order_assertion_requires_one_exact_update_pair():
    """One early lock must not hide a later unpaired update in the same capture."""
    captured = _CapturedQueries(
        'SELECT "ipam_vlan"."id" FROM "ipam_vlan" FOR UPDATE',
        'UPDATE "ipam_vlan" SET "name" = \'one\' WHERE "ipam_vlan"."id" = 1',
        'UPDATE "ipam_vlan" SET "name" = \'two\' WHERE "ipam_vlan"."id" = 2',
    )

    with pytest.raises(AssertionError, match="exactly one lock/update pair"):
        assert_locked_before_update(captured, "ipam_vlan")


class _GlobalVLANLookupBarrier:
    """Pause both workers after their first global VLAN lookup completes."""

    def __init__(self, barrier):
        self.barrier = barrier
        self.lookup_seen = False

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if (
            not self.lookup_seen
            and sql.lstrip().upper().startswith("SELECT")
            and 'FROM "ipam_vlan"' in sql
            and '"ipam_vlan"."group_id" IS NULL' in sql
            and '"ipam_vlan"."vid"' in sql
        ):
            self.lookup_seen = True
            try:
                self.barrier.wait(timeout=1)
            except BrokenBarrierError:
                # With serialization, the first worker times out while the second
                # waits for its transaction lock. It can then create and commit.
                pass
        return result


def _sync_global_vlan(device, user, token, lookup_wrapper):
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("SET statement_timeout = '5s'")

        request = make_request(
            data={"action": "create_vlans", FIELD: [token], "server_key": "default"},
            user=user,
            path="/sync/vlans/",
        )

        view = SyncVLANsView()
        view.request = request

        with connection.execute_wrapper(lookup_wrapper):
            view.post(request, object_type="device", object_id=device.pk)
    finally:
        connection.close()


@pytest.mark.django_db(
    transaction=True,
    # Explicit available apps make Django use cascade-aware transaction cleanup.
    # This is required when another installed plugin has M2M tables outside the default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)
def test_concurrent_global_vlan_sync_creates_one_vlan():
    """Two requests that sync the same missing global VID must create one row."""
    from ipam.models import VLAN

    user = get_user_model().objects.create_user(
        username="global-vlan-concurrency-user",
        password="test-password",
        is_superuser=True,
    )
    devices = [make_device("global-vlan-device-a"), make_device("global-vlan-device-b")]
    for device in devices:
        seed_source_snapshot(
            device, Resource.VLANS, "default", vlans=[{"vlan_vlan": 321, "vlan_name": "Concurrent global VLAN"}]
        )
    tokens = [assessed_tokens(device, user)[321] for device in devices]
    lookup_barrier = Barrier(2, timeout=10)
    lookup_wrappers = [_GlobalVLANLookupBarrier(lookup_barrier) for _device in devices]

    run_in_threads(
        *(
            partial(_sync_global_vlan, device, user, token, lookup_wrapper)
            for device, token, lookup_wrapper in zip(devices, tokens, lookup_wrappers, strict=True)
        ),
        barriers=(lookup_barrier,),
        timeout=10,
    )

    assert all(wrapper.lookup_seen for wrapper in lookup_wrappers)
    assert VLAN.objects.filter(vid=321, group__isnull=True).count() == 1


def _grouped_vlan_user(name):
    from ipam.models import VLAN, VLANGroup

    return make_user_with_perms(
        name, [("view", Device), ("view", VLAN), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)]
    )


def _drive_grouped_sync(device, user, token, *, confirm=True):
    """POST one token into the real view and return the recorded messages."""
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    data = {"action": "create_vlans", FIELD: [token], "server_key": "default"}
    if confirm:
        data["confirm_conflicts"] = "1"
    request = make_request(data=data, user=user, path="/sync/vlans/")
    post(SyncVLANsView(), request, object_type="device", object_id=device.pk)
    return messages_on(request)


def _seed_grouped_vid(device, vid, librenms_name):
    seed_source_snapshot(device, Resource.VLANS, "default", vlans=[{"vlan_vlan": vid, "vlan_name": librenms_name}])


@pytest.mark.django_db
def test_grouped_vlan_row_is_locked_before_the_rename():
    """Verify SQL locks a grouped VLAN before its confirmed rename."""
    from django.db import transaction
    from django.test.utils import CaptureQueriesContext
    from ipam.models import VLAN, VLANGroup

    device = make_device("vlan-lock-sql")
    group = VLANGroup.objects.create(name="Lock-SQL", slug="lock-sql")
    vlan = VLAN.objects.create(vid=41, group=group, name="old-name", status="active")
    user = _grouped_vlan_user("vlan-lock-sql-user")
    _seed_grouped_vid(device, 41, "librenms-name")
    token = assessed_tokens(device, user)[41]

    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        _drive_grouped_sync(device, user, token)

    assert_locked_before_update(captured, "ipam_vlan")
    vlan.refresh_from_db()
    assert vlan.name == "librenms-name"


@pytest.mark.django_db
def test_grouped_vlan_name_collision_is_rejected_without_a_database_error():
    """A confirmed rename must not violate the group's unique VLAN-name constraint."""
    from ipam.models import VLAN, VLANGroup

    device = make_device("vlan-rename-collision")
    group = VLANGroup.objects.create(name="Rename-Collision", slug="rename-collision")
    vlan = VLAN.objects.create(vid=51, group=group, name="old-name", status="active")
    VLAN.objects.create(vid=52, group=group, name="claimed-name", status="active")
    user = _grouped_vlan_user("vlan-rename-collision-user")
    _seed_grouped_vid(device, 51, "claimed-name")
    token = assessed_tokens(device, user)[51]

    recorded_messages = _drive_grouped_sync(device, user, token)

    vlan.refresh_from_db()
    assert vlan.name == "old-name"
    assert any(level == "error" and "claimed-name" in message for level, message in recorded_messages)


def _wait_until_blocked(pid, future, seconds=5):
    """Block until the worker backend waits on a database lock."""
    deadline = monotonic() + seconds
    last_state = None
    while monotonic() < deadline:
        if future.done():
            raise AssertionError(f"the VLAN sync finished before it waited for a lock: {future.result()}")
        with connection.cursor() as cursor:
            cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
            last_state = cursor.fetchone()
        if last_state and last_state[0] == "Lock":
            return
        sleep(0.01)
    raise AssertionError(f"the VLAN sync never waited for a lock: {last_state!r}")


@pytest.mark.django_db(
    transaction=True,
    # Explicit available apps make Django use cascade-aware transaction cleanup.
    # This is required when another installed plugin has M2M tables outside the default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)
@pytest.mark.parametrize(
    ("change", "path"),
    [
        ("rename", "assumptions.target.name"),
        ("delete", "assumptions.target"),
        ("create", "assumptions.target"),
    ],
)
def test_a_target_change_committed_while_the_writer_waits_for_its_lock_is_stale(change, path):
    """The rebuild reads the target under its lock, so a change that commits while it waits refuses the write."""
    from django.db import transaction
    from ipam.models import VLAN, VLANGroup

    device = make_device(f"vlan-lock-race-{change}")
    group = VLANGroup.objects.create(name=f"Lock-Race-{change}", slug=f"lock-race-{change}")
    vlan = VLAN.objects.create(vid=61, group=group, name="old-name", status="active") if change != "create" else None
    user = _grouped_vlan_user(f"vlan-lock-race-{change}-user")
    _seed_grouped_vid(device, 61, "librenms-name")
    token = assessed_tokens(device, user, choices={61: group.pk})[61]
    backend_pids = Queue()

    def sync_as_caller():
        close_old_connections()
        try:
            connection.ensure_connection()
            backend_pids.put(connection.connection.info.backend_pid)
            return _drive_grouped_sync(device, get_user_model().objects.get(pk=user.pk), token)
        finally:
            close_old_connections()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            # The competitor holds the lock the writer takes for this target: the VLAN row, or the group row for a create.
            if vlan is not None:
                VLAN.objects.select_for_update().filter(pk=vlan.pk).order_by().first()
            else:
                VLANGroup.objects.select_for_update().filter(pk=group.pk).first()
            syncing = executor.submit(sync_as_caller)
            _wait_until_blocked(backend_pids.get(timeout=5), syncing)
            if change == "rename":
                VLAN.objects.filter(pk=vlan.pk).update(name="renamed")
            elif change == "delete":
                VLAN.objects.filter(pk=vlan.pk).delete()
            else:
                VLAN.objects.create(vid=61, group=group, name="competitor", status="active")
        recorded = syncing.result(timeout=20)

    joined = " || ".join(text for _level, text in recorded)
    assert "VLAN 61: " in joined and f"({path}" in joined, joined
    assert not any(level == "success" for level, _text in recorded), joined
    assert (
        list(VLAN.objects.filter(vid=61).values_list("name", flat=True))
        == {
            "rename": ["renamed"],
            "delete": [],
            "create": ["competitor"],
        }[change]
    )
