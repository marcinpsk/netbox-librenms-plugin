"""
Test fixture helpers for the stored object server mapping.

Tests seed and inspect the stored mapping only through these helpers. This is the one test module
that reaches the private storage adapter of ``server_mappings``.
"""

import copy
import json
from contextlib import contextmanager

from netbox_librenms_plugin.server_mappings import (
    PREFERRED_SERVER_FIELD,
    ChangeOutcome,
    ContainerStatus,
    PreferenceStatus,
    assign_own,
    attach_oob,
    mark_migrated,
    read_mapping,
    release_server,
    set_preference,
)

# The reviewed test seam to the raw storage adapter.
# nosemgrep: no-stored-mapping-access-private  # noqa: ERA001
from netbox_librenms_plugin.server_mappings import (
    _MAPPING_KEY,
    _STORAGE_FIELD,
    _STORAGE_FIELDS,
    _put_on,
    _put_stored_value,
    _stored_value,
)

_SEEDABLE_OUTCOMES = frozenset({ChangeOutcome.APPLIED, ChangeOutcome.UNCHANGED})


def _save(obj, fields):
    if obj.pk is None:
        obj.save()
    else:
        obj.save(update_fields=sorted(fields))


def _seed(obj, change):
    if change.outcome not in _SEEDABLE_OUTCOMES:
        raise ValueError(f"The builder skipped the seed: {change.outcome}.")
    _put_on(obj, change)


def apply_mapping_change(obj, change):
    """Put a built mapping change on *obj*, with no claim, no owner check and no save; return *obj*."""
    _put_on(obj, change)
    return obj


def seed_mapping(
    obj, server="default", *, own=None, oob=None, oob_type="oob", migrated_to=None, preferred=False, save=True
):
    """
    Seed the mapping of *obj* on *server* through the real builders; return *obj*.

    *own* is the own LibreNMS ID, *oob* the OOB controller ID with *oob_type*, *migrated_to* the winner
    pk of a migration marker that replaces the links on *server*, and *preferred* stores *server* as the
    preferred server. The other servers of *obj* stay. A builder that skips the write fails the setup.
    With *save*, save *obj*.
    """
    if own is None and oob is None and migrated_to is None and not preferred:
        raise ValueError("seed_mapping() needs own, oob, migrated_to or preferred.")
    if migrated_to is not None and (own is not None or oob is not None):
        raise ValueError("A migration marker replaces the own and OOB IDs on its server.")
    if own is not None:
        _seed(obj, assign_own(obj, server, own))
    if oob is not None:
        _seed(obj, attach_oob(obj, server, oob, oob_type=oob_type))
    if migrated_to is not None:
        _seed(obj, mark_migrated(obj, migrated_to, server))
    if preferred:
        # A preference with no server mapping is a state of its own.
        if _stored_value(obj) is None:
            _put_stored_value(obj, {})
        _seed(obj, set_preference(obj, server))
    if save:
        # An unchanged build writes no field, but the object can hold an unsaved seed.
        _save(obj, _STORAGE_FIELDS)
    return obj


def unmapped_only_constraint():
    """Return an object permission constraint that admits only objects without a stored mapping, as an operator can write one."""
    return [{f"{_STORAGE_FIELD}__{_MAPPING_KEY}__isnull": True}, {f"{_STORAGE_FIELD}__{_MAPPING_KEY}": None}]


def mapped_on_server_constraint(server):
    """Return an object permission constraint that admits only objects with an entry on *server*, as an operator can write one."""
    return {f"{_STORAGE_FIELD}__{_MAPPING_KEY}__{server}__isnull": False}


def seed_legacy(obj, legacy_id, *, save=True):
    """Seed the bare legacy value *legacy_id* on *obj*, which resolves on every server; return *obj*."""
    fields = _put_stored_value(obj, legacy_id)
    if read_mapping(obj).container is not ContainerStatus.LEGACY:
        raise AssertionError("The legacy seed must read as a legacy mapping.")
    if save:
        _save(obj, fields)
    return obj


def seed_malformed_preference(obj, *, save=True):
    """Seed a stored preference that names no server, as a hand edit does; the server mappings stay. Return *obj*."""
    stored = copy.deepcopy(_stored_value(obj))
    stored = {} if stored is None else stored
    if not isinstance(stored, dict):
        raise ValueError(f"Cannot store a preference on a {read_mapping(obj).container} mapping.")
    stored[PREFERRED_SERVER_FIELD] = 123
    fields = _put_stored_value(obj, stored)
    if save:
        _save(obj, fields)
    return obj


def seed_unmapped(obj, server="default", *, save=True):
    """Remove the mapping of *obj* on *server* and any preference through the real builder; return *obj*."""
    _seed(obj, release_server(obj, server, clear_preference=True))
    if save:
        _save(obj, _STORAGE_FIELDS)
    return obj


def seed_quarantined(obj, *, save=True):
    """
    Seed a mapping on *obj* that no reader can use, as a hand-edited or corrupt value; return *obj*.

    Every path that refuses an invalid mapping refuses it. With *save*, save *obj*.
    """
    fields = _put_stored_value(obj, "not-a-mapping")
    if read_mapping(obj).container is not ContainerStatus.INVALID:
        raise AssertionError("The quarantine seed must read as an invalid mapping.")
    if save:
        _save(obj, fields)
    return obj


def seed_mapping_row(obj, source, *, connection=None):
    """
    Write the mapping of *source* to the database row of *obj* only, as a concurrent writer does.

    *source* is an unsaved object of the same model, seeded with ``seed_mapping(..., save=False)``;
    a bare one writes no mapping. *obj* keeps the mapping that it loaded. The write sends no signal
    and records no change. The other custom fields of the row stay. With *connection*, a second
    database connection, the write runs in its open transaction and the caller commits or holds
    it. Returns *obj*.
    """
    if type(source) is not type(obj) or source.pk is not None:
        raise ValueError("The source of a row write is an unsaved object of the same model.")
    if connection is None:
        return seed_stored_mapping_row(obj, _stored_value(source))
    table = obj._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET "{_STORAGE_FIELD}" = '
            f"""jsonb_set(COALESCE("{_STORAGE_FIELD}", '{{}}'::jsonb), %s::text[], %s::jsonb) WHERE id = %s""",
            [[_MAPPING_KEY], json.dumps(_stored_value(source)), obj.pk],
        )
        if cursor.rowcount != 1:
            raise ValueError(f"{type(obj).__name__} {obj.pk} is not visible to the connection.")
    return obj


@contextmanager
def mapping_in_memory(obj, server="default", **seed):
    """Seed the mapping of *obj* with ``seed_mapping(**seed)`` and no save for the block, then put the old one back."""
    previous = _stored_value(obj)
    seed_mapping(obj, server, save=False, **seed)
    try:
        yield obj
    finally:
        _put_stored_value(obj, previous)


def seed_stored_mapping(obj, value, *, save=False):
    """
    Store a copy of the raw mapping *value* on *obj*, only for a legacy runtime or a storage shape test.

    ``test_mapping_fixture_scope.py`` pins the raw uses that each file may keep. With *save*, save
    *obj*. Returns *obj*.
    """
    fields = _put_stored_value(obj, value)
    if save:
        _save(obj, fields)
    return obj


def seed_stored_mapping_row(obj, value):
    """
    Write the raw mapping *value* to the database row of *obj* only, as a concurrent writer does.

    Only for a legacy runtime or a storage shape test; else use :func:`seed_mapping_row`. *obj* keeps the value that it loaded. The write sends no signal and records no change. The other
    custom fields of the row stay. Returns *obj*.
    """
    row = type(obj).objects.get(pk=obj.pk)
    fields = _put_stored_value(row, value)
    type(obj).objects.filter(pk=obj.pk).update(**{field: getattr(row, field) for field in fields})
    return obj


def mapping_summary(obj) -> dict:
    """
    Return the mapping of *obj* in terms that do not depend on the storage, to compare in an assertion.

    Each stored server entry maps to its set fields: ``own``, ``oob``, ``oob_type``,
    ``oob_recorded`` (an OOB entry with no ID) and ``migrated_to`` (the winner pk of an effective
    marker), or to ``{}`` when it has no readable field. A named preference is
    ``"preferred": server``, a malformed one ``PreferenceStatus.MALFORMED``. An unusable mapping is
    ``{"quarantined": True}``, and no mapping is ``{}``.

    Raises:
        ValueError: *obj* holds a legacy value; only a legacy runtime test reads one, with
            :func:`stored_mapping_for_test`.

    """
    mapping = read_mapping(obj)
    if mapping.container is ContainerStatus.LEGACY:
        raise ValueError("A legacy mapping has no server-scoped summary.")
    if mapping.container is ContainerStatus.INVALID:
        return {"quarantined": True}
    summary = {}
    for entry in mapping.servers:
        if entry.server in ("preferred", "quarantined"):
            raise ValueError(f"The server key {entry.server!r} collides with a summary key.")
        fields = {
            "own": entry.own_id,
            "oob": entry.oob_id,
            "oob_type": entry.oob_type if entry.oob_recorded else None,
            "oob_recorded": entry.oob_recorded and entry.oob_id is None or None,
            "migrated_to": entry.migration.effective.device_id if entry.migration.effective else None,
        }
        summary[entry.server] = {key: value for key, value in fields.items() if value is not None}
    if mapping.preference.status is PreferenceStatus.NAMED:
        summary["preferred"] = mapping.preference.server
    elif mapping.preference.status is PreferenceStatus.MALFORMED:
        summary["preferred"] = PreferenceStatus.MALFORMED
    return summary


def stored_mapping_for_test(obj):
    """Return a copy of the raw mapping that *obj* stores, or None; only for a legacy runtime or a storage shape test."""
    return copy.deepcopy(_stored_value(obj))


def mapping_from_change_record(change, *, before):
    """
    Return the raw stored mapping in the before-state (*before*) or the after-state of an ObjectChange.

    Raises:
        ValueError: The record has no data for that side, for example the before-state of a create.

    """
    data = change.prechange_data if before else change.postchange_data
    if data is None:
        raise ValueError(f"The change record has no {'before' if before else 'after'}-state.")
    return data["custom_fields"].get(_MAPPING_KEY)


def custom_fields_without_mapping(custom_fields):
    """Return a copy of a custom field container without the stored mapping, to compare the other fields."""
    rest = copy.deepcopy(custom_fields)
    rest.pop(_MAPPING_KEY, None)
    return rest
