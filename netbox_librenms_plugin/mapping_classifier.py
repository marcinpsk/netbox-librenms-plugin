"""Classify the stored LibreNMS mappings for their move to plugin tables; ``nbshell < mapping_classifier.py`` checks them."""

import json
import re
import sys
import traceback
from dataclasses import dataclass, field

from django.apps import apps as django_apps
from django.conf import settings
from django.db.models import JSONField

MODULE_NAME = "netbox_librenms_plugin.mapping_classifier"

if __name__ != MODULE_NAME:
    # nbshell runs piped stdin in an interactive console: end its prompt line, and drop the later prompts.
    sys.ps1 = sys.ps2 = ""
    print()

PLUGIN_NAME = "netbox_librenms_plugin"
MAPPING_KEY = "librenms_id"
PREFERRED_SERVER_FIELD = "_preferred_server"
MIGRATED_TO_FIELD = "_migrated_to"
LEGACY_SERVER_KEY = "default"
SPACE_DEVICE = "device"
SPACE_PORT = "port"
DEVICE_MODELS = ("dcim.device", "virtualization.virtualmachine")
PORT_MODELS = ("dcim.interface", "virtualization.vminterface")
MODEL_SPACES = {**dict.fromkeys(DEVICE_MODELS, SPACE_DEVICE), **dict.fromkeys(PORT_MODELS, SPACE_PORT)}
ROLE_OWN = "own"
ROLE_OOB = "oob"
ROLE_LEGACY = "legacy"
ENTRY_KEYS = frozenset({"id", "oob", MIGRATED_TO_FIELD})
OOB_KEYS = frozenset({"id", "type"})
MARKER_KEYS = frozenset({"device_id", "server_key", "at"})
EXIT_CLEAN = 0
EXIT_QUARANTINE = 1
EXIT_COLLISION = 2
EXIT_ERROR = 3

# The same rule as librenms_ids.coerce_librenms_id, which an old plugin version may not have.
_ID_MAX = 2**63 - 1
_ID_TEXT_RE = re.compile(r"^[ \t\r\n\f\v]*\+?[0-9]{1,19}[ \t\r\n\f\v]*$")
_JSON_LOOKUP_NAMES = frozenset(JSONField.get_lookups())


class ClassifierError(Exception):
    """The settings do not allow a classification."""


# nbshell reads this file line by line, and a blank line ends a block: no blank line in a function or a class,
# and no class docstring, because ruff format puts a blank line after one.


# One object that stores a mapping value: its model label and primary key.
@dataclass(frozen=True, order=True)
class Target:
    model: str
    pk: int


# One MappingIdentity row: an identity that the object holds on one server.
@dataclass(frozen=True, order=True)
class Identity:
    server_key: str
    space: str
    identity: int
    is_own: bool
    is_oob: bool


# One MappingLink row: the state of the object on one server, apart from its identities.
@dataclass(frozen=True)
class Link:
    server_key: str
    oob_recorded: bool = False
    oob_type: str | None = None
    migrated_to_pk: int | None = None
    migrated_at: object = None


# Why a stored value does not convert: a reason code and the JSON path of the part that it names.
@dataclass(frozen=True, order=True)
class Reason:
    code: str
    path: tuple = ()


# An id that an object holds under the strict id rule, converted or not: the collision check reads it.
@dataclass(frozen=True)
class Claim:
    target: Target
    server_key: str
    space: str
    identity: int
    role: str


# The classification of one object. A quarantined object has reasons, and no preference, links or identities.
@dataclass(frozen=True)
class ObjectResult:
    target: Target
    original: object
    preference_recorded: bool
    preference_value: object
    links: tuple
    identities: tuple
    reasons: tuple
    claims: tuple


# More than one object holds one identity on one server.
@dataclass(frozen=True)
class Collision:
    server_key: str
    space: str
    identity: int
    claims: tuple


@dataclass(frozen=True)
class Report:
    configured_servers: frozenset
    objects: tuple
    collisions: tuple


@dataclass
class _Found:
    preference_recorded: bool = False
    preference_value: object = None
    links: list = field(default_factory=list)
    identities: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    claims: list = field(default_factory=list)


def strict_id(value) -> int | None:
    """Return the LibreNMS id that the plugin's ``coerce_librenms_id`` reads from *value*, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not _ID_TEXT_RE.fullmatch(value):
            return None
        value = int(value)
    if isinstance(value, int) and 0 < value <= _ID_MAX:
        return value
    return None


def _wide_legacy_id(value) -> int | None:
    """Return the positive id that the runtime's legacy rule, ``int()`` of an int or a text, reads, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        identity = int(value)
    except ValueError:
        return None
    return identity if identity > 0 else None


def is_server_key(value) -> bool:
    """Return whether the plugin accepts *value* as a server key."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "__" not in value
        and value not in _JSON_LOOKUP_NAMES
        and value != PREFERRED_SERVER_FIELD
    )


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def configured_server_keys() -> frozenset:
    """
    Return the configured LibreNMS server keys, from the settings, as the plugin reads them.

    A ``servers`` section names the servers. Without it, the single-server form is the server ``default``.

    Raises:
        ClassifierError: The settings configure no server, or a server key that the plugin rejects.

    """
    plugin_settings = getattr(settings, "PLUGINS_CONFIG", {}).get(PLUGIN_NAME, {})
    if "servers" not in plugin_settings:
        if plugin_settings.get("librenms_url"):
            return frozenset({LEGACY_SERVER_KEY})
        raise ClassifierError("The plugin settings configure no LibreNMS server.")
    servers = plugin_settings["servers"]
    if not isinstance(servers, dict) or not servers:
        raise ClassifierError("The 'servers' section of the plugin settings is empty or is not a dictionary.")
    invalid = sorted(repr(key) for key in servers if not is_server_key(key))
    if invalid:
        raise ClassifierError(f"The plugin settings name server keys that the plugin rejects: {', '.join(invalid)}.")
    return frozenset(servers)


def load_stored_values(apps=None) -> list:
    """
    Return ``(target, value)`` for each Device, VM, Interface and VMInterface that stores a mapping value.

    *apps* is the app registry, so that a data migration can give its historical models.
    """
    apps = django_apps if apps is None else apps
    stored = []
    for label in (*DEVICE_MODELS, *PORT_MODELS):
        rows = (
            apps.get_model(label)
            ._default_manager.filter(custom_field_data__has_key=MAPPING_KEY)
            .order_by("pk")
            .values_list("pk", "custom_field_data")
        )
        for pk, data in rows.iterator():
            if data[MAPPING_KEY] is not None:
                stored.append((Target(label, pk), data[MAPPING_KEY]))
    return stored


def _stored_server_names(values) -> frozenset:
    """Return the server keys that the stored values name, in a server entry, a preference or a marker."""
    names = []
    for value in values:
        if not isinstance(value, dict):
            continue
        names.append(value.get(PREFERRED_SERVER_FIELD))
        for key, entry in value.items():
            # A preference is opaque JSON: only its own text names a server.
            if key == PREFERRED_SERVER_FIELD:
                continue
            names.append(key)
            marker = entry.get(MIGRATED_TO_FIELD) if isinstance(entry, dict) else None
            if isinstance(marker, dict):
                names.append(marker.get("server_key"))
    # A stored name can be any JSON value, so the check runs before the set.
    return frozenset(name for name in names if is_server_key(name))


def _legacy_refusal(configured_servers, names) -> Reason | None:
    """Return why no legacy value converts, or None when each one converts to the one configured server."""
    if len(configured_servers) != 1:
        return Reason("legacy_several_servers_configured")
    if names - configured_servers:
        return Reason("legacy_other_server_stored")
    return None


def _read_id(raw, path, found) -> int | None:
    """Return the strict id in *raw*. A blank value is no id, and any other unreadable value is a reason."""
    identity = strict_id(raw)
    if identity is None and not _is_blank(raw):
        found.reasons.append(Reason("invalid_id", path))
    return identity


def _read_unknown_keys(raw, known, path, found) -> None:
    found.reasons.extend(Reason("unknown_key", (*path, key)) for key in sorted(set(raw) - known))


def _read_oob(raw, path, space, found):
    """Return ``(recorded, id, type)`` of an OOB entry. A null entry is no OOB entry."""
    if raw is None:
        return False, None, None
    if not isinstance(raw, dict):
        found.reasons.append(Reason("invalid_oob", path))
        return False, None, None
    _read_unknown_keys(raw, OOB_KEYS, path, found)
    oob_id = _read_id(raw.get("id"), (*path, "id"), found)
    oob_type = raw.get("type")
    if oob_type is not None and not isinstance(oob_type, str):
        found.reasons.append(Reason("invalid_oob_type", (*path, "type")))
        oob_type = None
    if raw and space == SPACE_PORT:
        found.reasons.append(Reason("oob_in_port_space", path))
    return bool(raw), oob_id, oob_type


def _read_marker(raw, server_key, path, found):
    """Return ``(winner pk, at)`` of a migration marker, or None. A null marker is no marker."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        found.reasons.append(Reason("invalid_marker", path))
        return None
    _read_unknown_keys(raw, MARKER_KEYS, path, found)
    winner_pk = raw.get("device_id")
    valid_pk = isinstance(winner_pk, int) and not isinstance(winner_pk, bool) and 0 < winner_pk <= _ID_MAX
    if raw.get("server_key") != server_key or not valid_pk:
        found.reasons.append(Reason("invalid_marker", path))
        return None
    return winner_pk, raw.get("at")


def _add_server_state(target, space, server_key, state, found) -> None:
    """Add the claims, the link and the identities of one server entry."""
    own_id, oob_id, oob_recorded, oob_type, marker = state
    for identity, role in ((own_id, ROLE_OWN), (oob_id, ROLE_OOB)):
        if identity is not None:
            found.claims.append(Claim(target, server_key, space, identity, role))
    if own_id is None and oob_id is None and not oob_recorded and marker is None:
        return
    winner_pk, migrated_at = marker or (None, None)
    found.links.append(Link(server_key, oob_recorded, oob_type, winner_pk, migrated_at))
    if own_id is not None and own_id == oob_id:
        found.identities.append(Identity(server_key, space, own_id, is_own=True, is_oob=True))
        return
    if own_id is not None:
        found.identities.append(Identity(server_key, space, own_id, is_own=True, is_oob=False))
    if oob_id is not None:
        found.identities.append(Identity(server_key, space, oob_id, is_own=False, is_oob=True))


def _read_entry(target, space, server_key, entry, found) -> None:
    """Read one server entry: a bare id, or a dict with an own id, an OOB entry and a migration marker."""
    path = (server_key,)
    if not isinstance(entry, dict):
        own_id = _read_id(entry, path, found)
        _add_server_state(target, space, server_key, (own_id, None, False, None, None), found)
        return
    _read_unknown_keys(entry, ENTRY_KEYS, path, found)
    own_id = _read_id(entry.get("id"), (*path, "id"), found)
    oob_recorded, oob_id, oob_type = _read_oob(entry.get("oob"), (*path, "oob"), space, found)
    marker = None
    # A marker beside a live id has no effect, so it is dropped.
    if own_id is None and oob_id is None:
        marker = _read_marker(entry.get(MIGRATED_TO_FIELD), server_key, (*path, MIGRATED_TO_FIELD), found)
    _add_server_state(target, space, server_key, (own_id, oob_id, oob_recorded, oob_type, marker), found)


def _read_scoped(target, space, value, found) -> None:
    for key, entry in value.items():
        if key == PREFERRED_SERVER_FIELD:
            found.preference_recorded = True
            found.preference_value = entry
        elif is_server_key(key):
            _read_entry(target, space, key, entry, found)
        else:
            found.reasons.append(Reason("invalid_server_key", (key,)))


def _read_legacy(target, space, value, legacy, found) -> None:
    """Read a bare legacy value. It converts only to the one configured server, and else claims every key seen."""
    refusal, claim_keys = legacy
    identity = strict_id(value)
    readable_id = _wide_legacy_id(value)
    if readable_id is None:
        found.reasons.append(Reason("invalid_value"))
        return
    if identity is None:
        refusal = Reason("legacy_id_unreadable")
    if refusal is not None:
        found.reasons.append(refusal)
        # The runtime reads a legacy id with the wide rule, on every server.
        found.claims.extend(Claim(target, key, space, readable_id, ROLE_LEGACY) for key in claim_keys)
        return
    (server_key,) = claim_keys
    _add_server_state(target, space, server_key, (identity, None, False, None, None), found)


def _classify_object(target, value, legacy) -> ObjectResult:
    """Classify one stored value, all or nothing: an object with a reason converts nothing."""
    space = MODEL_SPACES[target.model]
    found = _Found()
    if isinstance(value, dict):
        _read_scoped(target, space, value, found)
    else:
        _read_legacy(target, space, value, legacy, found)
    quarantined = bool(found.reasons)
    return ObjectResult(
        target=target,
        original=value,
        preference_recorded=found.preference_recorded and not quarantined,
        preference_value=None if quarantined else found.preference_value,
        links=() if quarantined else tuple(sorted(found.links, key=lambda row: row.server_key)),
        identities=() if quarantined else tuple(sorted(found.identities)),
        reasons=tuple(sorted(found.reasons)),
        claims=tuple(found.claims),
    )


def _collisions(objects) -> tuple:
    claims_by_key = {}
    for result in objects:
        for claim in result.claims:
            claims_by_key.setdefault((claim.server_key, claim.space, claim.identity), []).append(claim)
    return tuple(
        Collision(*key, claims=tuple(claims))
        for key, claims in sorted(claims_by_key.items())
        if len({claim.target for claim in claims}) > 1
    )


def classify(stored, configured_servers) -> Report:
    """
    Classify each ``(target, value)`` pair in *stored*, and find the collisions between the objects.

    A legacy bare value converts only when exactly one server is configured and no stored value names another
    server key. An unconverted legacy id counts as claimed on every server key that is configured or stored.

    Raises:
        ClassifierError: No server is configured.

    """
    stored = tuple(stored)
    configured_servers = frozenset(configured_servers)
    if not configured_servers:
        raise ClassifierError("No LibreNMS server is configured.")
    names = _stored_server_names(value for _target, value in stored)
    refusal = _legacy_refusal(configured_servers, names)
    legacy = (refusal, tuple(sorted(configured_servers | names)))
    objects = tuple(_classify_object(target, value, legacy) for target, value in stored)
    return Report(configured_servers=configured_servers, objects=objects, collisions=_collisions(objects))


def check(apps=None) -> Report:
    """Classify every stored mapping value in the database against the configured servers."""
    return classify(load_stored_values(apps), configured_server_keys())


def exit_status(report) -> int:
    """Return the check script's exit status for *report*: a collision wins over a quarantine finding."""
    if report.collisions:
        return EXIT_COLLISION
    if any(result.reasons for result in report.objects):
        return EXIT_QUARANTINE
    return EXIT_CLEAN


def _object_name(target) -> str:
    return f"{target.model} pk={target.pk}"


def _path_text(path) -> str:
    return "$" + "".join(f"[{json.dumps(key)}]" for key in path)


def report_lines(report) -> list:
    """Return one line for each reason that an object does not convert, one for each collision, and a summary."""
    lines = [
        f"QUARANTINE {_object_name(result.target)} reason={found.code} path={_path_text(found.path)} "
        f"value={json.dumps(result.original)}"
        for result in report.objects
        for found in result.reasons
    ]
    for collision in report.collisions:
        owners = ", ".join(f"{_object_name(claim.target)} {claim.role}" for claim in collision.claims)
        lines.append(
            f"COLLISION server={collision.server_key} space={collision.space} identity={collision.identity} "
            f"owners={owners}"
        )
    quarantined = sum(1 for result in report.objects if result.reasons)
    lines.append(
        f"Checked {len(report.objects)} stored mapping values. Not convertible: {quarantined}. "
        f"Collisions: {len(report.collisions)}."
    )
    return lines


def main() -> int:
    """Print the check report and return the exit status. An error prints to stderr and returns 3."""
    try:
        report = check()
        # An output error must give status 3 here: the nbshell console would print it and exit 0.
        sys.stdout.write("\n".join(report_lines(report)) + "\n")
        sys.stdout.flush()
    except ClassifierError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:  # A failed check must never look like a clean one.
        print("ERROR: the mapping check failed.", file=sys.stderr)
        traceback.print_exc()
        return EXIT_ERROR
    return exit_status(report)


def _run_unless_imported(name) -> None:
    """Run the check and exit with its status, unless the module was imported under its own name."""
    if name != MODULE_NAME:
        sys.exit(main())


_run_unless_imported(__name__)
