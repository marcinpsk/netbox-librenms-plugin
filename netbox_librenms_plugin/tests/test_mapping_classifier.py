"""Tests for the classifier of stored mappings and its check script, on real NetBox rows."""

import ast
import copy
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from netbox_librenms_plugin import mapping_classifier
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.server_mappings import ContainerStatus, PreferenceStatus, read_mapping
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_interface,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping

BIGINT_MAX = 2**63 - 1
MARKER = {"device_id": 9, "server_key": "primary", "at": "2026-01-01T00:00:00Z"}


def _server_settings(*keys):
    return {key: {"librenms_url": f"https://{key}.example.com", "api_token": "token"} for key in keys}


def _configure(settings, *keys):
    configure_librenms_servers(settings, _server_settings(*keys))


def own(server, identity, *, oob=False, space="device"):
    return mapping_classifier.Identity(server, space, identity, is_own=True, is_oob=oob)


def oob(server, identity):
    return mapping_classifier.Identity(server, "device", identity, is_own=False, is_oob=True)


def link(server, **columns):
    return mapping_classifier.Link(server_key=server, **columns)


def reason(code, *path):
    return mapping_classifier.Reason(code, tuple(path))


@dataclass(frozen=True)
class Shape:
    name: str
    value: object
    model: str = "device"
    links: tuple = ()
    identities: tuple = ()
    preference: tuple = (False, None)
    reasons: tuple = ()
    servers: tuple = ("primary",)


CONVERTING = (
    Shape("empty dict", {}),
    Shape("blank entry null", {"primary": None}),
    Shape("blank entry text", {"primary": "  "}),
    Shape("blank entry dict", {"primary": {}}),
    Shape("blank entry id", {"primary": {"id": None}}),
    Shape("scalar id", {"primary": 5}, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape("text id", {"primary": " +05 "}, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape("largest id", {"primary": BIGINT_MAX}, links=(link("primary"),), identities=(own("primary", BIGINT_MAX),)),
    Shape("dict id", {"primary": {"id": 5}}, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape(
        "own and oob, different ids",
        {"primary": {"id": 5, "oob": {"id": 7, "type": "idrac"}}},
        links=(link("primary", oob_recorded=True, oob_type="idrac"),),
        identities=(own("primary", 5), oob("primary", 7)),
    ),
    Shape(
        "own and oob, same id",
        {"primary": {"id": 5, "oob": {"id": "5", "type": "ilo"}}},
        links=(link("primary", oob_recorded=True, oob_type="ilo"),),
        identities=(own("primary", 5, oob=True),),
    ),
    Shape(
        "oob only",
        {"primary": {"oob": {"id": 7, "type": "ilo"}}},
        links=(link("primary", oob_recorded=True, oob_type="ilo"),),
        identities=(oob("primary", 7),),
    ),
    Shape("oob type null", {"primary": {"oob": {"type": None}}}, links=(link("primary", oob_recorded=True),)),
    Shape(
        "oob metadata with a type",
        {"primary": {"oob": {"id": "", "type": "drac"}}},
        links=(link("primary", oob_recorded=True, oob_type="drac"),),
    ),
    Shape("empty oob", {"primary": {"id": 5, "oob": {}}}, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape("null oob", {"primary": {"id": 5, "oob": None}}, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape(
        "marker",
        {"primary": {"_migrated_to": MARKER}},
        links=(link("primary", migrated_to_pk=9, migrated_at="2026-01-01T00:00:00Z"),),
    ),
    Shape(
        "marker without at",
        {"primary": {"_migrated_to": {"device_id": 9, "server_key": "primary"}}},
        links=(link("primary", migrated_to_pk=9),),
    ),
    Shape(
        "marker beside metadata oob",
        {"primary": {"oob": {"type": "ilo"}, "_migrated_to": MARKER}},
        links=(link("primary", oob_recorded=True, oob_type="ilo", migrated_to_pk=9, migrated_at=MARKER["at"]),),
    ),
    Shape(
        "marker beside a live id is dropped",
        {"primary": {"id": 5, "_migrated_to": {"device_id": "bad"}}},
        links=(link("primary"),),
        identities=(own("primary", 5),),
    ),
    Shape(
        "named preference",
        {"primary": 5, "_preferred_server": "primary"},
        links=(link("primary"),),
        identities=(own("primary", 5),),
        preference=(True, "primary"),
    ),
    Shape(
        "malformed preference beside a valid id",
        {"primary": {"id": 101}, "_preferred_server": []},
        links=(link("primary"),),
        identities=(own("primary", 101),),
        preference=(True, []),
    ),
    Shape("preference only", {"_preferred_server": "primary"}, preference=(True, "primary")),
    Shape("blank preference", {"_preferred_server": " "}, preference=(True, " ")),
    Shape(
        "server key not in the settings",
        {"secondary": {"id": 5, "oob": {"id": 6, "type": "ilo"}}},
        links=(link("secondary", oob_recorded=True, oob_type="ilo"),),
        identities=(own("secondary", 5), oob("secondary", 6)),
    ),
    Shape("legacy value, one server", 5, links=(link("primary"),), identities=(own("primary", 5),)),
    Shape("legacy text value, one server", " 42 ", links=(link("primary"),), identities=(own("primary", 42),)),
    Shape("vm id", {"primary": 5}, model="vm", links=(link("primary"),), identities=(own("primary", 5),)),
    Shape(
        "vm own and oob",
        {"primary": {"id": 5, "oob": {"id": 6, "type": "oob"}}},
        model="vm",
        links=(link("primary", oob_recorded=True, oob_type="oob"),),
        identities=(own("primary", 5), oob("primary", 6)),
    ),
    Shape(
        "interface id",
        {"primary": 11},
        model="interface",
        links=(link("primary"),),
        identities=(own("primary", 11, space="port"),),
    ),
    Shape(
        "interface legacy value",
        11,
        model="interface",
        links=(link("primary"),),
        identities=(own("primary", 11, space="port"),),
    ),
    Shape(
        "vminterface id",
        {"primary": {"id": 12}},
        model="vminterface",
        links=(link("primary"),),
        identities=(own("primary", 12, space="port"),),
    ),
)

QUARANTINED = (
    Shape("legacy wide-only text", "4_2", reasons=(reason("legacy_id_unreadable"),)),
    Shape("legacy above bigint", BIGINT_MAX + 1, reasons=(reason("legacy_id_unreadable"),)),
    Shape("legacy text above bigint", str(BIGINT_MAX + 1), reasons=(reason("legacy_id_unreadable"),)),
    Shape(
        "legacy value, two servers",
        5,
        servers=("primary", "secondary"),
        reasons=(reason("legacy_several_servers_configured"),),
    ),
    Shape("bare text", "abc", reasons=(reason("invalid_value"),)),
    Shape("bare empty text", "", reasons=(reason("invalid_value"),)),
    Shape("bare zero", 0, reasons=(reason("invalid_value"),)),
    Shape("bare negative", -3, reasons=(reason("invalid_value"),)),
    Shape("bare boolean", True, reasons=(reason("invalid_value"),)),
    Shape("bare float", 4.0, reasons=(reason("invalid_value"),)),
    Shape("bare list", [5], reasons=(reason("invalid_value"),)),
    Shape("entry text", {"primary": "abc"}, reasons=(reason("invalid_id", "primary"),)),
    Shape("entry above bigint", {"primary": BIGINT_MAX + 1}, reasons=(reason("invalid_id", "primary"),)),
    Shape("entry boolean", {"primary": True}, reasons=(reason("invalid_id", "primary"),)),
    Shape("entry list", {"primary": [5]}, reasons=(reason("invalid_id", "primary"),)),
    Shape("dict id wide-only", {"primary": {"id": "4_2"}}, reasons=(reason("invalid_id", "primary", "id"),)),
    Shape(
        "oob id text",
        {"primary": {"id": 5, "oob": {"id": "x", "type": "ilo"}}},
        reasons=(reason("invalid_id", "primary", "oob", "id"),),
    ),
    Shape("oob not a dict", {"primary": {"id": 5, "oob": 7}}, reasons=(reason("invalid_oob", "primary", "oob"),)),
    Shape(
        "oob type not a string",
        {"primary": {"id": 5, "oob": {"id": 7, "type": 3}}},
        reasons=(reason("invalid_oob_type", "primary", "oob", "type"),),
    ),
    Shape(
        "unknown entry key", {"primary": {"id": 5, "note": "x"}}, reasons=(reason("unknown_key", "primary", "note"),)
    ),
    Shape(
        "unknown oob key",
        {"primary": {"oob": {"id": 7, "type": "ilo", "ip": "192.0.2.1"}}},
        reasons=(reason("unknown_key", "primary", "oob", "ip"),),
    ),
    Shape(
        "unknown marker key",
        {"primary": {"_migrated_to": {**MARKER, "note": "x"}}},
        reasons=(reason("unknown_key", "primary", "_migrated_to", "note"),),
    ),
    Shape(
        "marker not a dict",
        {"primary": {"_migrated_to": "x"}},
        reasons=(reason("invalid_marker", "primary", "_migrated_to"),),
    ),
    Shape(
        "marker names another server",
        {"primary": {"_migrated_to": {**MARKER, "server_key": "secondary"}}},
        reasons=(reason("invalid_marker", "primary", "_migrated_to"),),
    ),
    Shape(
        "marker text device id",
        {"primary": {"_migrated_to": {**MARKER, "device_id": "9"}}},
        reasons=(reason("invalid_marker", "primary", "_migrated_to"),),
    ),
    Shape(
        "marker boolean device id",
        {"primary": {"_migrated_to": {**MARKER, "device_id": True}}},
        reasons=(reason("invalid_marker", "primary", "_migrated_to"),),
    ),
    Shape(
        "marker device id above bigint",
        {"primary": {"_migrated_to": {**MARKER, "device_id": BIGINT_MAX + 1}}},
        reasons=(reason("invalid_marker", "primary", "_migrated_to"),),
    ),
    Shape("invalid server key", {"a__b": 5}, reasons=(reason("invalid_server_key", "a__b"),)),
    Shape("padded server key", {" primary": 5}, reasons=(reason("invalid_server_key", " primary"),)),
    Shape("lookup server key", {"contains": 5}, reasons=(reason("invalid_server_key", "contains"),)),
    Shape(
        "interface oob",
        {"primary": {"id": 11, "oob": {"id": 12, "type": "ilo"}}},
        model="interface",
        reasons=(reason("oob_in_port_space", "primary", "oob"),),
    ),
    Shape(
        "vminterface metadata oob",
        {"primary": {"id": 11, "oob": {"type": "ilo"}}},
        model="vminterface",
        reasons=(reason("oob_in_port_space", "primary", "oob"),),
    ),
    Shape(
        "valid entry beside a bad one",
        {"primary": 5, "secondary": "abc", "_preferred_server": "primary"},
        reasons=(reason("invalid_id", "secondary"),),
    ),
    Shape(
        "several reasons",
        {"primary": {"id": "x", "oob": 3}, "a__b": 1},
        reasons=(
            reason("invalid_server_key", "a__b"),
            reason("invalid_id", "primary", "id"),
            reason("invalid_oob", "primary", "oob"),
        ),
    ),
)

SHAPES = CONVERTING + QUARANTINED


def _make_target(model, name):
    from virtualization.models import VMInterface

    if model == "device":
        return make_device(name)
    if model == "vm":
        return make_vm(name)
    if model == "interface":
        return make_interface(make_device(f"{name}-host"), "eth0")
    if model == "vminterface":
        return VMInterface.objects.create(virtual_machine=make_vm(f"{name}-host"), name="eth0")
    raise AssertionError(model)


def _stored(model, value, name):
    return seed_stored_mapping(_make_target(model, name), copy.deepcopy(value), save=True)


def _target(obj):
    return mapping_classifier.Target(obj._meta.label_lower, obj.pk)


def _only_result(report, obj):
    (result,) = [result for result in report.objects if result.target == _target(obj)]
    return result


@pytest.mark.django_db
@pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
def test_each_stored_shape_classifies(settings, shape):
    """Each stored shape gives the rows of the design's representation table, or a quarantine with its reasons."""
    _configure(settings, *shape.servers)
    obj = _stored(shape.model, shape.value, "subject")

    report = mapping_classifier.check()

    result = _only_result(report, obj)
    assert result.original == shape.value
    assert result.reasons == tuple(sorted(shape.reasons))
    assert result.links == shape.links
    assert result.identities == shape.identities
    assert (result.preference_recorded, result.preference_value) == shape.preference
    assert report.collisions == ()
    assert mapping_classifier.exit_status(report) == (1 if shape.reasons else 0)


@pytest.mark.django_db
def test_a_null_value_and_a_missing_key_give_no_result(settings):
    """An object that stores nothing has nothing to convert or to report."""
    _configure(settings, "primary")
    stored_null = _stored("device", None, "null")
    without_key = make_device("no-key")

    report = mapping_classifier.check()

    assert {result.target for result in report.objects} & {_target(stored_null), _target(without_key)} == set()
    assert mapping_classifier.exit_status(report) == 0


@pytest.mark.django_db
def test_a_marker_that_names_a_deleted_device_converts(settings):
    """A marker stays effective when its winner is gone, so the link keeps it."""
    _configure(settings, "primary")
    winner = make_device("winner")
    winner_pk = winner.pk
    winner.delete()
    donor = _stored("device", {"primary": {"_migrated_to": {"device_id": winner_pk, "server_key": "primary"}}}, "donor")

    result = _only_result(mapping_classifier.check(), donor)

    assert result.reasons == ()
    assert result.links == (link("primary", migrated_to_pk=winner_pk),)
    assert read_mapping(donor).migrated_to("primary").device_id == winner_pk


@pytest.mark.django_db
def test_a_legacy_value_quarantines_when_another_stored_value_names_another_server(settings):
    """One configured server is not enough: no stored value may name another server key."""
    _configure(settings, "primary")
    legacy = _stored("device", 5, "legacy")
    _stored("vm", {"_preferred_server": "elsewhere"}, "names-another")

    result = _only_result(mapping_classifier.check(), legacy)

    assert result.reasons == (reason("legacy_other_server_stored"),)
    assert result.links == result.identities == ()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "other_value",
    [
        {"secondary": None},
        {"primary": {"_migrated_to": {"device_id": 3, "server_key": "secondary"}}},
        {"_preferred_server": "secondary"},
    ],
    ids=["server entry", "marker", "preference"],
)
def test_every_kind_of_stored_server_name_blocks_a_legacy_conversion(settings, other_value):
    """A server entry, a marker and a preference each name a server key."""
    _configure(settings, "primary")
    legacy = _stored("device", 5, "legacy")
    _stored("interface", other_value, "names-another")

    result = _only_result(mapping_classifier.check(), legacy)

    assert reason("legacy_other_server_stored") in result.reasons


@pytest.mark.django_db
def test_a_legacy_value_converts_when_stored_values_name_only_the_configured_server(settings):
    """A stored name of the one configured server keeps the legacy conversion."""
    _configure(settings, "primary")
    legacy = _stored("device", 5, "legacy")
    _stored("device", {"primary": 6, "_preferred_server": "primary"}, "same-server")

    result = _only_result(mapping_classifier.check(), legacy)

    assert result.reasons == ()
    assert result.identities == (own("primary", 5),)


@pytest.mark.django_db
def test_a_malformed_preference_names_no_server(settings):
    """A preference is opaque JSON: a marker shape inside it names no server and blocks no legacy conversion."""
    _configure(settings, "primary")
    legacy = _stored("device", 5, "legacy")
    preference = {"_migrated_to": {"device_id": 3, "server_key": "secondary"}}
    holder = _stored("device", {"_preferred_server": preference}, "malformed-preference")

    report = mapping_classifier.check()

    result = _only_result(report, legacy)
    assert result.reasons == ()
    assert result.identities == (own("primary", 5),)
    assert {claim.server_key for claim in result.claims} == {"primary"}
    holder_result = _only_result(report, holder)
    assert (holder_result.preference_recorded, holder_result.preference_value) == (True, preference)
    assert holder_result.links == holder_result.reasons == ()


@pytest.mark.django_db
def test_a_wide_only_legacy_id_claims_every_server_key_seen(settings):
    """The decoder reads "4_2" as 42 on every server, so the quarantined value still owns 42 on each key."""
    _configure(settings, "primary", "secondary")
    legacy = _stored("device", "4_2", "legacy")
    owner = _stored("vm", {"tertiary": {"oob": {"id": 42, "type": "ilo"}}}, "owner")

    report = mapping_classifier.check()

    assert _only_result(report, legacy).reasons == (reason("legacy_id_unreadable"),)
    assert {claim.server_key for claim in _only_result(report, legacy).claims} == {"primary", "secondary", "tertiary"}
    assert _collision_keys(report) == {("tertiary", "device", 42)}
    assert _claimants(report, ("tertiary", "device", 42)) == {(_target(legacy), "legacy"), (_target(owner), "oob")}
    assert mapping_classifier.exit_status(report) == 2


# Collisions across objects.


def _collision_key(collision):
    return (collision.server_key, collision.space, collision.identity)


def _collision_keys(report):
    return {_collision_key(collision) for collision in report.collisions}


def _claimants(report, key):
    (collision,) = [collision for collision in report.collisions if _collision_key(collision) == key]
    return {(claim.target, claim.role) for claim in collision.claims}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (("device", {"primary": 5}), ("device", {"primary": {"id": 5}}), {("primary", "device", 5)}),
        (("device", {"primary": 5}), ("vm", {"primary": 5}), {("primary", "device", 5)}),
        (("interface", {"primary": 8}), ("vminterface", {"primary": 8}), {("primary", "port", 8)}),
        (("interface", {"primary": 8}), ("interface", {"primary": "8"}), {("primary", "port", 8)}),
        (
            ("device", {"primary": 5}),
            ("device", {"primary": {"oob": {"id": 5, "type": "ilo"}}}),
            {("primary", "device", 5)},
        ),
        (
            ("vm", {"primary": {"id": 1, "oob": {"id": 5, "type": "ilo"}}}),
            ("device", {"primary": {"oob": {"id": 5, "type": "drac"}}}),
            {("primary", "device", 5)},
        ),
        (("device", 5), ("device", {"primary": 5}), {("primary", "device", 5)}),
        (("device", {"primary": 5}), ("interface", {"primary": 5}), set()),
        (("device", {"primary": 5}), ("device", {"secondary": 5}), set()),
        (("device", {"primary": {"id": 5, "oob": {"id": 5, "type": "ilo"}}}), ("device", {}), set()),
        (("device", "4_2"), ("device", {"primary": 42}), {("primary", "device", 42)}),
        (("vm", str(BIGINT_MAX + 1)), ("device", BIGINT_MAX + 1), {("primary", "device", BIGINT_MAX + 1)}),
        (("interface", "1_1"), ("vminterface", {"primary": {"id": 11}}), {("primary", "port", 11)}),
    ],
    ids=[
        "two devices",
        "device and vm",
        "interface and vminterface",
        "two interfaces",
        "host against controller",
        "controller against controller across models",
        "converted legacy against scoped",
        "device and port spaces are separate",
        "servers are separate",
        "own and oob on one object",
        "a wide-only legacy id claims the id that the decoder reads",
        "two legacy ids above bigint",
        "a wide-only legacy port id",
    ],
)
def test_collisions_across_objects(settings, first, second, expected):
    """One owner per server, space and identity: a second object on the same key is a collision."""
    _configure(settings, "primary")
    first_obj = _stored(*first, "first")
    second_obj = _stored(*second, "second")

    report = mapping_classifier.check()

    assert _collision_keys(report) == expected
    for key in expected:
        assert {target for target, _role in _claimants(report, key)} == {_target(first_obj), _target(second_obj)}
    quarantined = any(result.reasons for result in report.objects)
    assert mapping_classifier.exit_status(report) == (2 if expected else 1 if quarantined else 0)


@pytest.mark.django_db
def test_a_quarantined_object_still_claims_its_readable_ids(settings):
    """A quarantine does not hide an identity from the collision check."""
    _configure(settings, "primary")
    quarantined = _stored("device", {"primary": {"id": 5, "oob": 7}}, "quarantined")
    owner = _stored("device", {"primary": 5}, "owner")

    report = mapping_classifier.check()

    assert _collision_keys(report) == {("primary", "device", 5)}
    assert _claimants(report, ("primary", "device", 5)) == {(_target(quarantined), "own"), (_target(owner), "own")}
    assert mapping_classifier.exit_status(report) == 2


@pytest.mark.django_db
def test_an_unconvertible_legacy_id_claims_every_server_key_seen(settings):
    """The legacy id collides on a configured key and on a key that only a stored value names."""
    _configure(settings, "primary", "secondary")
    legacy = _stored("device", 5, "legacy")
    on_configured = _stored("vm", {"secondary": 5}, "on-configured")
    on_stored_only = _stored("device", {"tertiary": {"oob": {"id": 5, "type": "ilo"}}}, "on-stored-only")
    _stored("device", {"fourth": 9}, "names-fourth")

    report = mapping_classifier.check()

    assert _only_result(report, legacy).reasons == (reason("legacy_several_servers_configured"),)
    assert _collision_keys(report) == {("secondary", "device", 5), ("tertiary", "device", 5)}
    assert _claimants(report, ("secondary", "device", 5)) == {
        (_target(legacy), "legacy"),
        (_target(on_configured), "own"),
    }
    assert _claimants(report, ("tertiary", "device", 5)) == {
        (_target(legacy), "legacy"),
        (_target(on_stored_only), "oob"),
    }
    legacy_claims = {claim.server_key for claim in _only_result(report, legacy).claims}
    assert legacy_claims == {"primary", "secondary", "tertiary", "fourth"}


@pytest.mark.django_db
def test_two_unconvertible_legacy_ids_collide_with_each_other(settings):
    """Two legacy values with one id cannot both own it on any server."""
    _configure(settings, "primary", "secondary")
    first = _stored("device", 5, "first")
    second = _stored("vm", "5", "second")

    report = mapping_classifier.check()

    assert _collision_keys(report) == {("primary", "device", 5), ("secondary", "device", 5)}
    assert _claimants(report, ("primary", "device", 5)) == {(_target(first), "legacy"), (_target(second), "legacy")}


@transactional_db_with_all_apps()
def test_an_id_that_moves_during_the_check_is_no_collision(settings):
    """The check reads every model from one snapshot, so a committed move between two reads finds no collision."""
    from django.db import connection, connections

    _configure(settings, "primary")
    device = _stored("device", {"primary": 5}, "device")
    vm = make_vm("vm")
    writer = connections.create_connection("default")
    moved = []

    def move_after_the_first_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not moved and "custom_field_data" in sql:
            moved.append(True)
            with writer.cursor() as cursor:
                cursor.execute(
                    "WITH cleared AS (UPDATE dcim_device SET custom_field_data = custom_field_data - %s WHERE id = %s) "
                    "UPDATE virtualization_virtualmachine SET custom_field_data = custom_field_data || %s::jsonb "
                    "WHERE id = %s",
                    ["librenms_id", device.pk, '{"librenms_id": {"primary": 5}}', vm.pk],
                )
        return result

    try:
        with connection.execute_wrapper(move_after_the_first_read):
            report = mapping_classifier.check()
    finally:
        writer.close()

    assert moved
    assert read_mapping(type(vm).objects.get(pk=vm.pk)).server("primary").own_id == 5
    assert report.collisions == ()
    assert [result.target for result in report.objects] == [_target(device)]
    assert mapping_classifier.exit_status(report) == 0


# Configured servers.


@pytest.mark.django_db
def test_configured_servers_come_from_the_servers_section(settings):
    """The keys of the servers section are the configured servers."""
    _configure(settings, "primary", "secondary")

    assert mapping_classifier.configured_server_keys() == frozenset({"primary", "secondary"})


@pytest.mark.django_db
def test_the_single_server_form_counts_as_default(settings):
    """The legacy single-server settings name the server ``default``."""
    plugin_config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_settings = plugin_config["netbox_librenms_plugin"]
    plugin_settings.pop("servers", None)
    plugin_settings.update(librenms_url="https://librenms.example.com", api_token="token")
    settings.PLUGINS_CONFIG = plugin_config

    assert mapping_classifier.configured_server_keys() == frozenset({"default"})


@pytest.mark.django_db
@pytest.mark.parametrize("servers", [{}, ["primary"], {"a__b": {}}], ids=["empty", "not a dict", "invalid key"])
def test_an_unusable_servers_section_is_an_error(settings, servers):
    """The check refuses to guess a server from settings that the plugin itself rejects."""
    configure_librenms_servers(settings, servers)

    with pytest.raises(mapping_classifier.ClassifierError):
        mapping_classifier.configured_server_keys()


@pytest.mark.django_db
def test_settings_without_any_server_are_an_error(settings):
    """Neither form of the server settings is present."""
    plugin_config = copy.deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"].pop("servers", None)
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config

    with pytest.raises(mapping_classifier.ClassifierError):
        mapping_classifier.configured_server_keys()


# Agreement with the runtime decoder.


def _preference_state(recorded, value):
    if not recorded:
        return (PreferenceStatus.ABSENT, None)
    if isinstance(value, str) and value.strip():
        return (PreferenceStatus.NAMED, value)
    return (PreferenceStatus.MALFORMED, None)


def _classifier_meaning(result):
    servers = {}
    for row in result.links:
        own_id = next((i.identity for i in result.identities if i.server_key == row.server_key and i.is_own), None)
        oob_id = next((i.identity for i in result.identities if i.server_key == row.server_key and i.is_oob), None)
        marker = (row.migrated_to_pk, row.migrated_at) if row.migrated_to_pk is not None else None
        servers[row.server_key] = (own_id, oob_id, row.oob_type, row.oob_recorded, marker)
    container = "scoped" if isinstance(result.original, dict) else "legacy"
    return container, _preference_state(result.preference_recorded, result.preference_value), servers


def _decoder_meaning(state, *, legacy_server):
    if state.container is ContainerStatus.LEGACY:
        servers = {legacy_server: (state.legacy.queryable_id, None, None, False, None)}
        return "legacy", (state.preference.status, state.preference.server), servers
    servers = {}
    for entry in state.servers:
        effective = entry.migration.effective
        marker = (effective.device_id, effective.at) if effective is not None else None
        if entry.own_id is None and entry.oob_id is None and not entry.oob_recorded and marker is None:
            continue
        servers[entry.server] = (entry.own_id, entry.oob_id, entry.oob_type, entry.oob_recorded, marker)
    return state.container.value, (state.preference.status, state.preference.server), servers


def _without(value, path):
    stripped = copy.deepcopy(value)
    node = stripped
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return stripped


def _decode(model_obj, value):
    return read_mapping(seed_stored_mapping(type(model_obj)(), copy.deepcopy(value)))


def _decoder_shows(found, state, raw, obj):
    """Return whether the decoder shows the problem that *found* names at its path."""
    entry = state.server(found.path[0]) if found.path else None
    shows = {
        "invalid_value": lambda: state.container is ContainerStatus.INVALID,
        "legacy_id_unreadable": lambda: state.container is ContainerStatus.LEGACY and state.legacy.queryable_id is None,
        "legacy_several_servers_configured": lambda: state.legacy.queryable_id is not None,
        "legacy_other_server_stored": lambda: state.legacy.queryable_id is not None,
        "invalid_server_key": lambda: entry is None,
        "invalid_id": lambda: (entry.oob_id if found.path[1:] == ("oob", "id") else entry.own_id) is None,
        "invalid_oob": lambda: entry.oob_id is None and not entry.oob_recorded,
        "invalid_oob_type": lambda: not isinstance(entry.oob_type, (str, type(None))),
        "unknown_key": lambda: _decode(obj, _without(raw, found.path)).server(found.path[0]) == entry,
        # The decoder has no upper bound on the winner pk, and the bigint column has.
        "invalid_marker": lambda: (
            entry.display_id is None
            and (entry.migration.effective is None or entry.migration.effective.device_id > BIGINT_MAX)
        ),
        "oob_in_port_space": lambda: entry.oob_recorded and _target(obj).model in mapping_classifier.PORT_MODELS,
    }
    return shows[found.code]()


def _decoder_claims(state, configured):
    if state.container is ContainerStatus.LEGACY:
        legacy_id = state.legacy.readable_id
        return {(server, legacy_id) for server in configured if legacy_id is not None}
    return {
        (entry.server, identity)
        for entry in state.servers
        for identity in (entry.own_id, entry.oob_id)
        if identity is not None
    }


@pytest.mark.django_db
@pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
def test_classifier_and_decoder_agree_on_each_shape(settings, shape):
    """The classifier reads each stored shape as the runtime decoder does."""
    # The runtime decoder repeats these rules until the cutover deletes it, and this test with it.
    _configure(settings, *shape.servers)
    obj = _stored(shape.model, shape.value, "subject")
    result = _only_result(mapping_classifier.check(), obj)
    state = read_mapping(type(obj).objects.get(pk=obj.pk))

    if result.reasons:
        assert all(_decoder_shows(found, state, shape.value, obj) for found in result.reasons), result.reasons
    else:
        assert _classifier_meaning(result) == _decoder_meaning(state, legacy_server=shape.servers[0])
    assert {(claim.server_key, claim.identity) for claim in result.claims} == _decoder_claims(state, shape.servers)


@pytest.mark.parametrize(
    "value",
    [
        5,
        "5",
        " 5 ",
        "+5",
        "005",
        "4_2",
        "-5",
        "0",
        0,
        -1,
        True,
        False,
        5.0,
        None,
        [],
        {},
        "٥",
        "5\n",
        " 5",
        BIGINT_MAX,
        BIGINT_MAX + 1,
        str(BIGINT_MAX),
        str(BIGINT_MAX + 1),
        "0" * 18 + "5",
        "0" * 19 + "5",
        "",
    ],
)
def test_strict_id_agrees_with_the_runtime_id_rule(value):
    """The classifier's id rule is the runtime's coerce_librenms_id, which it cannot import."""
    assert mapping_classifier.strict_id(value) == coerce_librenms_id(value)


def test_the_classifier_imports_only_the_standard_library_and_django():
    """The check script runs on an old plugin version, so it imports only the standard library and Django."""
    tree = ast.parse(Path(mapping_classifier.__file__).read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        "." * node.level + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert {name.split(".")[0] for name in imported} - sys.stdlib_module_names - {"django"} == set()


# The check script entry point.


@pytest.mark.django_db
def test_the_entry_point_reports_each_finding_and_its_exit_status(settings, capsys):
    """The report names each quarantined object with its reason, and each collision with its owners."""
    _configure(settings, "primary")
    quarantined = _stored("device", {"primary": {"id": "abc"}}, "quarantined")
    first = _stored("device", {"primary": 5}, "first")
    second = _stored("vm", {"primary": {"oob": {"id": 5, "type": "ilo"}}}, "second")

    assert mapping_classifier.main() == 2

    lines = capsys.readouterr().out.splitlines()
    assert f'QUARANTINE dcim.device pk={quarantined.pk} reason=invalid_id path=$["primary"]["id"]' in "\n".join(lines)
    (collision,) = [line for line in lines if line.startswith("COLLISION ")]
    assert "server=primary space=device identity=5" in collision
    assert f"dcim.device pk={first.pk} own" in collision
    assert f"virtualization.virtualmachine pk={second.pk} oob" in collision


@pytest.mark.django_db
def test_the_entry_point_reports_an_error_with_status_3(settings, capsys):
    """An error is reported, not hidden behind a clean status."""
    configure_librenms_servers(settings, {})

    assert mapping_classifier.main() == 3

    assert "ERROR" in capsys.readouterr().err


def _manage_py():
    import netbox

    manage = Path(netbox.__file__).resolve().parents[1] / "manage.py"
    assert manage.is_file(), manage
    return manage


def _nbshell_environment(tmp_path, *, database, servers, **extra_environment):
    (tmp_path / "classifier_check_settings.py").write_text(
        "from netbox_librenms_plugin.tests.isolated_settings import *  # noqa: F403\n"
        f"DATABASES['default']['NAME'] = {database!r}\n"
        f"PLUGINS_CONFIG['netbox_librenms_plugin']['servers'] = {_server_settings(*servers)!r}\n",
        encoding="utf-8",
    )
    return {
        **os.environ,
        **extra_environment,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), *(path for path in sys.path if path)]),
        "DJANGO_SETTINGS_MODULE": "classifier_check_settings",
    }


def _run_nbshell(tmp_path, *, database, servers, stdout=subprocess.PIPE, **extra_environment):
    environment = _nbshell_environment(tmp_path, database=database, servers=servers, **extra_environment)
    with Path(mapping_classifier.__file__).open() as script:
        return subprocess.run(
            [sys.executable, str(_manage_py()), "nbshell"],
            stdin=script,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            timeout=120,
            check=False,
        )


@transactional_db_with_all_apps()
def test_the_check_script_runs_through_nbshell(tmp_path):
    """``manage.py nbshell < mapping_classifier.py`` prints each finding and exits with its status."""
    from django.db import connection

    database = connection.settings_dict["NAME"]
    clean = _stored("device", {"primary": 5}, "clean")

    result = _run_nbshell(tmp_path, database=database, servers=("primary",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUARANTINE" not in result.stdout and "COLLISION" not in result.stdout

    quarantined = _stored("interface", {"primary": {"id": 3, "oob": {"id": 4, "type": "ilo"}}}, "quarantined")
    result = _run_nbshell(tmp_path, database=database, servers=("primary",))
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        f'QUARANTINE dcim.interface pk={quarantined.pk} reason=oob_in_port_space path=$["primary"]["oob"]'
        in result.stdout
    )

    _stored("vm", 5, "legacy")
    result = _run_nbshell(tmp_path, database=database, servers=("primary", "secondary"))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "reason=legacy_several_servers_configured" in result.stdout
    (collision,) = [line for line in result.stdout.splitlines() if "COLLISION " in line]
    assert "server=primary space=device identity=5" in collision
    assert f"dcim.device pk={clean.pk} own" in collision

    result = _run_nbshell(tmp_path, database=f"{database}_absent", servers=("primary",))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "ERROR" in result.stderr


@transactional_db_with_all_apps()
def test_a_report_that_cannot_be_written_exits_with_status_3_through_nbshell(tmp_path):
    """An output error must not end the console run with a clean status."""
    from django.db import connection

    _stored("device", {"café": 5}, "device")
    _stored("vm", {"café": 5}, "vm")

    result = _run_nbshell(
        tmp_path, database=connection.settings_dict["NAME"], servers=("café",), PYTHONIOENCODING="ascii:strict"
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "ERROR" in result.stderr


@transactional_db_with_all_apps()
def test_a_report_that_cannot_be_flushed_exits_with_status_3_through_nbshell(tmp_path):
    """A failed flush of the buffered report must not fail again at exit and change the status."""
    from django.db import connection

    _stored("device", {"primary": 5}, "device")

    with Path("/dev/full").open("w") as full:
        result = _run_nbshell(tmp_path, database=connection.settings_dict["NAME"], servers=("primary",), stdout=full)

    assert result.returncode == 3, result.stderr
    assert "ERROR" in result.stderr


def test_an_error_that_cannot_be_written_exits_with_status_3_through_nbshell(tmp_path):
    """A failed write of the error message must not end the console run with another status."""
    environment = _nbshell_environment(tmp_path, database="mapping_check_absent", servers=("primary",))
    process = subprocess.Popen(
        [sys.executable, str(_manage_py()), "nbshell"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
    )
    try:
        # The shell writes its banner to stderr, so stderr closes only after the shell has started.
        process.stdin.write("__import__('sys').stderr.write('STARTED\\n')\n")
        process.stdin.flush()
        for line in process.stderr:
            if line == "STARTED\n":
                break
        process.stderr.close()
        process.stdin.write(Path(mapping_classifier.__file__).read_text())
        process.stdin.close()
        output = process.stdout.read()
        status = process.wait(timeout=120)
    finally:
        process.kill()

    assert status == 3, output
