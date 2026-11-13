"""
The confirmation contract of every registered (resource, action).

Each scenario mints its token through the real assessment entry point, changes one fact in the real
database or source snapshot, and submits the token to the real applying route over HTTP. A refused
submit leaves every domain table as it was.
"""

import importlib
import pkgutil
import re
import time

import pytest
from dcim.models import Device
from django.core import signing
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.tests import link_intent_helpers as links
from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device
from netbox_librenms_plugin.tests.mapping_fixtures import mapping_summary, seed_mapping, seed_quarantined
from netbox_librenms_plugin.tests.view_test_helpers import domain_state, make_user_with_perms
from netbox_librenms_plugin.tests.vlan_intent_helpers import message_texts, reassess, seed_vlans, sync, tab_tokens

SERVERS = {
    "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"},
    "other": {"librenms_url": "https://other-librenms.example.com", "api_token": "test-token"},
}
VLAN_PERMISSIONS = [("view", Device), ("view", VLAN), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)]
_STALE = re.compile(r"^VLAN (\d+): .*\(([^)]*)\)")


class _Clock:
    """Stand in for the time module that django.core.signing reads, at a time the test sets."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


class _VLANScenario:
    """One device whose source snapshot holds VID 100 and VID 200, one VLAN group, and a user who may sync."""

    resource = intent.Resource.VLAN
    permissions = VLAN_PERMISSIONS
    vid = 100
    confirm = False

    def __init__(self, tag, client, settings, librenms_server):
        self.tag = tag
        self.client = client
        self.device = seed_mapping(make_device(f"contract-{tag}"), "default", own=42)
        seed_mapping(self.device, "other", own=42)
        self.group = VLANGroup.objects.create(name=f"contract-{tag}", slug=f"contract-{tag}")
        self.group_pk = self.group.pk
        self.user = make_user_with_perms(f"contract-{tag}", VLAN_PERMISSIONS)
        self.source_name = "users"
        self.seed()
        client.force_login(self.user)
        self.arrange()

    def seed(self, device=None, server_key="default", rows=None):
        rows = (
            rows
            if rows is not None
            else [
                {"vlan_vlan": self.vid, "vlan_name": self.source_name},
                {"vlan_vlan": 200, "vlan_name": "printers"},
            ]
        )
        seed_vlans(device or self.device, rows, server_key)

    def submit(self, tokens, *, subject=None, server_key="default"):
        return sync(self.client, subject or self.device, tokens, confirm=self.confirm, server_key=server_key)

    def stale_paths(self, response):
        return _stale_paths(response, self.vid)

    def other_subject(self):
        other = seed_mapping(make_device(f"other-subject-{self.tag}"), "default", own=43)
        self.seed(device=other)
        return other

    def prepare_server(self, server_key):
        self.seed(server_key=server_key)

    def legacy_tokens(self):
        legacy = signing.dumps(
            {
                "vid": self.vid,
                "object_type": "device",
                "object_pk": str(self.device.pk),
                "server_key": "default",
                "target_group_id": self.group.pk,
                "proposed_name": self.source_name,
            },
            salt="netbox_librenms_plugin.vlan_conflict",
            compress=True,
        )
        return [legacy, f"vlan:{legacy}"]

    def change_source_name(self):
        self.source_name = "staff"
        self.seed()

    def change_name_rule(self):
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_VLAN_NAME, match_pattern=r"^users$", replacement="people"
        )

    def remove_source_row(self):
        self.seed(rows=[{"vlan_vlan": 200, "vlan_name": "printers"}])

    def change_other_source_row(self):
        self.seed(rows=[{"vlan_vlan": self.vid, "vlan_name": self.source_name}, {"vlan_vlan": 200, "vlan_name": "x"}])

    def add_unrelated_vlan(self):
        VLAN.objects.create(vid=300, group=self.group, name="unrelated", status="active")

    def revoke(self, action):
        from users.models import ObjectPermission

        ObjectPermission.objects.filter(users=self.user, actions=[action], object_types__model="vlan").delete()


class _VLANCreateScenario(_VLANScenario):
    """No VLAN holds VID 100; the operator chose the group, so the token comes from the reassessment."""

    action = "create"
    relevant = {
        "target_appears": "assumptions.target",
        "source_name": "effects.name",
        "name_rule": "effects.name",
        "source_row_gone": "source.row",
    }
    unrelated = ("unrelated_vlan", "other_source_row", "group_description", "same_vid_in_another_scope")
    target_gone_path = "target.group"
    revoked = "add"

    def arrange(self):
        pass

    def mint(self):
        response = reassess(self.client, self.device, self.vid, self.group)
        assert response.status_code == 200, response.content
        return response.json()["confirmation_intent"]

    def applied(self):
        return VLAN.objects.filter(vid=self.vid, group_id=self.group_pk, name=self.source_name).exists()

    def change(self, name):
        if name == "target_appears":
            VLAN.objects.create(vid=self.vid, group=self.group, name="concurrent", status="active")
        elif name == "source_name":
            self.change_source_name()
        elif name == "name_rule":
            self.change_name_rule()
        elif name == "source_row_gone":
            self.remove_source_row()
        elif name == "unrelated_vlan":
            self.add_unrelated_vlan()
        elif name == "other_source_row":
            self.change_other_source_row()
        elif name == "group_description":
            VLANGroup.objects.filter(pk=self.group.pk).update(description="changed")
        elif name == "same_vid_in_another_scope":
            VLAN.objects.create(vid=self.vid, name="global", status="active")
        else:
            raise AssertionError(name)

    def remove_target(self):
        self.group.delete()


class _VLANUpdateScenario(_VLANScenario):
    """The group holds VID 100 under another name; the tab offers the rename, which the disclosure confirms."""

    action = "update"
    confirm = True
    relevant = {
        "renamed_in_netbox": "assumptions.target.name",
        "source_name": "effects.name",
        "moved_to_another_group": "assumptions.target",
        "source_row_gone": "source.row",
    }
    unrelated = ("description", "unrelated_vlan", "other_source_row")
    target_gone_path = "assumptions.target"
    revoked = "change"

    def arrange(self):
        self.vlan = VLAN.objects.create(vid=self.vid, group=self.group, name="old name", status="active")
        self.vlan_pk = self.vlan.pk

    def mint(self):
        return tab_tokens(self.client, self.device)[self.vid]

    def applied(self):
        return VLAN.objects.filter(pk=self.vlan_pk, name=self.source_name).exists()

    def change(self, name):
        if name == "renamed_in_netbox":
            VLAN.objects.filter(pk=self.vlan.pk).update(name="renamed")
        elif name == "source_name":
            self.change_source_name()
        elif name == "moved_to_another_group":
            other = VLANGroup.objects.create(name=f"moved-{self.tag}", slug=f"moved-{self.tag}")
            VLAN.objects.filter(pk=self.vlan.pk).update(group=other)
        elif name == "source_row_gone":
            self.remove_source_row()
        elif name == "description":
            VLAN.objects.filter(pk=self.vlan.pk).update(description="changed")
        elif name == "unrelated_vlan":
            self.add_unrelated_vlan()
        elif name == "other_source_row":
            self.change_other_source_row()
        else:
            raise AssertionError(name)

    def remove_target(self):
        self.vlan.delete()


class _LinkScenario:
    """An unlinked device whose primary IP the LibreNMS fake matches as device 7100, and a user who may link it."""

    resource = intent.Resource.MAPPING_LIFECYCLE
    action = "link"
    permissions = [("view", Device), ("change", Device)]
    librenms_id = 7100
    relevant = {
        "primary_ip_changed": "assumptions.inputs.primary_ip4.address",
        "another_object_took_the_id": "assumptions.owned",
        "linked_meanwhile": "assumptions.own_id",
        "stored_value_invalid": "assumptions.linkable",
    }
    unrelated = ("name_changed", "description_changed", "another_id_taken", "other_server_linked")
    target_gone_path = "assumptions.inputs.primary_ip4"
    revoked = "change"

    def __init__(self, tag, client, settings, librenms_server):
        self.tag = tag
        self.client = client
        self.server = librenms_server
        url = librenms_server.url
        configure_librenms_servers(
            settings, {key: {"librenms_url": url, "api_token": "test-token"} for key in ("default", "other")}
        )
        self.device = make_device(f"contract-{tag}")
        self.ip = links.give_primary_ip(self.device, "198.51.100.10/24")
        links.register_match(librenms_server, "198.51.100.10", self.librenms_id)
        self.user = make_user_with_perms(f"contract-{tag}", self.permissions)
        client.force_login(self.user)

    def mint(self):
        return links.page_token(self.client, self.device)

    def submit(self, tokens, *, subject=None, server_key="default"):
        return links.link(self.client, subject or self.device, tokens, server_key=server_key)

    def applied(self):
        self.device.refresh_from_db()
        return mapping_summary(self.device).get("default", {}).get("own") == self.librenms_id

    def stale_paths(self, response):
        return links.stale_paths(response, self.device.name)

    def change(self, name):
        from ipam.models import IPAddress

        if name == "primary_ip_changed":
            IPAddress.objects.filter(pk=self.ip.pk).update(address="198.51.100.11/24")
        elif name == "another_object_took_the_id":
            seed_mapping(make_device(f"owner-{self.tag}"), "default", own=self.librenms_id)
        elif name == "linked_meanwhile":
            seed_mapping(Device.objects.get(pk=self.device.pk), "default", own=self.librenms_id + 1)
        elif name == "stored_value_invalid":
            seed_quarantined(Device.objects.get(pk=self.device.pk))
        elif name == "name_changed":
            Device.objects.filter(pk=self.device.pk).update(name=f"renamed-{self.tag}")
            self.device.name = f"renamed-{self.tag}"
        elif name == "description_changed":
            Device.objects.filter(pk=self.device.pk).update(description="changed")
        elif name == "another_id_taken":
            seed_mapping(make_device(f"unrelated-{self.tag}"), "default", own=self.librenms_id + 2)
        elif name == "other_server_linked":
            seed_mapping(Device.objects.get(pk=self.device.pk), "other", own=self.librenms_id)
        else:
            raise AssertionError(name)

    def remove_target(self):
        self.ip.delete()

    def revoke(self, action):
        from users.models import ObjectPermission

        ObjectPermission.objects.filter(users=self.user, actions=[action], object_types__model="device").delete()

    def other_subject(self):
        other = make_device(f"other-subject-{self.tag}")
        links.give_primary_ip(other, "198.51.100.20/24")
        links.register_match(self.server, "198.51.100.20", self.librenms_id + 3)
        return other

    def prepare_server(self, server_key):
        pass

    def legacy_tokens(self):
        legacy = signing.dumps(
            {"object_pk": self.device.pk, "librenms_id": self.librenms_id, "server_key": "default"},
            salt="netbox_librenms_plugin.librenms_id",
            compress=True,
        )
        return [legacy, f"mapping_lifecycle:{legacy}"]


SCENARIOS = {
    (intent.Resource.VLAN, "create"): _VLANCreateScenario,
    (intent.Resource.VLAN, "update"): _VLANUpdateScenario,
    (intent.Resource.MAPPING_LIFECYCLE, "link"): _LinkScenario,
}
ACTION_IDS = {key: f"{key[0]}.{key[1]}" for key in SCENARIOS}


def _actions():
    return pytest.mark.parametrize("key", list(SCENARIOS), ids=ACTION_IDS.get)


def _facts(kind):
    cases = [(key, name) for key, scenario in SCENARIOS.items() for name in getattr(scenario, kind)]
    return pytest.mark.parametrize(("key", "fact"), cases, ids=[f"{ACTION_IDS[key]}-{name}" for key, name in cases])


@pytest.fixture
def scenario_for(client, settings, librenms_server):
    configure_librenms_servers(settings, SERVERS)

    def build(key, tag=None):
        return SCENARIOS[key](tag or ACTION_IDS[key].replace(".", "-"), client, settings, librenms_server)

    return build


def _assert_refused(scenario, tokens, **submit):
    """Submit *tokens*, and assert that the request changed no domain table and applied nothing."""
    before = domain_state()
    response = scenario.submit(tokens, **submit)
    assert domain_state() == before
    assert not scenario.applied()
    return response


def _stale_paths(response, vid):
    return {
        path
        for text in message_texts(response)
        if (match := _STALE.match(text)) and int(match.group(1)) == vid
        for path in match.group(2).split(", ")
    }


def _resign(token, change):
    prefix, _separator, signed = token.partition(":")
    envelope = signing.loads(signed, salt=f"{intent.SALT_NAMESPACE}:{prefix}")
    change(envelope)
    return f"{prefix}:{signing.dumps(envelope, salt=f'{intent.SALT_NAMESPACE}:{prefix}', compress=True)}"


def _foreign_resource(resource):
    return intent.Resource.IP_ADDRESS if resource is not intent.Resource.IP_ADDRESS else intent.Resource.VLAN


def test_every_registered_action_has_one_scenario():
    import netbox_librenms_plugin.assessment as assessment

    for module in pkgutil.walk_packages(assessment.__path__, prefix=f"{assessment.__name__}."):
        importlib.import_module(module.name)

    assert set(SCENARIOS) == intent.registered_actions()


@pytest.mark.django_db
class TestContract:
    @_actions()
    def test_an_unchanged_approval_applies(self, scenario_for, key):
        scenario = scenario_for(key)

        response = scenario.submit([scenario.mint()])

        assert response.status_code in (200, 302)
        assert scenario.applied()

    @_facts("relevant")
    def test_a_relevant_change_is_stale_with_its_path(self, scenario_for, key, fact):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.change(fact)

        response = _assert_refused(scenario, [token])

        assert SCENARIOS[key].relevant[fact] in scenario.stale_paths(response)

    @_facts("unrelated")
    def test_an_unrelated_change_still_applies(self, scenario_for, key, fact):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.change(fact)

        scenario.submit([token])

        assert scenario.applied()

    @_actions()
    def test_the_target_gone_is_stale(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.remove_target()

        response = _assert_refused(scenario, [token])

        assert scenario.target_gone_path in scenario.stale_paths(response)

    @_actions()
    def test_an_expired_approval_refuses(self, scenario_for, key, monkeypatch):
        scenario = scenario_for(key)
        with monkeypatch.context() as clock:
            clock.setattr(signing, "time", _Clock(time.time() - intent.MAX_AGE_SECONDS - 1))
            token = scenario.mint()

        _assert_refused(scenario, [token])

    @_actions()
    def test_a_tampered_token_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()

        _assert_refused(scenario, [token[:-1] + ("A" if token[-1] != "A" else "B")])

    @_actions()
    def test_a_cross_resource_token_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        prefix, _separator, signed = token.partition(":")
        other = _foreign_resource(scenario.resource).value
        envelope = signing.loads(signed, salt=f"{intent.SALT_NAMESPACE}:{prefix}")
        foreign = signing.dumps(envelope, salt=f"{intent.SALT_NAMESPACE}:{other}", compress=True)

        _assert_refused(scenario, [f"{other}:{signed}"])
        _assert_refused(scenario, [f"{other}:{foreign}"])
        _assert_refused(scenario, [f"{prefix}:{foreign}"])

    @_actions()
    def test_another_actor_refuses(self, scenario_for, key, client):
        scenario = scenario_for(key)
        token = scenario.mint()
        client.force_login(make_user_with_perms(f"other-actor-{scenario.tag}", scenario.permissions))

        _assert_refused(scenario, [token])

    @_actions()
    def test_another_subject_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        other = scenario.other_subject()

        _assert_refused(scenario, [token], subject=other)

    @_actions()
    def test_another_server_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.prepare_server("other")

        _assert_refused(scenario, [token], server_key="other")

    @_actions()
    def test_another_action_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        others = sorted({action for _resource, action in SCENARIOS} - {key[1]}) + ["delete", "unlink"]

        for other in others:
            _assert_refused(scenario, [_resign(token, lambda envelope, other=other: envelope.update(action=other))])

    @_actions()
    def test_a_missing_token_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        scenario.mint()

        _assert_refused(scenario, [])
        _assert_refused(scenario, [""])

    @_actions()
    def test_a_duplicate_token_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()

        _assert_refused(scenario, [token, token])

    @_actions()
    def test_a_legacy_token_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        scenario.mint()

        for legacy in scenario.legacy_tokens():
            _assert_refused(scenario, [legacy])

    @_actions()
    def test_a_revoked_permission_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.revoke(scenario.revoked)

        response = _assert_refused(scenario, [token])

        assert any("Missing permissions" in text for text in message_texts(response))


@pytest.mark.django_db
def test_an_unconfirmed_rename_discloses_its_token_and_the_disclosed_token_applies(scenario_for):
    scenario = scenario_for((intent.Resource.VLAN, "update"), tag="disclosure")
    token = scenario.mint()
    before = domain_state()

    disclosure = sync(scenario.client, scenario.device, [token])

    assert domain_state() == before
    assert [conflict["token"] for conflict in disclosure.context["conflicts"]] == [token]
    assert scenario.submit([token]).status_code == 200
    assert scenario.applied()


@pytest.mark.django_db
def test_a_token_carries_only_what_the_viewer_may_see(client, settings):
    """A signed payload is readable, so a VLAN outside the viewer's scope never enters it, nor a message."""
    from netbox_librenms_plugin.tests.view_test_helpers import grant

    configure_librenms_servers(settings, SERVERS)
    device = seed_mapping(make_device("contract-disclosure"), "default", own=42)
    hidden_group = VLANGroup.objects.create(name="contract-hidden", slug="contract-hidden")
    hidden = VLAN.objects.create(vid=100, group=hidden_group, name="secret-name", status="active")
    user = make_user_with_perms(
        "contract-disclosure", [("view", Device), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)]
    )
    user = grant(user, "view", VLAN, constraints={"group__name": "another-group"})
    seed_vlans(device, [{"vlan_vlan": 100, "vlan_name": "users"}])
    client.force_login(user)

    tokens = [
        tab_tokens(client, device)[100],
        reassess(client, device, 100, hidden_group).json()["confirmation_intent"],
    ]

    for token in tokens:
        payload = signing.loads(token.partition(":")[2], salt=f"{intent.SALT_NAMESPACE}:vlan")
        assert payload["operation"]["assumptions"] == {"target": None}
        assert "secret-name" not in str(payload) and f"'vlan': {hidden.pk}" not in str(payload)
    response = _assert_refused(_SingleTokenRoute(client, device), [tokens[1]])
    assert "assumptions.target" in _stale_paths(response, 100)
    assert not any("secret-name" in text for text in message_texts(response))


class _SingleTokenRoute:
    """Submit tokens to one device's VLAN route; nothing a refused submit could apply is in scope."""

    def __init__(self, client, device):
        self.client, self.device = client, device

    def submit(self, tokens):
        return sync(self.client, self.device, tokens)

    def applied(self):
        return VLAN.objects.filter(vid=100, name="users").exists()


@pytest.mark.django_db
def test_a_repeated_token_cannot_clear_the_way_for_another_token_of_its_vid(client, settings):
    """[T1, T1, T2] for one VID refuses both choices; an unrelated VID still applies."""
    configure_librenms_servers(settings, SERVERS)
    device = seed_mapping(make_device("contract-repeat"), "default", own=42)
    first = VLANGroup.objects.create(name="contract-repeat-a", slug="contract-repeat-a")
    second = VLANGroup.objects.create(name="contract-repeat-b", slug="contract-repeat-b")
    seed_vlans(device, [{"vlan_vlan": 100, "vlan_name": "users"}, {"vlan_vlan": 200, "vlan_name": "printers"}])
    client.force_login(make_user_with_perms("contract-repeat", VLAN_PERMISSIONS))
    in_first = reassess(client, device, 100, first).json()["confirmation_intent"]
    in_second = reassess(client, device, 100, second).json()["confirmation_intent"]

    response = sync(client, device, [in_first, in_first, in_second, tab_tokens(client, device)[200]])

    assert not VLAN.objects.filter(vid=100).exists()
    assert VLAN.objects.filter(vid=200, name="printers").exists()
    assert "identity.duplicate" in _stale_paths(response, 100)


class _DependentRename:
    """VID 100 is named users in a group; the source renames it to staff and creates VID 200 named users."""

    def __init__(self, client, settings, tag, permissions=VLAN_PERMISSIONS):
        configure_librenms_servers(settings, SERVERS)
        self.client = client
        self.device = seed_mapping(make_device(f"dependent-{tag}"), "default", own=42)
        self.group = VLANGroup.objects.create(name=f"dependent-{tag}", slug=f"dependent-{tag}")
        self.vlan = VLAN.objects.create(vid=100, group=self.group, name="users", status="active")
        self.seed("users")
        self.user = make_user_with_perms(f"dependent-{tag}", permissions)
        client.force_login(self.user)
        self.rename = tab_tokens(client, self.device)[100]
        self.create = reassess(client, self.device, 200, self.group).json()["confirmation_intent"]

    def seed(self, name_of_200):
        seed_vlans(
            self.device, [{"vlan_vlan": 100, "vlan_name": "staff"}, {"vlan_vlan": 200, "vlan_name": name_of_200}]
        )

    def names(self):
        return dict(VLAN.objects.filter(group=self.group).values_list("vid", "name"))


@pytest.mark.django_db
class TestDependentVLANOperations:
    """A create that takes the name a rename frees depends on it: they verify and write as one group."""

    def test_a_stale_dependent_member_writes_nothing_of_its_group(self, client, settings):
        case = _DependentRename(client, settings, "stale")
        case.seed("guests")
        before = domain_state()

        response = sync(client, case.device, [case.rename, case.create], confirm=True)

        assert domain_state() == before
        assert case.names() == {100: "users"}
        assert "effects.name" in _stale_paths(response, 100)
        assert "effects.name" in _stale_paths(response, 200)

    def test_an_unchanged_group_writes_every_member_in_the_writer_schedule(self, client, settings):
        case = _DependentRename(client, settings, "control")

        # The create comes first in the request; the writer still renames first, which frees the name.
        sync(client, case.device, [case.create, case.rename], confirm=True)

        assert case.names() == {100: "staff", 200: "users"}

    def test_a_write_failure_in_the_group_rolls_back_every_member(self, client, settings):
        from netbox_librenms_plugin.tests.view_test_helpers import grant

        permissions = [permission for permission in VLAN_PERMISSIONS if permission != ("add", VLAN)]
        case = _DependentRename(client, settings, "rollback", permissions)
        # The new VLAN falls outside the add constraint, so the create fails after the rename wrote.
        client.force_login(grant(case.user, "add", VLAN, constraints={"vid": 999}))

        response = sync(client, case.device, [case.rename, case.create], confirm=True)

        assert case.names() == {100: "users"}
        texts = message_texts(response)
        assert any(text.startswith("VLAN 200: ") and "add permission" in text for text in texts)
        assert any(text.startswith("VLAN 100: ") and "group failed" in text for text in texts)

    def test_a_rolled_back_group_sends_no_event(self, client, settings, flushed_events):
        """The rename wrote and the create was refused; both roll back, so neither save's event is sent."""
        from netbox_librenms_plugin.tests.view_test_helpers import grant

        permissions = [permission for permission in VLAN_PERMISSIONS if permission != ("add", VLAN)]
        case = _DependentRename(client, settings, "rollback-events", permissions)
        client.force_login(grant(case.user, "add", VLAN, constraints={"vid": 999}))

        sync(client, case.device, [case.rename, case.create], confirm=True)

        assert case.names() == {100: "users"}
        assert flushed_events == []

    def test_an_unconfirmed_group_is_disclosed_whole_and_its_tokens_apply_it(self, client, settings):
        case = _DependentRename(client, settings, "disclosure")
        before = domain_state()

        disclosure = sync(client, case.device, [case.create, case.rename])

        assert domain_state() == before
        assert sorted(conflict["token"] for conflict in disclosure.context["conflicts"]) == sorted(
            [case.create, case.rename]
        )
        sync(client, case.device, [conflict["token"] for conflict in disclosure.context["conflicts"]], confirm=True)
        assert case.names() == {100: "staff", 200: "users"}

    def test_unrelated_operations_still_succeed_partially(self, client, settings):
        case = _DependentRename(client, settings, "partial")
        seed_vlans(case.device, [{"vlan_vlan": 300, "vlan_name": "printers"}, {"vlan_vlan": 301, "vlan_name": "voice"}])
        tokens = tab_tokens(client, case.device)
        seed_vlans(case.device, [{"vlan_vlan": 300, "vlan_name": "changed"}, {"vlan_vlan": 301, "vlan_name": "voice"}])

        response = sync(client, case.device, [tokens[300], tokens[301]])

        assert not VLAN.objects.filter(vid=300).exists()
        assert VLAN.objects.filter(vid=301, name="voice").exists()
        assert "effects.name" in _stale_paths(response, 300)


class _RenameChain:
    """VID 100 and VID 200 hold alpha and beta in one group; the source proposes the names *proposed*."""

    def __init__(self, client, settings, tag, proposed):
        configure_librenms_servers(settings, SERVERS)
        self.device = seed_mapping(make_device(f"chain-{tag}"), "default", own=42)
        self.group = VLANGroup.objects.create(name=f"chain-{tag}", slug=f"chain-{tag}")
        VLAN.objects.create(vid=100, group=self.group, name="alpha", status="active")
        VLAN.objects.create(vid=200, group=self.group, name="beta", status="active")
        seed_vlans(
            self.device, [{"vlan_vlan": 100, "vlan_name": proposed[0]}, {"vlan_vlan": 200, "vlan_name": proposed[1]}]
        )
        client.force_login(make_user_with_perms(f"chain-{tag}", VLAN_PERMISSIONS))
        self.tokens = tab_tokens(client, self.device)

    def names(self):
        return dict(VLAN.objects.filter(group=self.group).values_list("vid", "name"))


@pytest.mark.django_db
class TestRenameSchedule:
    """The writer frees a name before another member of the group takes it, whatever the request order."""

    @pytest.mark.parametrize("order", [(100, 200), (200, 100)], ids=["vid-order", "reverse-order"])
    def test_a_rename_chain_applies_in_either_submission_order(self, client, settings, order):
        case = _RenameChain(client, settings, f"order-{order[0]}", ("beta", "gamma"))

        sync(client, case.device, [case.tokens[vid] for vid in order], confirm=True)

        assert case.names() == {100: "beta", 200: "gamma"}

    def test_a_name_swap_refuses_its_group_and_writes_nothing(self, client, settings):
        case = _RenameChain(client, settings, "swap", ("beta", "alpha"))
        before = domain_state()

        response = sync(client, case.device, [case.tokens[100], case.tokens[200]], confirm=True)

        assert domain_state() == before
        assert "schedule.cycle" in _stale_paths(response, 100)
        assert "schedule.cycle" in _stale_paths(response, 200)
