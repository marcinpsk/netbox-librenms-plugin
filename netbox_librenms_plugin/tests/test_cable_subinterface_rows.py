"""Sub-interface neighbour rows on the cable tab.

The primary home for the cable view is test_cable_verify.py. These cases live in their
own file so they do not collide at that shared file's tail when the stack is restacked.

A router advertises LLDP from a physical port and from each sub-unit configured on it, so
one local port can report the same neighbour three times. A cable terminates on the
physical port only, so the sub-unit rows can never be anything but a mismatch.
"""

import pytest


def _link(local_id, local_name, remote_port, remote_port_id, *, remote_device_id=55):
    """One LibreNMS LLDP link row."""
    return {
        "local_port_id": local_id,
        "local_port": local_name,
        "remote_port": remote_port,
        "remote_port_id": remote_port_id,
        "remote_hostname": "peer.example.test",
        "remote_device_id": remote_device_id,
    }


def _acquire_links(links, live_librenms):
    """Acquire neighbour source facts over HTTP."""
    from uuid import uuid4

    from netbox_librenms_plugin.acquisition.coordinator import acquire
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
    from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms

    subject = map_device_to_librenms(make_device(f"subunit-source-{uuid4().hex}"), 42, server_key="default")
    live_librenms.server.register("/api/v0/devices/42/links", {"status": "ok", "links": links})
    live_librenms.server.register("/api/v0/devices/42/ports", {"status": "ok", "ports": []})
    for link in links:
        neighbour_id = coerce_librenms_id(link.get("remote_device_id"))
        if neighbour_id is not None:
            live_librenms.server.register(f"/api/v0/devices/{neighbour_id}/ports", {"status": "ok", "ports": []})
    snapshot = acquire(subject, server_key="default", resources=[Resource.CABLES])
    return subject, snapshot


def _collect(links, live_librenms):
    """Assess valid source facts for the reported neighbour identities."""
    from netbox_librenms_plugin.assessment.cables import assess

    subject, snapshot = _acquire_links(links, live_librenms)
    assessed = assess(snapshot, subject=subject)
    assert not assessed["identity_error"]
    return assessed["links"]


def _assert_failed_links(links, live_librenms):
    """Require malformed link rows to block their source instead of yielding an empty collection."""
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.acquisition.resources import is_ready
    from netbox_librenms_plugin.acquisition.snapshot import IncompleteSource, Outcome

    _subject, snapshot = _acquire_links(links, live_librenms)
    record = snapshot.record(Resource.CABLES, "links")
    assert record.outcome is Outcome.FAILED
    assert record.request.path == "devices/42/links"
    assert not is_ready(snapshot, Resource.CABLES)
    with pytest.raises(IncompleteSource):
        snapshot.value(Resource.CABLES, "links")


@pytest.mark.django_db
class TestSubInterfaceNeighbourRows:
    """Only the physical remote port can carry a cable."""

    def test_sub_unit_rows_are_dropped_when_their_physical_port_is_present(self, live_librenms):
        """The reported case: one cable, plus two sub-units that can only ever mismatch."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.1", 203),
            ],
            live_librenms,
        )

        assert [r["remote_port"] for r in rows] == ["FourHundredGigE0/0/0/8"]

    def test_a_sub_unit_is_kept_when_its_physical_port_was_not_reported(self, live_librenms):
        """Never drop the only evidence of a neighbour: keep it and let the user judge."""
        rows = _collect([_link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202)], live_librenms)

        assert [r["remote_port"] for r in rows] == ["FourHundredGigE0/0/0/8.100"]

    def test_two_real_neighbours_on_one_local_port_both_survive(self, live_librenms):
        """A breakout or a hub can legitimately show two physical neighbours."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/9", 204),
            ],
            live_librenms,
        )

        assert sorted(r["remote_port"] for r in rows) == [
            "FourHundredGigE0/0/0/8",
            "FourHundredGigE0/0/0/9",
        ]

    def test_a_sub_unit_of_a_different_neighbour_is_not_dropped(self, live_librenms):
        """The physical port must belong to the SAME remote device to mask a sub-unit."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8.100", 202, remote_device_id=77),
            ],
            live_librenms,
        )

        assert sorted(r["remote_port"] for r in rows) == [
            "FourHundredGigE0/0/0/8",
            "FourHundredGigE0/0/0/8.100",
        ]

    def test_a_sub_unit_on_a_different_local_port_is_not_dropped(self, live_librenms):
        """Masking is per local port: another port's cable says nothing about this one."""
        rows = _collect(
            [
                _link(100, "1/1/c28/1", "FourHundredGigE0/0/0/8", 201),
                _link(101, "1/1/c28/2", "FourHundredGigE0/0/0/8.100", 202),
            ],
            live_librenms,
        )

        assert len(rows) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("remote_port", [True, 123, ["eth0"], {"name": "eth0"}])
def test_malformed_remote_port_blocks_the_source(remote_port, live_librenms):
    """A malformed remote name must refuse the source before assessment."""
    _assert_failed_links([_link(100, "eth0", remote_port, 201)], live_librenms)


@pytest.mark.django_db
@pytest.mark.parametrize("same_hostname", [False, True])
def test_missing_remote_device_id_groups_by_hostname(same_hostname, live_librenms):
    """Only the same remote hostname can mask a sub-unit without a device ID."""
    physical = _link(100, "eth0", "eth1", 201, remote_device_id=None)
    physical["remote_hostname"] = "peer-a.example"
    sub_unit = _link(100, "eth0", "eth1.100", 202, remote_device_id=None)
    sub_unit["remote_hostname"] = "peer-a.example" if same_hostname else "peer-b.example"
    rows = _collect([physical, sub_unit], live_librenms)
    assert len(rows) == (1 if same_hostname else 2)


@pytest.mark.django_db
def test_a_padded_hostname_resolves_the_same_device_as_the_grouping():
    """The grouping identity trims, so a padded hostname must not group with its neighbour and
    then still report the device as missing."""
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    device = make_device("padded-neighbour")

    found, matched, error = BaseCableTableView().get_device_by_id_or_name(None, "  padded-neighbour  ", "default")

    assert found == device
    assert matched
    assert error is None


@pytest.mark.django_db
def test_hostname_grouping_folds_case_like_the_device_lookup(live_librenms):
    """get_device_by_id_or_name resolves a hostname with name__iexact, so two spellings of one
    neighbour must land in the same group and the physical row must still mask the sub-unit."""
    physical = _link(100, "eth0", "eth1", 201, remote_device_id=None)
    physical["remote_hostname"] = "PEER-A.example"
    sub_unit = _link(100, "eth0", "eth1.100", 202, remote_device_id=None)
    sub_unit["remote_hostname"] = "  peer-a.example  "

    rows = _collect([physical, sub_unit], live_librenms)

    assert [row["remote_port"] for row in rows] == ["eth1"]


@pytest.mark.django_db
@pytest.mark.parametrize("hostname", [None, ""])
def test_unknown_remote_hostname_preserves_rows_and_skips_name_lookup(hostname, live_librenms):
    """Unknown neighbor identities cannot mask rows or identify a device by name."""
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    links = [
        _link(100, "eth0", "eth1", 201, remote_device_id=None),
        _link(100, "eth0", "eth1.100", 202, remote_device_id=None),
    ]
    for link in links:
        link["remote_hostname"] = hostname
    rows = _collect(links, live_librenms)
    assert [row["remote_port"] for row in rows] == ["eth1", "eth1.100"]
    device, matched, _error = BaseCableTableView().get_device_by_id_or_name(None, hostname, "default")
    assert device is None
    assert not matched


@pytest.mark.django_db
@pytest.mark.parametrize("hostname", [[], {}, 123, True])
def test_malformed_remote_hostname_blocks_the_source(hostname, live_librenms):
    link = _link(100, "eth0", "eth1", 201, remote_device_id=None)
    link["remote_hostname"] = hostname
    _assert_failed_links([link], live_librenms)


@pytest.mark.django_db
def test_remote_device_id_resolves_with_malformed_hostname():
    """A valid remote ID remains usable when the advertised hostname is malformed."""
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    device = make_device("remote-id-without-name")
    # The canonical writer, so the test pins ID resolution rather than a hand-built field shape.
    seed_mapping(device, own=42, save=False)
    device.save()
    found, matched, error = BaseCableTableView().get_device_by_id_or_name(42, [], "default")
    assert found == device
    assert matched
    assert error is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "id_field, malformed_id",
    [("local_port_id", value) for value in ([], {}, True, 1.5, None, "", 0)]
    + [("remote_device_id", value) for value in ([], {}, True, 1.5)],
)
def test_malformed_cable_identifiers_block_the_source(id_field, malformed_id, live_librenms):
    """Malformed identifiers must refuse the source before assessment."""
    links = [
        _link(100, "eth0", "eth1", 201),
        _link(100, "eth0", "eth1.100", 202),
    ]
    for index, link in enumerate(links):
        link[id_field] = malformed_id
        link["remote_hostname"] = f"peer-{index}.example"
    _assert_failed_links(links, live_librenms)


@pytest.mark.django_db
@pytest.mark.parametrize("remote_device_id", [None, "", 0])
def test_unknown_remote_identifier_does_not_mask_different_neighbors(remote_device_id, live_librenms):
    links = [
        _link(100, "eth0", "eth1", 201, remote_device_id=remote_device_id),
        _link(100, "eth0", "eth1.100", 202, remote_device_id=remote_device_id),
    ]
    for index, link in enumerate(links):
        link["remote_hostname"] = f"peer-{index}.example"

    rows = _collect(links, live_librenms)

    assert [row["remote_port"] for row in rows] == ["eth1", "eth1.100"]


@pytest.mark.django_db
@pytest.mark.parametrize("remote_device_id", [None, "", 0])
@pytest.mark.parametrize("hostname", [None, "peer.example"])
def test_unknown_remote_identifier_can_use_a_matching_hostname(remote_device_id, hostname, live_librenms):
    """A shared hostname identifies the physical neighbor when its ID is unknown."""
    links = [
        _link(100, "eth0", "eth1", 201, remote_device_id=remote_device_id),
        _link(100, "eth0", "eth1.100", 202, remote_device_id=remote_device_id),
    ]
    for link in links:
        link["remote_hostname"] = hostname
    expected = ["eth1"] if hostname else ["eth1", "eth1.100"]
    assert [row["remote_port"] for row in _collect(links, live_librenms)] == expected
