"""Module identity writes and a chassis host unlink must complete without a lock cycle."""

import json
from functools import partial
from threading import Event, local
from time import monotonic, sleep

import pytest
from dcim.models import Module
from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client
from django.urls import reverse

from netbox_librenms_plugin import confirmation_intent
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    configure_librenms_servers,
    install_module,
    make_device_with_module_bays,
    make_interface,
    make_module_bay,
    make_superuser,
    make_virtual_chassis,
    stack_inventory,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.link_intent_helpers import lifecycle_url, offered_token, unlink_page
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_mapped_modules
from netbox_librenms_plugin.tests.view_test_helpers import render_module_tab, rendered_module_form
from netbox_librenms_plugin.utils import netbox_relocates_module_subtree


def _blocked_by_or_finished(role, blocker, future, *, pids, statements, edges):
    """Observe a real wait edge, or return False when that request can complete without the blocker."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if future.done():
            future.result()
            return False
        if role in pids:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [pids[role]])
                if pids[blocker] in cursor.fetchone()[0]:
                    edges.append((role, blocker, statements[role]))
                    return True
        sleep(0.01)
    raise AssertionError(f"{role} neither finished nor waited for {blocker}: {statements}")


@pytest.mark.skipif(not netbox_relocates_module_subtree(), reason="NetBox moves a module from 4.7.")
@transactional_db_with_all_apps()
def test_a_serial_update_a_module_move_and_a_chassis_unlink_do_not_deadlock(
    client, settings, librenms_server, monkeypatch, flushed_events
):
    """The update pauses before its serial claim while the move claims that serial and the unlink starts."""
    from netbox_librenms_plugin import module_writes

    configure_librenms_servers(
        settings,
        {"default": {"librenms_url": librenms_server.url, "api_token": "module-mapping-race-token"}},
    )
    lower = make_device_with_module_bays("module-mapping-lower", ["Source"], serial="LOWER-CHASSIS")
    higher = make_device_with_module_bays("module-mapping-higher", ["Serial"], serial="HIGHER-CHASSIS")
    assert lower.pk < higher.pk
    chassis = make_virtual_chassis("module-mapping-chassis", lower, higher)
    # The master holds the chassis mappings, and the unlink acts on it.
    chassis.master = higher
    chassis.save()
    destination = make_module_bay(higher, "Destination")
    updated = install_module(higher, "Serial", "MODULE-MAPPING-CARD", serial="OLD-SERIAL")
    ports = [make_interface(device, "Ethernet1") for device in (lower, higher)]
    for offset, port in enumerate(ports):
        seed_mapping(port, "default", own=7101 + offset)
    rows = stack_inventory(
        [
            {
                "entPhysicalIndex": 1,
                "entPhysicalContainedIn": 0,
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": higher.serial,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalContainedIn": 1,
                "entPhysicalClass": "module",
                "entPhysicalName": "Serial",
                "entPhysicalModelName": updated.module_type.model,
                "entPhysicalSerialNum": "SHARED-SERIAL",
            },
        ],
        [lower, higher],
    )
    seed_mapped_modules(higher, rows, librenms_id=7100)
    user = make_superuser("module-mapping-writer")
    update_url, update_fields = rendered_module_form(render_module_tab(higher, user=user), "update-module-serial", 2)
    # This installed module appears after the update form was rendered. The form is still genuine.
    moving = Module.objects.create(
        device=lower,
        module_bay=lower.modulebays.get(name="Source"),
        module_type=updated.module_type,
        serial="SHARED-SERIAL",
        status="active",
    )
    # Creating the competing module invalidates the chassis cache. Restore the same endpoint facts.
    seed_mapped_modules(higher, rows, librenms_id=7100)
    move_url = reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": higher.pk})
    move_fields = {
        "server_key": "default",
        "conflict_module_id": str(moving.pk),
        "target_bay_id": str(destination.pk),
    }
    client.force_login(user)
    unlink_fields = {
        "server_key": "default",
        confirmation_intent.FIELD: [offered_token(unlink_page(client, higher))],
    }
    flushed_events.clear()

    before_claim, owns_claim = Event(), Event()
    release_update, release_move = Event(), Event()
    thread_state = local()
    pids, statements, sqlstates, edges = {}, {}, [], []
    real_assignment_lock = module_writes.assignment_lock

    def schedule_real_claim(serials, **kwargs):
        role = getattr(thread_state, "role", None)
        serials = list(serials)
        if role == "update" and not before_claim.is_set():
            assert "SHARED-SERIAL" in serials
            before_claim.set()
            assert release_update.wait(8), "the serial update was not released"
        result = real_assignment_lock(serials, **kwargs)
        if role == "move" and not owns_claim.is_set():
            assert "SHARED-SERIAL" in serials
            owns_claim.set()
            assert release_move.wait(8), "the module move was not released"
        return result

    monkeypatch.setattr(module_writes, "assignment_lock", schedule_real_claim)

    def post(role, url, fields):
        close_old_connections()
        thread_state.role = role
        try:
            request_client = Client()
            request_client.force_login(get_user_model().objects.get(pk=user.pk))
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]

            def record_real_statement(execute, sql, params, many, context):
                statements[role] = (sql, params)
                try:
                    return execute(sql, params, many, context)
                except DatabaseError as exc:
                    sqlstates.append((role, getattr(exc.__cause__, "sqlstate", None), sql, params))
                    raise

            with connection.execute_wrapper(record_real_statement):
                response = request_client.post(url, fields)
            return response.status_code, [str(message) for message in response.wsgi_request._messages]
        finally:
            connection.close()

    observe = partial(_blocked_by_or_finished, pids=pids, statements=statements, edges=edges)

    with bounded_executor(3) as executor:
        try:
            update = executor.submit(post, "update", update_url, update_fields)
            assert before_claim.wait(5), (
                "the real update did not reach its serial claim",
                update.result() if update.done() else statements,
            )
            move = executor.submit(post, "move", move_url, move_fields)
            assert owns_claim.wait(5), "the real move did not acquire the shared serial claim"
            unlink = executor.submit(post, "unlink", lifecycle_url(higher), unlink_fields)
            unlink_waits = observe("unlink", "update", unlink)
            release_move.set()
            if unlink_waits:
                # The unlink owns the lower Device row and waits for the update's higher row.
                assert observe("move", "unlink", move), edges
            release_update.set()
            if unlink_waits:
                observe("update", "move", update)
            outcomes = {
                "update": update.result(timeout=15),
                "move": move.result(timeout=15),
                "unlink": unlink.result(timeout=15),
            }
        finally:
            release_move.set()
            release_update.set()

    updated.refresh_from_db()
    moving.refresh_from_db()
    higher.refresh_from_db()
    for port in ports:
        port.refresh_from_db()
    module_type = updated.module_type
    module_type.refresh_from_db()
    assert Module.objects.filter(module_type=module_type).count() == module_type.module_count == 2
    assert updated.serial in {"OLD-SERIAL", "SHARED-SERIAL"}
    assert moving.serial == "SHARED-SERIAL"
    assert moving.device_id in {lower.pk, higher.pk}
    assert moving.module_bay_id == (
        destination.pk if moving.device_id == higher.pk else lower.modulebays.get(name="Source").pk
    )
    assert not (updated.serial == "SHARED-SERIAL" and moving.device_id == higher.pk)
    unlinked = read_mapping(higher).own_id("default") is None
    assert [read_mapping(port).own_id("default") for port in ports] == ([None, None] if unlinked else [7101, 7102])
    changed = set(flushed_events)
    assert (("module", updated.pk) in changed) == (updated.serial != "OLD-SERIAL")
    assert (("module", moving.pk) in changed) == (moving.device_id != lower.pk)
    assert (("device", higher.pk) in changed) == unlinked
    assert all((("interface", port.pk) in changed) == unlinked for port in ports)
    assert all(status == 302 for status, _messages in outcomes.values()), outcomes
    diagnostic = {
        "backends": pids,
        "sqlstates": [(role, state, sql.split(" WHERE ")[-1], params) for role, state, sql, params in sqlstates],
        "wait_edges": [(waiter, holder, sql.split(" WHERE ")[-1], params) for waiter, holder, (sql, params) in edges],
        "responses": outcomes,
        "events": flushed_events,
        "modules": [(updated.pk, updated.device_id, updated.serial), (moving.pk, moving.device_id, moving.serial)],
        "module_type_count": module_type.module_count,
        "unlinked": unlinked,
    }
    assert not sqlstates, json.dumps(diagnostic, indent=2)
