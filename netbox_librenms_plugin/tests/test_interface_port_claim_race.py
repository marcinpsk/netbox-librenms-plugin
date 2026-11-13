"""Two writers on one device that bind the same LibreNMS port at the same time leave one holder."""

from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_mapped_modules

import time
from threading import Event, local

from netbox_librenms_plugin.tests.source_snapshot_helpers import transceiver_row

import pytest
from django.apps import apps

from netbox_librenms_plugin.server_mappings import MappingRole, identity_q
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    configure_default_librenms_server,
    make_device,
    make_superuser,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    post_interface_sync,
    seed_ports,
    sync_port,
)

PORT = 9301
LIBRENMS_DEVICE_ID = 71
ENT_INDEX = 4077
SHORT_NAME, LONG_NAME = "Gi0/1", "GigabitEthernet0/1"
PAUSE_TIMEOUT_SECONDS = 10
# The paused writer outwaits a full observation window, so a failed observation reports itself.
RELEASE_TIMEOUT_SECONDS = 2 * PAUSE_TIMEOUT_SECONDS
# A blocked query fails after this time instead of hanging the suite; it is longer than the pause.
DB_TIMEOUT = "30s"
_INTERFACE_UPDATE = 'UPDATE "dcim_interface"'
# The row matches the installed module, so the table renders its Update Interface form.
_INVENTORY_ITEM = {
    "entPhysicalIndex": ENT_INDEX,
    "entPhysicalClass": "module",
    "entPhysicalName": "port-claim-bay",
    "entPhysicalModelName": "port-claim-mt",
}
_SNAPSHOT_FIELDS = ("name", "mtu", "description", "enabled", "custom_field_data", "last_updated", "module_id")
SYNCED = "Selected interfaces synced successfully."
TABS_CLEARED = "Other sync tabs were cleared because you synchronized data from LibreNMS."
# Per thread: the event that the tab cleanup of this writer waits for; the race sets it for each writer.
_writer = local()

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


@pytest.fixture(autouse=True)
def restore_librenms_id_custom_field():
    """Recreate migration-seeded custom-field state after each TransactionTestCase flush."""
    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    executed_aliases = getattr(_ensure_librenms_id_custom_field, "_executed_aliases", set())
    executed_aliases.discard("default")
    _ensure_librenms_id_custom_field(sender=None, using="default")


@pytest.fixture(autouse=True)
def first_writer_clears_the_tabs_after_the_second_answers(monkeypatch):
    """Hold the first writer's post-commit tab cleanup, so the second writer reads the same snapshots in every run."""
    real_apply = SyncCacheConsistency._apply_mutation

    def apply_after_the_second_answer(self, *args, **kwargs):
        if (second_answered := getattr(_writer, "cleanup_waits_for", None)) is not None:
            assert second_answered.wait(timeout=RELEASE_TIMEOUT_SECONDS), "the second writer did not answer"
        return real_apply(self, *args, **kwargs)

    monkeypatch.setattr(SyncCacheConsistency, "_apply_mutation", apply_after_the_second_answer)


def _seed():
    """Return a device with a module interface ``Gi0/1`` and a plain ``GigabitEthernet0/1``, both unbound."""
    from dcim.models import Interface, Module, ModuleBay, ModuleType

    device = seed_mapping(make_device("port-claim"), SERVER_KEY, own=LIBRENMS_DEVICE_ID)
    module_type = ModuleType.objects.create(manufacturer=device.device_type.manufacturer, model="port-claim-mt")
    bay = ModuleBay.objects.create(device=device, name="port-claim-bay")
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type)
    short = Interface.objects.create(device=device, name=SHORT_NAME, type="1000base-t", module=module)
    long = Interface.objects.create(device=device, name=LONG_NAME, type="1000base-t")
    seed_ports(device, [sync_port(PORT, SHORT_NAME) | {"ifDescr": LONG_NAME}])
    _seed_inventory(device)
    return device, module, short, long


def _seed_inventory(device):
    """Cache the module inventory row that names ``PORT`` by ifName; the helper saves the device row."""

    seed_mapped_modules(
        device,
        [_INVENTORY_ITEM],
        server_key=SERVER_KEY,
        librenms_id=LIBRENMS_DEVICE_ID,
        ports=[{"port_id": PORT, "ifName": SHORT_NAME}],
        transceivers=[transceiver_row(ENT_INDEX, port_id=PORT, model="port-claim-mt")],
    )


def _port_holders():
    """Return the pk of every Interface and VMInterface that holds ``PORT`` on the server."""
    from dcim.models import Interface
    from virtualization.models import VMInterface

    roles = (MappingRole.OWN, MappingRole.OOB)
    holders = (
        model.objects.filter(identity_q(model, server=SERVER_KEY, identities=(PORT,), roles=roles)).values_list(
            "pk", flat=True
        )
        for model in (Interface, VMInterface)
    )
    return sorted(pk for pks in holders for pk in pks)


def _persisted(interface):
    """Return the persisted state of *interface* that a refused writer must not change."""
    from core.models import ObjectChange
    from dcim.models import Interface

    row = Interface.objects.values(*_SNAPSHOT_FIELDS).get(pk=interface.pk)
    changes = ObjectChange.objects.filter(changed_object_type__model="interface", changed_object_id=interface.pk)
    return row, changes.count()


def _interface_sync(user_pk, device, name_field):
    """Post the interface sync of *device* with *name_field* naming the port."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import messages_on

    client = Client()
    client.force_login(get_user_model().objects.get(pk=user_pk))
    response = post_interface_sync(client, device, [PORT], htmx=False, interface_name_field=name_field)
    return messages_on(response.wsgi_request)


def _rendered_bind(device):
    """Return the Update Interface form that the cached Modules tab renders for the inventory row."""
    from netbox_librenms_plugin.tests.view_test_helpers import render_module_tab, rendered_module_form

    return rendered_module_form(render_module_tab(device, server_key=SERVER_KEY), "update-module-interface", ENT_INDEX)


def _module_bind(user_pk, form):
    """Post the rendered module "Update Interface" form, through the middleware."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import messages_on

    action, fields = form
    client = Client()
    client.force_login(get_user_model().objects.get(pk=user_pk))
    response = client.post(action, data=fields)
    return messages_on(response.wsgi_request)


def _on_own_connection(writer, *args, pids, role, paused=None, resume=None, answered):
    """Run *writer* on a bounded connection of this thread; pause at the first interface write if *paused* is set."""
    from django.db import close_old_connections, connection

    first_write = True
    _writer.cleanup_waits_for = answered if role == "first" else None

    def pause_at_first_interface_write(execute, sql, params, many, context):
        nonlocal first_write
        if paused is not None and first_write and sql.startswith(_INTERFACE_UPDATE):
            first_write = False
            paused.set()
            assert resume.wait(timeout=RELEASE_TIMEOUT_SECONDS), "the race did not release the first writer"
        return execute(sql, params, many, context)

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = '{DB_TIMEOUT}'")
            cursor.execute(f"SET statement_timeout = '{DB_TIMEOUT}'")
            cursor.execute("SELECT pg_backend_pid()")
            pids[role] = cursor.fetchone()[0]
        with connection.execute_wrapper(pause_at_first_interface_write):
            return writer(*args)
    finally:
        connection.close()
        if role == "second":
            answered.set()


def _race(first, second, while_first_paused):
    """Pause *first* at its first interface write, run *second*, observe, then release *first*."""
    first_paused, resume, second_answered, pids = Event(), Event(), Event(), {}
    with bounded_executor(max_workers=2) as executor:
        try:
            first_future = executor.submit(
                _on_own_connection,
                *first,
                pids=pids,
                role="first",
                paused=first_paused,
                resume=resume,
                answered=second_answered,
            )
            assert first_paused.wait(timeout=PAUSE_TIMEOUT_SECONDS), "the first writer never reached its write"
            second_future = executor.submit(
                _on_own_connection, *second, pids=pids, role="second", answered=second_answered
            )
            observed = while_first_paused(second_future, pids)
            resume.set()
            first_result = first_future.result(timeout=PAUSE_TIMEOUT_SECONDS)
            second_result = second_future.result(timeout=PAUSE_TIMEOUT_SECONDS)
        finally:
            resume.set()
            second_answered.set()
    return first_result, second_result, observed


def _second_waits_on_first(_second_future, pids):
    """Return True when PostgreSQL reports that the second writer's backend waits on the first writer's backend."""
    from django.db import connection

    deadline = time.monotonic() + PAUSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if "second" in pids:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                if pids["first"] in cursor.fetchone()[0]:
                    return True
        time.sleep(0.05)
    return False


def test_two_interface_syncs_of_one_device_wait_on_the_device_row_lock(settings):
    """The ifName sync holds the device row; the ifDescr sync waits for it and then finds the holder."""
    configure_default_librenms_server(settings)
    device, _module, short, long = _seed()
    user_pk = make_superuser("port-claim-user").pk
    before = _persisted(long)

    _first, second_messages, second_waited = _race(
        (_interface_sync, user_pk, device, "ifName"),
        (_interface_sync, user_pk, device, "ifDescr"),
        _second_waits_on_first,
    )

    assert second_waited, "the second sync did not wait on the first sync's backend"
    assert _port_holders() == [short.pk]
    assert second_messages == [
        (
            "warning",
            f"Interface '{SHORT_NAME}' kept its current name because the reported name is in use "
            f"on the same interface owner: '{LONG_NAME}'.",
        ),
        ("success", SYNCED),
    ]
    assert _persisted(long) == before


@pytest.mark.parametrize("first_writer", ["interface_sync", "module_bind"])
def test_module_bind_and_interface_sync_of_one_device_wait_on_the_device_row_lock(settings, first_writer):
    """The module action binds Gi0/1 by ifName while the ifDescr sync binds GigabitEthernet0/1."""
    # The module action locks the device row FOR KEY SHARE and the sync FOR UPDATE, so the second writer waits.
    configure_default_librenms_server(settings)
    device, module, short, long = _seed()
    user_pk = make_superuser("port-claim-user").pk
    sync = (_interface_sync, user_pk, device, "ifDescr")
    bind = (_module_bind, user_pk, _rendered_bind(device))
    first, second = (sync, bind) if first_writer == "interface_sync" else (bind, sync)
    winner, loser = (long, short) if first_writer == "interface_sync" else (short, long)
    before = _persisted(loser)
    _winner_row, winner_changes = _persisted(winner)

    _first, second_messages, second_waited = _race(first, second, _second_waits_on_first)

    assert second_waited, "the second writer did not wait on the first writer's backend"
    assert _port_holders() == [winner.pk]
    assert _persisted(loser) == before
    # A change record for the winner shows that both writers run with change logging on.
    assert _persisted(winner)[1] > winner_changes
    if first_writer == "interface_sync":
        # The bind finds the port on the interface that the sync bound, and moves it into the module.
        assert second_messages == [
            ("success", f"Updated interface {LONG_NAME} for port-claim-mt in port-claim-bay."),
            ("info", TABS_CLEARED),
        ]
        long.refresh_from_db()
        assert long.module_id == module.pk
    else:
        assert second_messages == [
            (
                "warning",
                f"Interface '{SHORT_NAME}' kept its current name because the reported name is in use "
                f"on the same interface owner: '{LONG_NAME}'.",
            ),
            ("success", SYNCED),
            ("info", TABS_CLEARED),
        ]
