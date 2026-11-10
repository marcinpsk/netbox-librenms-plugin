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
from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device
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
    vid = 100
    confirm = False

    def __init__(self, tag, client):
        self.tag = tag
        self.client = client
        self.device = make_device(f"contract-{tag}", librenms_cf={"default": {"id": 42}, "other": {"id": 42}})
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

    def submit(self, tokens, *, device=None, server_key="default"):
        return sync(self.client, device or self.device, tokens, confirm=self.confirm, server_key=server_key)

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


SCENARIOS = {
    (intent.Resource.VLAN, "create"): _VLANCreateScenario,
    (intent.Resource.VLAN, "update"): _VLANUpdateScenario,
}
ACTION_IDS = {key: f"{key[0]}.{key[1]}" for key in SCENARIOS}


def _actions():
    return pytest.mark.parametrize("key", list(SCENARIOS), ids=ACTION_IDS.get)


def _facts(kind):
    cases = [(key, name) for key, scenario in SCENARIOS.items() for name in getattr(scenario, kind)]
    return pytest.mark.parametrize(("key", "fact"), cases, ids=[f"{ACTION_IDS[key]}-{name}" for key, name in cases])


@pytest.fixture
def scenario_for(client, settings):
    configure_librenms_servers(settings, SERVERS)

    def build(key, tag=None):
        return SCENARIOS[key](tag or ACTION_IDS[key].replace(".", "-"), client)

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
    _prefix, _separator, signed = token.partition(":")
    envelope = signing.loads(signed, salt=f"{intent.SALT_NAMESPACE}:vlan")
    change(envelope)
    return f"vlan:{signing.dumps(envelope, salt=f'{intent.SALT_NAMESPACE}:vlan', compress=True)}"


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

        assert response.status_code == 200
        assert scenario.applied()

    @_facts("relevant")
    def test_a_relevant_change_is_stale_with_its_path(self, scenario_for, key, fact):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.change(fact)

        response = _assert_refused(scenario, [token])

        assert SCENARIOS[key].relevant[fact] in _stale_paths(response, scenario.vid)

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

        assert scenario.target_gone_path in _stale_paths(response, scenario.vid)

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
        _prefix, _separator, signed = token.partition(":")
        envelope = signing.loads(signed, salt=f"{intent.SALT_NAMESPACE}:vlan")
        foreign = signing.dumps(envelope, salt=f"{intent.SALT_NAMESPACE}:ip_address", compress=True)

        _assert_refused(scenario, [f"ip_address:{signed}"])
        _assert_refused(scenario, [f"ip_address:{foreign}"])
        _assert_refused(scenario, [f"vlan:{foreign}"])

    @_actions()
    def test_another_actor_refuses(self, scenario_for, key, client):
        scenario = scenario_for(key)
        token = scenario.mint()
        client.force_login(make_user_with_perms(f"other-actor-{scenario.tag}", VLAN_PERMISSIONS))

        _assert_refused(scenario, [token])

    @_actions()
    def test_another_subject_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        other = make_device(f"other-subject-{scenario.tag}", librenms_cf={"default": {"id": 43}})
        scenario.seed(device=other)

        _assert_refused(scenario, [token], device=other)

    @_actions()
    def test_another_server_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        scenario.seed(server_key="other")

        _assert_refused(scenario, [token], server_key="other")

    @_actions()
    def test_another_action_refuses(self, scenario_for, key):
        scenario = scenario_for(key)
        token = scenario.mint()
        others = sorted({action for _resource, action in SCENARIOS} - {key[1]}) + ["delete"]

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
        legacy = signing.dumps(
            {
                "vid": scenario.vid,
                "object_type": "device",
                "object_pk": str(scenario.device.pk),
                "server_key": "default",
                "target_group_id": scenario.group.pk,
                "proposed_name": scenario.source_name,
            },
            salt="netbox_librenms_plugin.vlan_conflict",
            compress=True,
        )

        _assert_refused(scenario, [legacy])
        _assert_refused(scenario, [f"vlan:{legacy}"])

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
    device = make_device("contract-disclosure", librenms_cf={"default": {"id": 42}})
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
    device = make_device("contract-repeat", librenms_cf={"default": {"id": 42}})
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
