"""Request-level coverage for the sync page form that creates and edits an object server mapping."""

from collections import defaultdict
from contextlib import ExitStack
from itertools import count
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import (
    mapping_summary,
    seed_mapping,
    seed_mapping_row,
    seed_quarantined,
    seed_stored_mapping,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

_names = count()
EDITOR_FORM = "librenms-mapping-edit-"


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        configure_librenms_servers(
            settings,
            {
                key: {"display_name": f"{key.title()} LibreNMS", "librenms_url": server.url, "api_token": "edit-token"}
                for key, server in (("primary", primary), ("secondary", secondary))
            },
        )
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
        yield SimpleNamespace(primary=primary, secondary=secondary)


class _EditorForms(HTMLParser):
    """Collect the fields of each editor form, also the fields that join a form through ``form=``."""

    def __init__(self):
        super().__init__()
        self.forms = defaultdict(lambda: {"action": None, "fields": {}})
        self._form = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id", "").startswith(EDITOR_FORM):
            self._form = attrs["id"]
            self.forms[self._form]["action"] = attrs["action"]
        elif tag in ("input", "select") and "name" in attrs:
            owner = attrs.get("form") or self._form
            if not (owner or "").startswith(EDITOR_FORM):
                return
            self.forms[owner]["fields"][attrs["name"]] = attrs.get("value", "")
            if tag == "select":
                self._select = (owner, attrs["name"])
        elif tag == "option" and self._select is not None and "selected" in attrs:
            owner, name = self._select
            self.forms[owner]["fields"][name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[obj.pk])


def _editor_forms(client, obj):
    """Render the sync page and return the editor form of each server: ``{server_key: (action, fields)}``."""
    response = client.get(_sync_url(obj))
    assert response.status_code == 200
    parser = _EditorForms()
    parser.feed(response.content.decode())
    return {form["fields"]["server_key"]: (form["action"], form["fields"]) for form in parser.forms.values()}


def _submit(client, obj, server_key, **values):
    """Post the rendered editor form of *server_key* with *values* typed into it."""
    action, fields = _editor_forms(client, obj)[server_key]
    return client.post(action, {**fields, **values})


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _change_records(obj):
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(type(obj)), changed_object_id=obj.pk
    ).count()


def _post(client, obj, **data):
    """Post to the editor directly, as a hand-made request does."""
    url = reverse("plugins:netbox_librenms_plugin:edit_server_mapping", args=[obj.pk])
    return client.post(url, {"object_type": obj._meta.model_name, "server_key": "primary", **data})


@pytest.mark.django_db
@pytest.mark.parametrize("make_target", [make_device, make_vm], ids=["device", "vm"])
def test_the_form_creates_a_mapping_on_a_server_with_none(client, servers, make_target):
    target = make_target(f"edit-create-{make_target.__name__}")
    client.force_login(make_superuser("edit-create-writer"))
    records = _change_records(target)

    response = _submit(client, target, "secondary", own_id="14001")

    assert response.status_code == 302
    assert response.url == f"{_sync_url(target)}?tab=interfaces&server_key=secondary"
    target.refresh_from_db()
    assert mapping_summary(target) == {"secondary": {"own": 14001}}
    assert _change_records(target) == records + 1


@pytest.mark.django_db
def test_a_new_own_id_replaces_the_shown_one_and_keeps_the_other_servers(client, servers):
    target = make_device("edit-replace-own")
    seed_mapping(target, "secondary", own=14004, save=False)
    seed_mapping(target, "primary", own=14002, preferred=True)
    client.force_login(make_superuser("edit-replace-writer"))

    response = _submit(client, target, "primary", own_id="14003")

    assert response.status_code == 302
    target.refresh_from_db()
    assert mapping_summary(target) == {
        "secondary": {"own": 14004},
        "primary": {"own": 14003},
        "preferred": "primary",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("seed", "typed", "expected"),
    [
        ({"own": 14020}, {"oob_id": "14021", "oob_type": "idrac"}, {"own": 14020, "oob": 14021, "oob_type": "idrac"}),
        (
            {"own": 14022, "oob": 14023, "oob_type": "ilo"},
            {"oob_id": "14024", "oob_type": "ipmi"},
            {"own": 14022, "oob": 14024, "oob_type": "ipmi"},
        ),
        ({"own": 14025, "oob": 14026, "oob_type": "bmc"}, {"oob_id": ""}, {"own": 14025}),
        (None, {"oob_id": "14027", "oob_type": "oob"}, {"oob": 14027, "oob_type": "oob"}),
        (
            {"own": 14030},
            {"own_id": "14030", "oob_id": "14030", "oob_type": "drac"},
            {"own": 14030, "oob": 14030, "oob_type": "drac"},
        ),
    ],
    ids=["add", "change", "clear", "oob-only", "same-id"],
)
def test_the_form_adds_changes_and_clears_the_oob_entry(client, servers, seed, typed, expected):
    target = make_device(f"edit-oob-{next(_names)}")
    if seed is not None:
        seed_mapping(target, "primary", **seed)
    client.force_login(make_superuser("edit-oob-writer"))

    response = _submit(client, target, "primary", **typed)

    assert response.status_code == 302
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": expected}


@pytest.mark.django_db
def test_an_unchanged_form_writes_nothing(client, servers):
    target = seed_mapping(make_device("edit-unchanged"), "primary", own=14031, oob=14032, oob_type="ilo")
    client.force_login(make_superuser("edit-unchanged-writer"))
    records = _change_records(target)
    last_updated = type(target).objects.values_list("last_updated", flat=True).get(pk=target.pk)

    response = _submit(client, target, "primary")

    assert response.status_code == 302
    assert _change_records(target) == records
    assert type(target).objects.values_list("last_updated", flat=True).get(pk=target.pk) == last_updated
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"own": 14031, "oob": 14032, "oob_type": "ilo"}}


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["own_id", "oob_id"])
@pytest.mark.parametrize("owner_visible", [True, False], ids=["owner-visible", "owner-hidden"])
def test_an_id_that_another_object_owns_is_refused(client, servers, field, owner_visible):
    from dcim.models import Device

    target = seed_mapping(make_device(f"edit-conflict-target-{field}-{owner_visible}"), "primary", own=14040)
    owner = seed_mapping(make_device(f"edit-conflict-owner-{field}-{owner_visible}"), "primary", own=14041)
    if owner_visible:
        user = make_user_with_perms(
            f"edit-conflict-{field}-visible", [("change", Device)], constraints={"pk": target.pk}
        )
        user = grant(user, "view", Device)
    else:
        user = make_user_with_perms(
            f"edit-conflict-{field}-hidden", [("view", Device), ("change", Device)], constraints={"pk": target.pk}
        )
    client.force_login(user)
    typed = {"own_id": "14041"} if field == "own_id" else {"oob_id": "14041", "oob_type": "idrac"}

    response = _submit(client, target, "primary", **typed)

    assert response.status_code == 302
    [text] = _messages(response)
    assert "14041" in text
    assert (owner.name in text) is owner_visible
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"own": 14040}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "typed",
    [
        {"own_id": "abc"},
        {"own_id": "0"},
        {"own_id": "-5"},
        {"own_id": "9223372036854775808"},
        {"oob_id": "1.5", "oob_type": "idrac"},
        {"oob_id": "14051", "oob_type": "toaster"},
        {"oob_id": "14051", "oob_type": ""},
        {"own_id": "", "oob_id": ""},
    ],
    ids=["text", "zero", "negative", "above-bigint", "oob-fraction", "unknown-type", "no-type", "nothing"],
)
def test_a_bad_value_is_refused(client, servers, typed):
    target = seed_mapping(make_device(f"edit-bad-{next(_names)}"), "primary", own=14050)
    client.force_login(make_superuser("edit-bad-writer"))
    records = _change_records(target)

    response = _submit(client, target, "primary", **typed)

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    assert _change_records(target) == records
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"own": 14050}}


@pytest.mark.django_db
@pytest.mark.parametrize("server_key", ["retired", "", "_preferred_server"])
def test_a_server_that_is_not_configured_is_refused(client, servers, server_key):
    target = seed_mapping(make_device(f"edit-unknown-server-{server_key}"), "retired", own=14060)
    client.force_login(make_superuser("edit-unknown-server-writer"))

    response = _post(client, target, server_key=server_key, own_id="14061")

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    target.refresh_from_db()
    assert mapping_summary(target) == {"retired": {"own": 14060}}


@pytest.mark.django_db
def test_an_invalid_stored_mapping_is_refused_and_offers_no_form(client, servers):
    target = seed_quarantined(make_device("edit-quarantined"))
    client.force_login(make_superuser("edit-quarantined-writer"))

    assert _editor_forms(client, target) == {}
    response = _post(client, target, own_id="14070")

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    target.refresh_from_db()
    assert mapping_summary(target) == {"quarantined": True}


@pytest.mark.django_db
def test_a_legacy_stored_mapping_is_refused(client, servers):
    from netbox_librenms_plugin.server_mappings import ContainerStatus, read_mapping

    target = seed_stored_mapping(make_device("edit-legacy"), 14071, save=True)
    client.force_login(make_superuser("edit-legacy-writer"))

    response = _post(client, target, own_id="14072")

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    target.refresh_from_db()
    mapping = read_mapping(target)
    assert (mapping.container, mapping.legacy.readable_id) == (ContainerStatus.LEGACY, 14071)


@pytest.mark.django_db
def test_a_merged_server_entry_is_refused(client, servers):
    winner = make_device("edit-merge-winner")
    target = seed_mapping(make_device("edit-merge-donor"), "primary", migrated_to=winner.pk)
    client.force_login(make_superuser("edit-merge-writer"))

    response = _post(client, target, own_id="14073")

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"migrated_to": winner.pk}}


@pytest.mark.django_db
def test_an_object_outside_the_change_scope_is_not_found(client, servers):
    from dcim.models import Device

    target = make_device("edit-outside-scope")
    allowed = make_device("edit-inside-scope")
    user = make_user_with_perms("edit-scoped-writer", [("change", Device)], constraints={"pk": allowed.pk})
    client.force_login(user)

    response = _post(client, target, own_id="14080")

    assert response.status_code == 404
    target.refresh_from_db()
    assert mapping_summary(target) == {}


@pytest.mark.django_db
def test_a_viewer_without_change_permission_gets_no_form(client, servers):
    from dcim.models import Device

    target = seed_mapping(make_device("edit-view-only"), "primary", own=14081)
    _register_device(servers.primary, 14081, target.name)
    client.force_login(make_user_with_perms("edit-view-only-reader", [("view", Device)]))

    assert _editor_forms(client, target) == {}


@pytest.mark.django_db
def test_the_form_is_offered_only_for_the_configured_servers(client, servers):
    target = seed_mapping(make_device("edit-server-list"), "retired", own=14082)
    client.force_login(make_superuser("edit-server-list-writer"))

    assert set(_editor_forms(client, target)) == {"primary", "secondary"}


def _register_device(server, device_id, name):
    server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [{"device_id": device_id, "sysName": name, "hostname": name}]},
    )


@transactional_db_with_all_apps()
def test_a_change_between_the_render_and_the_lock_is_a_conflict(client, servers):
    from django.db import connection

    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection

    target = seed_mapping(make_device("edit-concurrent"), "primary", own=14090)
    _register_device(servers.primary, 14090, target.name)
    client.force_login(make_superuser("edit-concurrent-writer"))
    action, fields = _editor_forms(client, target)["primary"]
    other_writer = seed_mapping(type(target)(), "primary", own=14091, save=False)

    with second_connection() as other:
        committed = []

        def commit_before_the_lock(execute, sql, params, many, context):
            if not committed and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql.upper():
                seed_mapping_row(target, other_writer, connection=other)
                other.commit()
                committed.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(commit_before_the_lock):
            response = client.post(action, {**fields, "own_id": "14092"})

    assert committed
    assert response.status_code == 302
    assert _messages(response) == [TRY_AGAIN_MESSAGE]
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"own": 14091}}


@pytest.mark.django_db
def test_an_own_id_cannot_be_cleared_because_discovery_would_restore_it(client, servers):
    target = seed_mapping(make_device("edit-clear-own"), "primary", own=14028, oob=14029, oob_type="cimc")
    client.force_login(make_superuser("edit-clear-own-writer"))

    response = _submit(client, target, "primary", own_id="")

    assert response.status_code == 302
    assert len(_messages(response)) == 1
    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": {"own": 14028, "oob": 14029, "oob_type": "cimc"}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("typed", "expected"),
    [({"oob_id": ""}, {"own": 14300}), ({"own_id": "14302"}, {"own": 14302, "oob": 14301, "oob_type": "idrac"})],
    ids=["oob-cleared", "own-replaced"],
)
def test_an_edit_survives_the_page_load_after_the_redirect(client, servers, typed, expected):
    """Discovery finds the device by name as 14300, so a page load could write that ID back."""
    target = seed_mapping(
        make_device(f"edit-discoverable-{next(_names)}"), "primary", own=14300, oob=14301, oob_type="idrac"
    )
    for device_id in (14300, 14302):
        _register_device(servers.primary, device_id, target.name)
    servers.primary.register(
        f"/api/v0/devices/{target.name}",
        {"status": "ok", "devices": [{"device_id": 14300, "sysName": target.name, "hostname": target.name}]},
    )
    client.force_login(make_superuser("edit-discoverable-writer"))

    response = _submit(client, target, "primary", **typed)
    assert response.status_code == 302
    assert client.get(response.url).status_code == 200

    target.refresh_from_db()
    assert mapping_summary(target) == {"primary": expected}


@pytest.mark.django_db
def test_a_vc_member_edits_its_own_mapping_when_another_member_serves_the_active_server(client, servers):
    """The page's global owner is member one, but member two holds the own ID of the active server."""
    _chassis, (first, second) = make_virtual_chassis_members("edit-vc")
    seed_mapping(first, "primary", own=14200, save=False)
    seed_mapping(first, "secondary", oob=14201, oob_type="ilo")
    seed_mapping(second, "secondary", own=14202)
    client.force_login(make_superuser("edit-vc-writer"))

    response = client.get(_sync_url(first), {"server_key": "secondary"})
    assert response.status_code == 200
    parser = _EditorForms()
    parser.feed(response.content.decode())
    forms = {form["fields"]["server_key"]: form for form in parser.forms.values()}
    action, fields = forms["primary"]["action"], forms["primary"]["fields"]

    response = client.post(action, {**fields, "own_id": "14203"})

    assert response.status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert mapping_summary(first) == {"primary": {"own": 14203}, "secondary": {"oob": 14201, "oob_type": "ilo"}}
    assert mapping_summary(second) == {"secondary": {"own": 14202}}


@pytest.mark.django_db
def test_the_editor_shows_the_own_id_that_discovery_stored_on_a_vc_member(client, servers):
    """Discovery writes the member through another instance; the next OOB edit must not read as a change."""
    _chassis, (first, _second) = make_virtual_chassis_members("edit-vc-discovery")
    seed_mapping(first, "primary", own=14400)
    _register_device(servers.secondary, 14402, first.name)
    servers.secondary.register(
        f"/api/v0/devices/{first.name}",
        {"status": "ok", "devices": [{"device_id": 14402, "sysName": first.name, "hostname": first.name}]},
    )
    client.force_login(make_superuser("edit-vc-discovery-writer"))
    created = _submit(client, first, "secondary", oob_id="14401", oob_type="idrac")
    rendered = client.get(created.url)
    assert rendered.status_code == 200
    parser = _EditorForms()
    parser.feed(rendered.content.decode())
    form = next(form for form in parser.forms.values() if form["fields"]["server_key"] == "secondary")

    response = client.post(form["action"], {**form["fields"], "oob_id": "14403", "oob_type": "ilo"})

    assert response.status_code == 302
    first.refresh_from_db()
    assert mapping_summary(first) == {
        "primary": {"own": 14400},
        "secondary": {"own": 14402, "oob": 14403, "oob_type": "ilo"},
    }
