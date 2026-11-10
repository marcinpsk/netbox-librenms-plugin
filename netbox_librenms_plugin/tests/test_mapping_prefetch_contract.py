"""
The read-loading contract: every collection producer prefetches the mappings it reads.

Each test drives a producer through its real entry point, mostly an HTTP request to the view, on
real rows. The spy tests prove that every saved instance read in the producer was passed to
``prefetch_mappings`` first. The query-count tests prove that the producer runs the same number of
queries for one object and for many objects in mixed mapping states. On the custom-field storage a
missing prefetch changes no query count, so the spy tests catch it today and the query-count tests
catch it after the storage moves to its own tables.
"""

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    configure_default_librenms_server,
    make_cluster,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.mapping_prefetch_spy import spy_mapping_reads
from netbox_librenms_plugin.tests.source_snapshot_helpers import (
    link_row,
    port_pair,
    port_row,
    seed_cables_snapshot,
    seed_interfaces_snapshot,
    seed_ip_snapshot,
    seed_mapped_modules,
    transceiver_row,
)

SERVER_KEY = "default"
OTHER_SERVER = "secondary"
MANY = 100
# One link per mixed state: one object cannot hold every state, and a hostname-only link adds one name lookup.
FEW_LINKS = 5
# The cable catalog reads 32 identities per query and each link names two ports, so 16 links fit in one chunk.
MANY_LINKS = 16
HOST_ID = 61
NEIGHBOUR_BASE = 20_000
REMOTE_PORT_BASE = 30_000


@pytest.fixture
def superuser_client(client, settings):
    configure_default_librenms_server(settings)
    client.force_login(make_superuser("mapping-prefetch-user"))
    return client


def _seed_port_mapping(interface, index, port_id):
    """Give *interface* one of the mixed port mapping states, chosen by *index*."""
    state = index % 4
    if state == 1:
        seed_mapping(interface, SERVER_KEY, own=port_id)
    elif state == 2:
        seed_mapping(interface, SERVER_KEY, own=port_id)
        seed_mapping(interface, OTHER_SERVER, own=port_id + 50_000)
    elif state == 3:
        seed_stored_mapping(interface, "not-an-id", save=True)
    return interface


def _seed_device_mapping(device, index, librenms_id):
    """
    Give *device* one of the mixed device mapping states, chosen by *index*.

    The states are: own ID only, own plus OOB, own on two servers, absent and an invalid stored value.
    Returns the LibreNMS IDs that *device* holds on the default server.
    """
    state = index % 5
    if state == 0:
        seed_mapping(device, SERVER_KEY, own=librenms_id)
    elif state == 1:
        seed_mapping(device, SERVER_KEY, own=librenms_id, oob=librenms_id + 50_000)
        return (librenms_id, librenms_id + 50_000)
    elif state == 2:
        seed_mapping(device, SERVER_KEY, own=librenms_id)
        seed_mapping(device, OTHER_SERVER, own=librenms_id + 50_000)
    elif state == 4:
        seed_stored_mapping(device, "not-an-id", save=True)
    return (librenms_id,) if state < 3 else ()


def _port(port_id, name, if_type="ethernetCsmacd"):
    return {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": if_type,
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }


def _interfaces_device(name, count):
    """Return a device whose *count* interfaces are LAG members in mixed mapping states, with their snapshot."""
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": 61}})
    lag = make_interface(device, "ae0", iface_type="lag")
    seed_mapping(lag, SERVER_KEY, own=999)
    ports = [_port(999, "ae0", "ieee8023adLag")]
    stack = []
    for index in range(count):
        port_id = 1000 + index
        interface = make_interface(device, f"eth{index}", iface_type="1000base-t")
        interface.lag = lag
        interface.save()
        _seed_port_mapping(interface, index, port_id)
        ports.append(_port(port_id, f"eth{index}"))
        stack.append(port_pair(port_id, 999))
    seed_interfaces_snapshot(device, ports, stack, server_key=SERVER_KEY)
    return device


def _interfaces_tab(client, device):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
    )
    response = client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"})
    assert response.status_code == 200
    return response


def _tab(client, device, tab, **params):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": tab},
    )
    response = client.get(url, {"server_key": SERVER_KEY, **params})
    assert response.status_code == 200
    return response


def _ignore_rule():
    """Store an interface rule that ignores no port here, so the tables preload the port disclosure."""
    InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern="^no-such-port$")


def _assert_prefetched(spy, *single, models=None):
    assert spy.saved_reads(), "the request read no saved mapping, so the check proves nothing"
    violations = spy.unprefetched(single=single, models=models)
    assert not violations, "\n".join(violations)


def _query_count(request):
    """Return the queries *request* runs; NetBox reloads its config revision when its cache expires, so that is left out."""
    with CaptureQueriesContext(connection) as queries:
        request()
    return len([query for query in queries.captured_queries if "core_configrevision" not in query["sql"]])


@pytest.mark.django_db
class TestInterfacesTab:
    def test_every_interface_read_is_prefetched(self, superuser_client):
        device = _interfaces_device("prefetch-interfaces", 8)

        with spy_mapping_reads() as spy:
            _interfaces_tab(superuser_client, device)

        _assert_prefetched(spy, device)

    def test_the_query_count_does_not_grow_with_the_interfaces(self, superuser_client):
        one = _interfaces_device("prefetch-interfaces-one", 1)
        many = _interfaces_device("prefetch-interfaces-many", MANY)
        _interfaces_tab(superuser_client, _interfaces_device("prefetch-interfaces-warm", 1))

        counts = [
            _query_count(lambda device=device: _interfaces_tab(superuser_client, device)) for device in (one, many)
        ]

        assert counts[0] == counts[1], counts


def _cables_device(name, count):
    """Return a device whose *count* LLDP neighbours and both ends are in mixed mapping states, with its snapshot."""
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": HOST_ID}})
    links, ports, neighbour_ports = [], [], {}
    for index in range(count):
        local_port_id, neighbour_id, remote_port_id = 1000 + index, NEIGHBOUR_BASE + index, REMOTE_PORT_BASE + index
        local = make_interface(device, f"eth{index}", iface_type="1000base-t")
        _seed_port_mapping(local, index, local_port_id)
        neighbour = make_device(f"{name}-peer{index}")
        held_ids = _seed_device_mapping(neighbour, index, neighbour_id)
        remote = make_interface(neighbour, "eth-r", iface_type="1000base-t")
        _seed_port_mapping(remote, index, remote_port_id)
        neighbour_ports.update((held_id, []) for held_id in held_ids)
        links.append(
            link_row(
                local_port_id,
                "eth-r",
                local_port=f"eth{index}",
                remote_device_id=neighbour_id if held_ids else None,
                remote_port_id=remote_port_id,
                remote_hostname=neighbour.name,
            )
        )
        ports.append(port_row(local_port_id, f"eth{index}", device_id=HOST_ID))
        neighbour_ports[neighbour_id] = [port_row(remote_port_id, "eth-r", device_id=neighbour_id)]
    seed_cables_snapshot(device, links, ports, neighbour_ports=neighbour_ports, server_key=SERVER_KEY)
    return device


def _cables_tab(client, device):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "cables"},
    )
    response = client.get(url, {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert response.context["cable_sync"]["table"] is not None
    return response


@pytest.mark.django_db
class TestCablesTab:
    def test_every_device_and_interface_read_is_prefetched(self, superuser_client):
        _ignore_rule()
        device = _cables_device("prefetch-cables", 10)

        with spy_mapping_reads() as spy:
            _cables_tab(superuser_client, device)

        _assert_prefetched(spy, device)

    def test_the_query_count_does_not_grow_with_the_links(self, superuser_client):
        one = _cables_device("prefetch-cables-few", FEW_LINKS)
        many = _cables_device("prefetch-cables-many", MANY_LINKS)
        _cables_tab(superuser_client, _cables_device("prefetch-cables-warm", FEW_LINKS))

        counts = [_query_count(lambda device=device: _cables_tab(superuser_client, device)) for device in (one, many)]

        assert counts[0] == counts[1], counts


def _ip_device(name, count):
    """Return a device whose *count* interfaces in mixed mapping states each carry one IP row, with its snapshot."""
    device = make_device(name, librenms_cf={SERVER_KEY: {"id": HOST_ID}})
    ports = []
    for index in range(count):
        port_id = 1000 + index
        _seed_port_mapping(make_interface(device, f"eth{index}", iface_type="1000base-t"), index, port_id)
        ports.append({"port_id": port_id, "ifName": f"eth{index}", "ifDescr": f"eth{index}"})
    addresses = [
        {"ipv4_address": f"198.18.{index // 250}.{index % 250 + 1}", "ipv4_prefixlen": 24, "port_id": port["port_id"]}
        for index, port in enumerate(ports)
    ]
    seed_ip_snapshot(device, addresses, ports, server_key=SERVER_KEY)
    return device


@pytest.mark.django_db
class TestIPAddressesTab:
    def test_every_interface_read_is_prefetched(self, superuser_client):
        _ignore_rule()
        device = _ip_device("prefetch-ip", 8)

        with spy_mapping_reads() as spy:
            _tab(superuser_client, device, "ipaddresses")

        _assert_prefetched(spy, device)

    def test_the_query_count_does_not_grow_with_the_addresses(self, superuser_client):
        _ignore_rule()
        one = _ip_device("prefetch-ip-one", 1)
        many = _ip_device("prefetch-ip-many", MANY)
        _tab(superuser_client, _ip_device("prefetch-ip-warm", 1), "ipaddresses")

        counts = [
            _query_count(lambda device=device: _tab(superuser_client, device, "ipaddresses")) for device in (one, many)
        ]

        assert counts[0] == counts[1], counts


def _modules_device(name, count):
    """Return a device whose *count* transceivers name interfaces in mixed mapping states, with its snapshot."""
    device = make_device(name)
    inventory, ports, transceivers = [], [], []
    for index in range(count):
        port_id = 1000 + index
        _seed_port_mapping(make_interface(device, f"eth{index}", iface_type="1000base-t"), index, port_id)
        inventory.append(
            {
                "entPhysicalIndex": 100 + index,
                "entPhysicalName": f"Transceiver eth{index}",
                "entPhysicalDescr": f"Transceiver eth{index}",
                "entPhysicalModelName": "SFP-1G",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": f"SN{index}",
            }
        )
        ports.append({"port_id": port_id, "ifName": f"eth{index}", "ifDescr": f"eth{index}"})
        transceivers.append(transceiver_row(100 + index, port_id=port_id, model="SFP-1G", serial=f"SN{index}"))
    seed_mapped_modules(device, inventory, ports, transceivers, librenms_id=HOST_ID, server_key=SERVER_KEY)
    return device


@pytest.mark.django_db
class TestModulesTab:
    def test_every_interface_read_is_prefetched(self, superuser_client):
        device = _modules_device("prefetch-modules", 8)

        with spy_mapping_reads() as spy:
            _tab(superuser_client, device, "modules")

        _assert_prefetched(spy, device)

    def test_the_query_count_does_not_grow_with_the_items(self, superuser_client):
        one = _modules_device("prefetch-modules-one", 1)
        many = _modules_device("prefetch-modules-many", MANY)
        _tab(superuser_client, _modules_device("prefetch-modules-warm", 1), "modules")

        counts = [
            _query_count(lambda device=device: _tab(superuser_client, device, "modules")) for device in (one, many)
        ]

        assert counts[0] == counts[1], counts


@pytest.mark.django_db
class TestVirtualChassisInterfacesTab:
    def test_every_member_and_interface_read_is_prefetched(self, superuser_client):
        _chassis, members = make_virtual_chassis_members("prefetch-vc", count=3)
        for position, member in enumerate(members):
            _seed_device_mapping(member, position, HOST_ID + position)
        ports = []
        for index in range(6):
            member = members[index % 3]
            interface = make_interface(member, f"eth{index}", iface_type="1000base-t")
            _seed_port_mapping(interface, index, 1000 + index)
            ports.append(_port(1000 + index, f"eth{index}"))
        seed_interfaces_snapshot(members[0], ports, server_key=SERVER_KEY)

        with spy_mapping_reads() as spy:
            _tab(superuser_client, members[0], "interfaces", interface_name_field="ifName")

        _assert_prefetched(spy, members[0])


def _status_devices(tag, count):
    """Return the filter of a new device role whose *count* devices are in mixed mapping states."""
    from dcim.models import DeviceRole

    role = DeviceRole.objects.create(name=tag, slug=tag, color="00ff00")
    for index in range(count):
        device = make_device(f"{tag}-{index}")
        device.role = role
        device.save()
        _seed_device_mapping(device, index, NEIGHBOUR_BASE + index)
    return {"role": role.pk}


def _status_vms(tag, count):
    """Return the filter of a new cluster whose *count* virtual machines are in mixed mapping states."""
    cluster = make_cluster(tag)
    for index in range(count):
        _seed_device_mapping(make_vm(f"{tag}-{index}", cluster), index, NEIGHBOUR_BASE + index)
    return {"cluster": cluster.pk}


def _status_list(client, url_name, filters):
    response = client.get(reverse(f"plugins:netbox_librenms_plugin:{url_name}"), {**filters, "per_page": 1000})
    assert response.status_code == 200
    return response


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "seed"),
    [("device_status_list", _status_devices), ("vm_status_list", _status_vms)],
    ids=["device", "vm"],
)
class TestStatusLists:
    @pytest.fixture
    def status_client(self, client, settings, librenms_server):
        bind_librenms_server(settings, librenms_server, server_key=SERVER_KEY)
        client.force_login(make_superuser("mapping-prefetch-status-user"))
        return client

    def test_every_listed_object_read_is_prefetched(self, status_client, url_name, seed):
        filters = seed("prefetch-status", 10)

        with spy_mapping_reads() as spy:
            _status_list(status_client, url_name, filters)

        _assert_prefetched(spy)

    def test_the_query_count_does_not_grow_with_the_objects(self, status_client, url_name, seed):
        few = seed("prefetch-status-few", FEW_LINKS)
        many = seed("prefetch-status-many", MANY)
        _status_list(status_client, url_name, seed("prefetch-status-warm", FEW_LINKS))

        counts = [
            _query_count(lambda filters=filters: _status_list(status_client, url_name, filters))
            for filters in (few, many)
        ]

        assert counts[0] == counts[1], counts


@pytest.mark.django_db
class TestCableWrites:
    """Each cable writer reads a few locked ends and owners; it prefetches them as one collection."""

    def test_a_cable_sync_prefetches_its_ends_and_owners(self, client, live_librenms):
        from dcim.models import Cable

        from netbox_librenms_plugin.tests.test_cable_snapshot_read_actions import _pair, _sync
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        _ignore_rule()
        subject, _neighbour, local, remote, *_rest = _pair(remote_interface=True)
        client.force_login(make_superuser("mapping-prefetch-cable-sync"))
        producers = [
            (SyncCablesView, name)
            for name in (
                "_apply_cable_action",
                "_rule_block",
                "_snapshot_cable_port_records",
                "_locked_ends_may_be_row_ports",
            )
        ]

        with spy_mapping_reads(*producers) as spy:
            response = _sync(client, subject, local, remote)

        assert response.status_code == 302
        assert Cable.objects.count() == 1
        assert spy.reading_scopes() == {name for _owner, name in producers}
        _assert_prefetched(spy)

    def test_a_cable_verify_prefetches_the_page_devices(self, client, live_librenms):
        from netbox_librenms_plugin.tests.test_cable_snapshot_read_actions import LOCAL_PORT_ID, _pair
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        subject, *_rest = _pair(remote_interface=True)
        client.force_login(make_superuser("mapping-prefetch-cable-verify"))

        with spy_mapping_reads((SingleCableVerifyView, "post")) as spy:
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:verify_cable"),
                {"device_id": subject.pk, "row_id": str(LOCAL_PORT_ID), "server_key": SERVER_KEY},
                content_type="application/json",
            )

        assert response.status_code == 200
        _assert_prefetched(spy)

    def test_a_far_end_create_prefetches_the_page_devices(self, client, live_librenms):
        from dcim.models import Cable

        from netbox_librenms_plugin import utils
        from netbox_librenms_plugin.tests.test_cable_snapshot_read_actions import LOCAL_PORT_ID, _pair

        subject, _neighbour, local, *_rest = _pair()
        client.force_login(make_superuser("mapping-prefetch-cable-create"))

        with spy_mapping_reads((utils, "any_device_migrated")) as spy:
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[subject.pk]),
                {"server_key": SERVER_KEY, "row_id": str(LOCAL_PORT_ID), "expected_local_id": local.pk},
            )

        assert response.status_code == 302
        assert Cable.objects.count() == 1
        _assert_prefetched(spy)

    def test_a_cable_refresh_prefetches_the_neighbours_and_port_owners(self, client, live_librenms):
        from netbox_librenms_plugin import cable_scope, source_cache
        from netbox_librenms_plugin.acquisition import resources

        subject = make_device("prefetch-refresh", librenms_cf={SERVER_KEY: {"id": HOST_ID}})
        _chassis, chassis_members = make_virtual_chassis_members("prefetch-refresh-vc", count=2)
        neighbours = [make_device(f"prefetch-refresh-peer{index}") for index in range(6)] + chassis_members
        links, ports = [], []
        server = live_librenms.server
        for index, neighbour in enumerate(neighbours):
            neighbour_id, local_port_id, remote_port_id = NEIGHBOUR_BASE + index, 1000 + index, REMOTE_PORT_BASE + index
            held_ids = _seed_device_mapping(neighbour, index, neighbour_id)
            local = make_interface(subject, f"eth{index}", iface_type="1000base-t")
            _seed_port_mapping(local, index, local_port_id)
            _seed_port_mapping(make_interface(neighbour, "eth-r", iface_type="1000base-t"), index, remote_port_id)
            links.append(
                link_row(
                    local_port_id,
                    "eth-r",
                    local_port=f"eth{index}",
                    remote_port_id=remote_port_id,
                    remote_hostname=neighbour.name,
                )
            )
            ports.append(port_row(local_port_id, f"eth{index}", device_id=HOST_ID))
            for held_id in held_ids:
                # The first neighbour's ports fail, so the tab names the failed source to the actor.
                body = {"status": "ok", "ports": [port_row(remote_port_id, "eth-r", device_id=held_id)]}
                server.register(f"/api/v0/devices/{held_id}/ports", body, status=500 if index == 0 else 200)
        server.register(f"/api/v0/devices/{HOST_ID}/links", {"status": "ok", "links": links})
        server.register(f"/api/v0/devices/{HOST_ID}/ports", {"status": "ok", "ports": ports})
        client.force_login(make_superuser("mapping-prefetch-cable-refresh"))
        producers = [
            (resources, "_cable_parts"),
            (cable_scope, "cable_port_owner_ids"),
            (source_cache, "visible_cable_port_sources"),
        ]

        with spy_mapping_reads(*producers) as spy:
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[subject.pk]),
                {"server_key": SERVER_KEY},
                HTTP_HX_REQUEST="true",
            )

        assert response.status_code == 200
        assert spy.reading_scopes() == {name for _owner, name in producers}
        _assert_prefetched(spy)


@pytest.mark.django_db
def test_a_module_install_bind_prefetches_its_candidate_interfaces(live_librenms):
    """The bind writer reads the same-name and module interfaces of the item; it prefetches them first."""
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type
    from netbox_librenms_plugin.tests.test_module_install_views import _item, _post_branch, _with_installable_child
    from netbox_librenms_plugin.views.sync import modules

    device = make_device_with_module_bays("prefetch-bind", ["Slot 1"])
    seed_mapping(device, SERVER_KEY, own=65)
    module_type = make_module_type("PREFETCH-BIND-CARD")
    interface = make_interface(device, "Te1/1/1")
    rows = _with_installable_child([_item(10, module_type.model, "Slot 1")], 10, "prefetch-bind")

    with spy_mapping_reads((modules, "_select_bind_candidate")) as spy:
        _post_branch(
            device,
            rows,
            10,
            65,
            live_librenms,
            ports=[{"port_id": 6501, "ifName": interface.name}],
            transceivers=[transceiver_row(10, port_id=6501, model=module_type.model)],
        )

    interface.refresh_from_db()
    assert read_mapping(interface).own_id(SERVER_KEY) == 6501
    _assert_prefetched(spy)


@pytest.mark.django_db
def test_an_ip_sync_that_creates_interfaces_prefetches_the_existing_ones(client, settings, live_librenms):
    """The writer indexes every interface of the owner by its bound port before it creates a missing one."""
    from dcim.models import Interface

    from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _configure_test_server, _ip_row, _seed_ip_rows
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    _configure_test_server(settings)
    device = make_device("prefetch-ip-create", librenms_cf={SERVER_KEY: {"id": 42}})
    for index in range(8):
        _seed_port_mapping(make_interface(device, f"eth{index}", iface_type="1000base-t"), index, 1000 + index)
    _seed_ip_rows(device, [_ip_row("198.18.12.10", 7001, "Ethernet1", ifType="ethernetCsmacd")])
    client.force_login(make_superuser("mapping-prefetch-ip-create"))

    with spy_mapping_reads((SyncIPAddressesView, "_build_interface_creation_state")) as spy:
        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {"server_key": SERVER_KEY, "create-missing-interfaces-toggle": "on", "select": ["198.18.12.10/24"]},
        )

    assert response.status_code == 302
    assert Interface.objects.filter(device=device, name="Ethernet1").exists()
    _assert_prefetched(spy)


def _merge_harness():
    from netbox_librenms_plugin.tests.test_coverage_actions import _MergeViewHarness

    return _MergeViewHarness


class TestMergeView(_merge_harness()):
    """The merge reads the winner and donor sync devices together; it prefetches the pair first."""

    def test_the_merge_prefetches_the_winner_and_the_donor(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.imports import actions

        winner = make_device("prefetch-merge-winner", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("prefetch-merge-donor", librenms_cf={self.server_key: {"id": 10}})

        with spy_mapping_reads((actions, "_legacy_link_label")) as spy:
            response = self._post_merge(winner, donor)

        assert response.status_code == 200
        assert read_mapping(Device.objects.get(pk=donor.pk)).migrated_to(self.server_key) is not None
        assert {obj.pk for obj in spy.saved_reads()} == {winner.pk, donor.pk}
        _assert_prefetched(spy)


@pytest.mark.django_db
def test_the_sync_page_of_a_chassis_member_prefetches_the_members(superuser_client):
    """The sync owner and the mapped servers of a chassis come from the mappings of every member."""
    from netbox_librenms_plugin import server_mappings, server_selection

    _chassis, members = make_virtual_chassis_members("prefetch-page-vc", count=4)
    for position, member in enumerate(members):
        _seed_device_mapping(member, position + 2, HOST_ID + position)
    producers = [(server_selection, "get_librenms_sync_device"), (server_mappings, "mapped_device_servers")]

    with spy_mapping_reads(*producers) as spy:
        response = superuser_client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[members[0].pk]),
            {"server_key": SERVER_KEY},
        )

    assert response.status_code == 200
    assert spy.reading_scopes() == {name for _owner, name in producers}
    _assert_prefetched(spy)


@pytest.mark.django_db
def test_the_cables_tab_of_a_chassis_member_prefetches_the_local_owners(superuser_client):
    """The migrated-owner check reads the page device, its sync owner and each row's local owner."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    _chassis, members = make_virtual_chassis_members("prefetch-cables-vc", count=3)
    seed_mapping(members[0], SERVER_KEY, own=HOST_ID)
    links, ports = [], []
    for index, member in enumerate(members):
        port_id = 1000 + index
        seed_mapping(make_interface(member, f"eth{index}", iface_type="1000base-t"), SERVER_KEY, own=port_id)
        links.append(link_row(port_id, "eth-r", local_port=f"eth{index}", remote_hostname="prefetch-cables-vc-peer"))
        ports.append(port_row(port_id, f"eth{index}", device_id=HOST_ID))
    seed_cables_snapshot(members[1], links, ports, server_key=SERVER_KEY)

    with spy_mapping_reads((BaseCableTableView, "_disable_actions_for_migrated_owners")) as spy:
        _cables_tab(superuser_client, members[1])

    assert len({obj.pk for obj in spy.saved_reads()}) == len(members)
    _assert_prefetched(spy)


@pytest.mark.django_db
def test_the_interfaces_tab_of_a_virtual_machine_prefetches_its_interfaces(superuser_client):
    from virtualization.models import VMInterface

    vm = make_vm("prefetch-vm-interfaces")
    seed_mapping(vm, SERVER_KEY, own=HOST_ID)
    parent = VMInterface.objects.create(virtual_machine=vm, name="eth0")
    seed_mapping(parent, SERVER_KEY, own=999)
    ports = [_port(999, "eth0")]
    for index in range(1, 8):
        interface = VMInterface.objects.create(virtual_machine=vm, name=f"eth0.{index}", parent=parent)
        _seed_port_mapping(interface, index, 1000 + index)
        ports.append(_port(1000 + index, f"eth0.{index}"))
    seed_interfaces_snapshot(vm, ports, [port_pair(1000 + index, 999) for index in range(1, 8)], server_key=SERVER_KEY)
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "virtualmachine", "pk": vm.pk, "tab": "interfaces"},
    )

    with spy_mapping_reads() as spy:
        response = superuser_client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"})

    assert response.status_code == 200
    _assert_prefetched(spy, vm)


@pytest.mark.django_db
def test_an_interface_verify_prefetches_the_relationship_index(superuser_client):
    """The verify repaint indexes the owner's interfaces by bound port to resolve the row's LAG."""
    from netbox_librenms_plugin import interface_relationships

    device = _interfaces_device("prefetch-verify", 6)

    with spy_mapping_reads((interface_relationships, "build_interface_index")) as spy:
        response = superuser_client.post(
            reverse("plugins:netbox_librenms_plugin:verify_interface"),
            json.dumps(
                {"device_id": device.pk, "port_id": 1001, "interface_name_field": "ifName", "server_key": SERVER_KEY}
            ),
            content_type="application/json",
        )

    assert response.status_code == 200
    assert spy.reading_scopes() == {"build_interface_index"}
    _assert_prefetched(spy)
