"""A sync write must leave each row it changes in the user's change scope, as NetBox's own edit views require."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.contrib import messages

from netbox_librenms_plugin.tests.cache_test_helpers import seed_module_rows
from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_interface,
    make_module_type,
    make_virtual_chassis,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import mapping_summary, seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    render_module_tab,
    rendered_module_form,
)
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post

# The fixtures of the cable and server-mapping tests below.
from netbox_librenms_plugin.tests.test_cable_breakout import breakout_server  # noqa: F401
from netbox_librenms_plugin.tests.test_object_server_preference import servers  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def flushed_events(monkeypatch):
    """Record the (model, pk) of each event that NetBox flushes at the end of a request, and flush it."""
    from netbox import context_managers

    flushed = set()
    flush = context_managers.flush_events

    def record(events):
        flushed.update((event["object_type"].model, event["object_id"]) for event in events)
        return flush(events)

    monkeypatch.setattr(context_managers, "flush_events", record)
    return flushed


ENT_INDEX = 11


@contextmanager
def _netbox_request(request):
    """Run the block as NetBox runs *request*, and yield the (model, pk) of each event that stays queued."""
    from netbox.context import current_request, events_queue

    request.id = uuid4()
    request_token, queue_token = current_request.set(request), events_queue.set({})
    queued = set()
    try:
        yield queued
        queued.update((event["object_type"].model, event["object_id"]) for event in events_queue.get().values())
    finally:
        current_request.reset(request_token)
        events_queue.reset(queue_token)


def _post(view_class, device, data, user, live_librenms):
    """POST *data* into *view_class* as *user* in a NetBox request; return the request and the queued events."""
    request = make_request("post", data, user=user, path="/modules/")
    view = view_class()
    view._librenms_api = live_librenms.api
    with _netbox_request(request) as queued:
        response = view_post(view, request, pk=device.pk)
    assert response.status_code == 302
    return request, queued


def _levels(request):
    return [m.level for m in messages.get_messages(request)]


def _updates(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk, action="update"
    )


def _install_user(tag, interface_change):
    """Return a user who may install a module, with the Interface change grant constrained by *interface_change*."""
    from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

    user = make_user_with_perms(
        f"{tag}-user",
        [
            ("view", Device),
            ("view", ModuleBay),
            ("view", ModuleType),
            ("add", Module),
            ("add", Interface),
            ("delete", Interface),
        ],
    )
    return grant(user, "change", Interface, constraints=interface_change)


def _carrier_install(device, module_type, user, live_librenms):
    """POST a carrier install, which has no inventory row and so needs no grant token."""
    from dcim.models import ModuleBay

    from netbox_librenms_plugin.views.sync.modules import InstallModuleView

    bay = ModuleBay.objects.get(device=device, name="Slot 1")
    data = {"module_bay_id": str(bay.pk), "module_type_id": str(module_type.pk), "server_key": "default"}
    return _post(InstallModuleView, device, data, user, live_librenms)


def _template_type(model, *names):
    from dcim.models import InterfaceTemplate

    module_type = make_module_type(model)
    for name in names:
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type="1000base-t")
    return module_type


class TestModuleComponentAdoption:
    def test_an_install_that_adopts_a_component_out_of_the_change_scope_is_refused(self, live_librenms):
        from dcim.models import Module

        device = make_device_with_module_bays("rb-adopt", ["Slot 1"])
        module_type = _template_type("RB-ADOPT-CARD", "Gi1/0/1")
        standalone = make_interface(device, "Gi1/0/1")
        user = _install_user("rb-adopt", {"module__isnull": True})

        request, queued = _carrier_install(device, module_type, user, live_librenms)

        standalone.refresh_from_db()
        assert standalone.module_id is None
        assert not Module.objects.filter(device=device).exists()
        assert not _updates(standalone).exists()
        assert ("interface", standalone.pk) not in queued
        assert messages.ERROR in _levels(request)

    def test_a_replace_that_adopts_a_component_out_of_the_change_scope_is_refused(self, live_librenms):
        from dcim.models import Device, Interface, Module, ModuleType

        from netbox_librenms_plugin.tests.view_test_helpers import rendered_modal_form
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device_with_module_bays("rb-replace", ["Slot 1"])
        seed_mapping(device, "default", own=81)
        installed = install_module(device, "Slot 1", "RB-REPLACE-OLD")
        new_type = _template_type("RB-REPLACE-NEW", "Gi1/0/1")
        standalone = make_interface(device, "Gi1/0/1")
        seed_module_rows(device, [_row(new_type.model, "Slot 1")], librenms_id=81)
        _action, fields = rendered_modal_form(device, ENT_INDEX, "replace-module")
        user = make_user_with_perms(
            "rb-replace-user",
            [
                ("view", Device),
                ("view", ModuleType),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"module__isnull": True})

        request, queued = _post(ReplaceModuleView, device, fields, user, live_librenms)

        standalone.refresh_from_db()
        assert standalone.module_id is None
        assert Module.objects.filter(pk=installed.pk).exists()
        assert not Module.objects.filter(module_type=new_type).exists()
        assert not _updates(standalone).exists()
        assert ("interface", standalone.pk) not in queued
        assert messages.ERROR in _levels(request)


def _row(model, name, **extra):
    return {
        "entPhysicalIndex": ENT_INDEX,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
        **extra,
    }


class TestVirtualChassisNormalization:
    """The install renames the module's interfaces to the member position; each write is one row."""

    @staticmethod
    def _member(tag):
        first = make_device(f"{tag}-first")
        device = make_device_with_module_bays(f"{tag}-second", ["Slot 1"])
        make_virtual_chassis(f"{tag}-vc", first, device)
        return device

    def test_a_rename_out_of_the_change_scope_is_rolled_back_and_skipped(self, live_librenms):
        from dcim.models import Interface

        device = self._member("rb-vc-rename")
        module_type = _template_type("RB-VC-RENAME-CARD", "Te1/1/1")
        user = _install_user("rb-vc-rename", {"name__startswith": "Te1/"})

        request, _queued = _carrier_install(device, module_type, user, live_librenms)

        interface = Interface.objects.get(device=device, module__module_type=module_type)
        assert interface.name == "Te1/1/1"
        assert not _updates(interface).exists()
        assert messages.WARNING in _levels(request)
        assert messages.ERROR not in _levels(request)

    def test_an_adoption_out_of_the_change_scope_is_rolled_back_and_skipped(self, live_librenms):
        from dcim.models import Interface

        device = self._member("rb-vc-adopt")
        module_type = _template_type("RB-VC-ADOPT-CARD", "Te1/1/1")
        standalone = make_interface(device, "Te2/1/1")
        user = _install_user("rb-vc-adopt", [{"module__isnull": True}, {"name__startswith": "Te1/"}])

        request, queued = _carrier_install(device, module_type, user, live_librenms)

        standalone.refresh_from_db()
        assert standalone.module_id is None
        assert Interface.objects.filter(device=device, module__module_type=module_type, name="Te1/1/1").exists()
        assert not _updates(standalone).exists()
        assert ("interface", standalone.pk) not in queued
        assert messages.WARNING in _levels(request)


class TestUpdateModuleInterface:
    """The Update Interface action binds the row's port and adopts the standalone template interfaces."""

    @staticmethod
    def _update(tag, live_librenms, port_id=None):
        from dcim.models import Device, Interface, InterfaceTemplate, Module

        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device_with_module_bays(tag, ["Slot 1"])
        seed_mapping(device, "default", own=82)
        module = install_module(device, "Slot 1", f"{tag.upper()}-CARD")
        # Added after the install, so the interface stays standalone and the module may adopt it.
        InterfaceTemplate.objects.create(module_type=module.module_type, name="Gi0/1", type="1000base-t")
        standalone = make_interface(device, "Gi0/1")
        seed_module_rows(
            device,
            [_row(module.module_type.model, "Slot 1")],
            librenms_id=82,
            bound_ports=[(ENT_INDEX, port_id, "Gi0/1")] if port_id is not None else (),
        )
        _action, fields = rendered_module_form(render_module_tab(device), "update-module-interface", ENT_INDEX)
        user = make_user_with_perms(f"{tag}-user", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints={"module__isnull": True})
        request, queued = _post(UpdateModuleInterfaceView, device, fields, user, live_librenms)
        standalone.refresh_from_db()
        return request, queued, standalone, mapping_summary(standalone)

    def test_a_bind_out_of_the_change_scope_is_rolled_back(self, live_librenms):
        request, queued, interface, mapping = self._update("rb-bind", live_librenms, port_id=8201)

        assert interface.module_id is None
        assert "default" not in mapping
        assert not _updates(interface).exists()
        assert ("interface", interface.pk) not in queued
        assert messages.WARNING in _levels(request)
        assert messages.SUCCESS not in _levels(request)

    def test_an_adoption_out_of_the_change_scope_keeps_the_bind_of_the_module_interface(self, live_librenms):
        from dcim.models import Device, Interface, InterfaceTemplate, Module

        from netbox_librenms_plugin.server_mappings import read_mapping
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device_with_module_bays("rb-bind-adopt", ["Slot 1"])
        seed_mapping(device, "default", own=84)
        module = install_module(device, "Slot 1", "RB-BIND-ADOPT-CARD")
        InterfaceTemplate.objects.create(module_type=module.module_type, name="Gi0/1", type="1000base-t")
        InterfaceTemplate.objects.create(module_type=module.module_type, name="Gi0/2", type="1000base-t")
        bound = Interface.objects.create(device=device, module=module, name="Gi0/1", type="1000base-t")
        standalone = make_interface(device, "Gi0/2")
        rows = [_row(module.module_type.model, "Slot 1")]
        seed_module_rows(device, rows, librenms_id=84, bound_ports=[(ENT_INDEX, 8401, "Gi0/1")])
        _action, fields = rendered_module_form(render_module_tab(device), "update-module-interface", ENT_INDEX)
        user = make_user_with_perms("rb-bind-adopt-user", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints=[{"module__isnull": True}, {"name": "Gi0/1"}])

        request, queued = _post(UpdateModuleInterfaceView, device, fields, user, live_librenms)

        bound.refresh_from_db()
        standalone.refresh_from_db()
        assert read_mapping(bound).own_id("default") == 8401
        assert standalone.module_id is None
        assert not _updates(standalone).exists()
        assert ("interface", standalone.pk) not in queued
        assert sorted(_levels(request)) == [messages.SUCCESS, messages.WARNING]

    def test_an_adoption_out_of_the_change_scope_is_rolled_back(self, live_librenms):
        request, queued, interface, _mapping = self._update("rb-template-adopt", live_librenms)

        assert interface.module_id is None
        assert not _updates(interface).exists()
        assert ("interface", interface.pk) not in queued
        assert messages.WARNING in _levels(request)
        assert messages.SUCCESS not in _levels(request)


def test_a_serial_update_out_of_the_change_scope_is_refused(live_librenms):
    from dcim.models import Device, Module

    from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

    device = make_device_with_module_bays("rb-serial", ["Slot 1"])
    seed_mapping(device, "default", own=83)
    module = install_module(device, "Slot 1", "RB-SERIAL-CARD", serial="OLD-1")
    seed_module_rows(device, [_row(module.module_type.model, "Slot 1", entPhysicalSerialNum="NEW-1")], librenms_id=83)
    _action, fields = rendered_module_form(render_module_tab(device), "update-module-serial", ENT_INDEX)
    user = make_user_with_perms("rb-serial-user", [("view", Device)])
    user = grant(user, "change", Module, constraints={"serial__startswith": "OLD-"})

    request, queued = _post(UpdateModuleSerialView, device, fields, user, live_librenms)

    module.refresh_from_db()
    assert module.serial == "OLD-1"
    assert not _updates(module).exists()
    assert ("module", module.pk) not in queued
    assert messages.ERROR in _levels(request)


def test_an_interface_type_change_out_of_the_change_scope_drops_its_event(live_librenms):
    from dcim.models import Device, Interface, InterfaceTemplate, Module

    from netbox_librenms_plugin.views.sync.modules import ApplyModuleInterfaceTypesView

    device = make_device_with_module_bays("rb-type", ["Slot 1"])
    module = install_module(device, "Slot 1", "RB-TYPE-CARD")
    InterfaceTemplate.objects.create(module_type=module.module_type, name="Gi0/1", type="10gbase-x-sfpp")
    interface = Interface.objects.create(device=device, module=module, name="Gi0/1", type="1000base-t")
    user = make_user_with_perms("rb-type-user", [("view", Device), ("view", Module)])
    user = grant(user, "change", Interface, constraints={"type": "1000base-t"})
    data = {
        "server_key": "default",
        "module_id": str(module.pk),
        "interface_id": [str(interface.pk)],
        f"current_type_{interface.pk}": "1000base-t",
        f"template_type_{interface.pk}": "10gbase-x-sfpp",
    }

    request, queued = _post(ApplyModuleInterfaceTypesView, device, data, user, live_librenms)

    interface.refresh_from_db()
    assert interface.type == "1000base-t"
    assert not _updates(interface).exists()
    assert ("interface", interface.pk) not in queued
    assert messages.ERROR in _levels(request)


def _changes(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk
    )


def _response_levels(response):
    return [m.level for m in messages.get_messages(response.wsgi_request)]


# Transactional: the cable sync commits each row through run_transaction, which keeps the events only on a commit.
@transactional_db_with_all_apps()
def test_a_cable_tag_that_moves_the_cable_out_of_the_change_scope_is_rolled_back(flushed_events):
    from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
    from django.test import Client

    from netbox_librenms_plugin.tests.test_cable_resync import untagged_serial_cable

    _port, cable, _admin, url, data = untagged_serial_cable("rb-cable-tag")
    user = make_user_with_perms(
        "rb-cable-tag-user",
        [("view", Device), ("view", Cable), ("add", Cable), ("view", ConsoleServerPort), ("view", ConsolePort)],
    )
    user = grant(user, "change", ConsoleServerPort)
    user = grant(user, "change", ConsolePort)
    user = grant(user, "change", Cable, constraints={"tags__isnull": True})
    client = Client()
    client.force_login(user)

    response = client.post(url, data=data)

    assert response.status_code == 302
    assert not cable.tags.exists()
    assert not _changes(cable).exists()
    assert ("cable", cable.pk) not in flushed_events
    assert _response_levels(response) == [messages.ERROR]


@transactional_db_with_all_apps()
@pytest.mark.usefixtures("breakout_server")
def test_a_breakout_extend_that_moves_the_cable_out_of_the_change_scope_is_rolled_back(flushed_events):
    from dcim.models import Cable, Interface
    from django.urls import reverse

    from netbox_librenms_plugin.tests.test_cable_breakout import _Breakout, _constrained_user
    from netbox_librenms_plugin.tests.test_cable_overwrite import _rendered_sync_data

    if not hasattr(Interface, "channel_id"):
        pytest.skip("NetBox before 4.7 has no channel interfaces")
    setup = _Breakout("rb-extend")
    setup.seed(setup.row(1), setup.row(2))
    setup.sync(1)
    cable = setup.parent_cable()
    logged = _changes(cable).count()
    flushed_events.clear()
    # The save of the extend moves last_updated past this bound, so the saved cable leaves the grant.
    setup.client.force_login(
        _constrained_user("rb-extend-user", cable_change={"last_updated__lte": cable.last_updated.isoformat()})
    )
    data = _rendered_sync_data(setup.client, setup.device, "102")

    response = setup.client.post(
        reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[setup.device.pk]), data
    )

    assert response.status_code == 302
    assert setup.b_side(Cable.objects.get(pk=cable.pk)) == [(1, setup.far_ends[0])]
    assert _changes(cable).count() == logged
    assert ("cable", cable.pk) not in flushed_events
    assert messages.ERROR in _response_levels(response)


def test_a_rebind_that_moves_the_interface_out_of_the_change_scope_is_rolled_back(client, settings, flushed_events):
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
    from netbox_librenms_plugin.tests.test_interface_name_owner_rebind import (
        HOST_PORT,
        STALE_PORT,
        _binding,
        _bound_interface,
        _device,
        _port,
        _rebind,
        _seed,
    )

    configure_default_librenms_server(settings)
    device = _device("rb-rebind")
    interface = _bound_interface(device, "eth0", STALE_PORT)
    _seed(device, [_port(HOST_PORT, "eth0")])
    user = make_user_with_perms("rb-rebind-user", [("view", Device), ("view", Interface)])
    # The grant names the stored mapping on purpose: the rebind writes only the mapping.
    binding = {"custom_field_data__librenms_id__default": STALE_PORT}  # nosemgrep: no-stored-mapping-access-orm
    user = grant(user, "change", Interface, constraints=binding)
    client.force_login(user)

    response = _rebind(client, device, HOST_PORT)

    assert _binding(interface) == STALE_PORT
    assert not _updates(interface).exists()
    assert ("interface", interface.pk) not in flushed_events
    assert _response_levels(response) == [messages.ERROR]


@pytest.mark.usefixtures("servers")
class TestServerMappingWrites:
    """The mapping views of a Device write only its mapping, which a grant constraint can still name on purpose."""

    @staticmethod
    def _owner(tag, constraints):
        from dcim.models import Device

        owner = seed_mapping(make_device(tag), "primary", own=13601, preferred=True, save=False)
        seed_mapping(owner, "secondary", own=13602, save=False)
        seed_mapping(owner, "retired", own=13603)
        return owner, make_user_with_perms(f"{tag}-user", [("change", Device)], constraints=constraints)

    def test_a_removal_that_moves_the_device_out_of_the_change_scope_is_rolled_back(self, client, flushed_events):
        from django.urls import reverse

        has_retired = {"custom_field_data__librenms_id__has_key": "retired"}  # nosemgrep: no-stored-mapping-access-orm
        owner, user = self._owner("rb-mapping-remove", has_retired)
        before = mapping_summary(owner)
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]),
            {"object_type": "device", "server_key": "retired"},
        )

        assert response.status_code == 302
        owner.refresh_from_db()
        assert mapping_summary(owner) == before
        assert not _updates(owner).exists()
        assert ("device", owner.pk) not in flushed_events
        assert _response_levels(response) == [messages.ERROR]

    def test_a_preference_that_moves_the_device_out_of_the_change_scope_is_rolled_back(self, client, flushed_events):
        from netbox_librenms_plugin.tests.test_object_server_preference import _preference_url

        preferred = "custom_field_data__librenms_id___preferred_server"  # nosemgrep: no-stored-mapping-access-orm
        prefers_primary = {preferred: "primary"}  # nosemgrep: no-stored-mapping-access-orm
        owner, user = self._owner("rb-mapping-prefer", prefers_primary)
        before = mapping_summary(owner)
        client.force_login(user)

        response = client.post(_preference_url(owner), {"object_type": "device", "server_key": "secondary"})

        assert response.status_code == 302
        owner.refresh_from_db()
        assert mapping_summary(owner) == before
        assert not _updates(owner).exists()
        assert ("device", owner.pk) not in flushed_events
        assert _response_levels(response) == [messages.ERROR]
