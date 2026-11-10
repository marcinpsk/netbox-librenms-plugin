"""The confirmation intent module: envelope, signing, expiry, strict decoding, context, roles and comparison."""

import dataclasses

import pytest
from django.core import signing

from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin.assessment.vlans import (
    VLANAssumptions,
    VLANChoices,
    VLANCreate,
    VLANCreateEffects,
    VLANIdentity,
    VLANTarget,
    VLANUpdate,
    VLANUpdateEffects,
)

CONTEXT = intent.IntentContext(7, ("dcim.device", 11), "default")
POLICY = intent.RoutePolicy(
    frozenset(
        {
            (intent.Resource.VLAN, "create", intent.Role.PRIMARY),
            (intent.Resource.VLAN, "update", intent.Role.PRIMARY),
        }
    )
)


def _create(vid=100, group=None, name="users"):
    return VLANCreate(
        VLANIdentity(vid), VLANChoices(group), VLANAssumptions(None), VLANCreateEffects(vid, group, name, "active")
    )


def _update(vid=100, group=5, current="old", proposed="new", pk=9):
    return VLANUpdate(
        VLANIdentity(vid),
        VLANChoices(group),
        VLANAssumptions(VLANTarget(pk, vid, group, current)),
        VLANUpdateEffects(pk, proposed),
    )


class _Rebuild:
    """Answer each approved operation with the operation it maps to, and record every call."""

    def __init__(self, rebuilt=None):
        self.rebuilt = rebuilt or {}
        self.calls = []

    def __call__(self, approved):
        self.calls.append(approved)
        return [((operation, self.rebuilt.get(operation, operation)),) for operation in approved]


def _signed(token):
    resource, _separator, signed = token.partition(":")
    return resource, signed


def _resign(token, change, *, resource="vlan"):
    """Return *token* with its envelope changed by *change* and signed again under *resource*'s salt."""
    _prefix, signed = _signed(token)
    envelope = signing.loads(signed, salt=f"{intent.SALT_NAMESPACE}:vlan")
    change(envelope)
    return f"vlan:{signing.dumps(envelope, salt=f'{intent.SALT_NAMESPACE}:{resource}', compress=True)}"


def _only(verification):
    (result,) = verification.results
    return result


class _Clock:
    """Stand in for the time module that django.core.signing reads, at a time the test sets."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


class TestEnvelope:
    def test_the_token_names_its_resource_and_signs_under_the_resource_salt(self):
        token = intent.offer(_create(), CONTEXT).token

        resource, signed = _signed(token)
        assert resource == "vlan"
        envelope = signing.loads(signed, salt="netbox_librenms_plugin.confirmation_intent.v1:vlan")
        assert envelope["operation"]["identity"] == {"vid": 100}
        with pytest.raises(signing.BadSignature):
            signing.loads(signed, salt="netbox_librenms_plugin.confirmation_intent.v1:ip_address")

    def test_an_unchanged_approval_verifies_with_its_token(self):
        offered = intent.offer(_create(), CONTEXT)
        rebuild = _Rebuild()

        result = _only(intent.verify([offered.token], policy=POLICY, context=CONTEXT, rebuild=rebuild))

        assert result == intent.Verified((intent.Offer(offered.token, offered.operation),))
        assert rebuild.calls == [(offered.operation,)]

    @pytest.mark.parametrize("prefix", ["ip_address", "interface", "unknown", ""])
    def test_a_relabelled_token_is_invalid(self, prefix):
        _resource, signed = _signed(intent.offer(_create(), CONTEXT).token)
        rebuild = _Rebuild()

        result = _only(intent.verify([f"{prefix}:{signed}"], policy=POLICY, context=CONTEXT, rebuild=rebuild))

        assert result.reason is intent.StaleReason.INVALID
        assert rebuild.calls == []

    def test_a_token_signed_under_another_resource_salt_is_invalid(self):
        token = _resign(intent.offer(_create(), CONTEXT).token, lambda envelope: None, resource="ip_address")

        result = _only(intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))

        assert result.reason is intent.StaleReason.INVALID

    def test_a_legacy_vlan_conflict_intent_is_invalid(self):
        legacy = signing.dumps(
            {"vid": 100, "object_type": "device", "object_pk": "11", "server_key": "default"},
            salt="netbox_librenms_plugin.vlan_conflict",
            compress=True,
        )

        for token in (legacy, f"vlan:{legacy}"):
            result = _only(intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))
            assert result.reason is intent.StaleReason.INVALID

    @pytest.mark.parametrize(
        "change",
        [
            lambda envelope: envelope["operation"]["identity"].update(vid=True),
            lambda envelope: envelope["operation"]["identity"].update(vid="100"),
            lambda envelope: envelope["operation"]["effects"].update(extra=1),
            lambda envelope: envelope["operation"]["effects"].pop("status"),
            lambda envelope: envelope["operation"].update(assumptions=[]),
            lambda envelope: envelope.update(v=2),
            lambda envelope: envelope.update(v=True),
            lambda envelope: envelope.update(extra="x"),
            lambda envelope: envelope.update(action="update"),
            lambda envelope: envelope.update(action="delete"),
            lambda envelope: envelope.update(resource="ip_address"),
        ],
        ids=[
            "bool-pk",
            "string-vid",
            "extra-field",
            "missing-field",
            "wrong-shape",
            "unknown-version",
            "bool-version",
            "extra-envelope-key",
            "other-action-schema",
            "action-outside-policy",
            "resource-mismatch",
        ],
    )
    def test_strict_decoding_refuses_every_other_shape(self, change):
        token = _resign(intent.offer(_create(), CONTEXT).token, change)
        rebuild = _Rebuild()

        result = _only(intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=rebuild))

        assert result.reason is intent.StaleReason.INVALID
        assert rebuild.calls == []

    @pytest.mark.parametrize(
        "token", ["", "vlan:", "vlan", "vlan:not-a-signature", "x" * (intent.MAX_TOKEN_LENGTH + 1)]
    )
    def test_missing_malformed_and_oversized_tokens_refuse(self, token):
        result = _only(intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))

        expected = intent.StaleReason.MISSING if token == "" else intent.StaleReason.INVALID
        assert result.reason is expected

    def test_no_token_is_missing(self):
        result = _only(intent.verify([], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))

        assert result == intent.Stale(intent.StaleReason.MISSING, (intent.FIELD,))

    def test_offer_refuses_a_value_that_is_not_its_exact_type(self):
        with pytest.raises(TypeError):
            intent.offer(_create(vid=True), CONTEXT)
        with pytest.raises(TypeError):
            intent.offer(_create(group="5"), CONTEXT)

    @pytest.mark.parametrize(
        "context",
        [(True, ("dcim.device", 1), "default"), (1, ("dcim.device", "1"), "default"), (1, ("dcim.device", 1), "")],
    )
    def test_a_context_needs_exact_types(self, context):
        with pytest.raises(TypeError):
            intent.IntentContext(*context)


class TestExpiry:
    @pytest.mark.parametrize(("age", "expired"), [(intent.MAX_AGE_SECONDS, False), (intent.MAX_AGE_SECONDS + 1, True)])
    def test_the_expiry_bound_is_exact(self, monkeypatch, age, expired):
        clock = _Clock(1_900_000_000)
        monkeypatch.setattr(signing, "time", clock)
        token = intent.offer(_create(), CONTEXT).token
        clock.now += age

        result = _only(intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))

        if expired:
            assert result == intent.Stale(intent.StaleReason.EXPIRED, (intent.FIELD,))
        else:
            assert isinstance(result, intent.Verified)


class TestContext:
    @pytest.mark.parametrize(
        ("context", "path"),
        [
            (intent.IntentContext(8, ("dcim.device", 11), "default"), "context.actor"),
            (intent.IntentContext(7, ("dcim.device", 12), "default"), "context.subject"),
            (intent.IntentContext(7, ("virtualization.virtualmachine", 11), "default"), "context.subject"),
            (intent.IntentContext(7, ("dcim.device", 11), "other"), "context.server"),
        ],
    )
    def test_a_token_of_another_context_refuses_before_any_rebuild(self, context, path):
        offered = intent.offer(_create(), CONTEXT)
        rebuild = _Rebuild()

        result = _only(intent.verify([offered.token], policy=POLICY, context=context, rebuild=rebuild))

        assert result == intent.Stale(intent.StaleReason.CONTEXT_CHANGED, (path,), (offered.operation,))
        assert rebuild.calls == []


class TestDuplicates:
    def test_a_repeated_token_refuses(self):
        token = intent.offer(_create(), CONTEXT).token
        rebuild = _Rebuild()

        result = _only(intent.verify([token, token], policy=POLICY, context=CONTEXT, rebuild=rebuild))

        assert result.reason is intent.StaleReason.INVALID
        assert rebuild.calls == []

    def test_two_tokens_for_one_identity_refuse_together(self):
        global_create = intent.offer(_create(group=None), CONTEXT)
        grouped_create = intent.offer(_create(group=5), CONTEXT)
        other = intent.offer(_create(vid=200), CONTEXT)
        rebuild = _Rebuild()

        verification = intent.verify(
            [global_create.token, grouped_create.token, other.token], policy=POLICY, context=CONTEXT, rebuild=rebuild
        )

        assert verification.results == (
            intent.Stale(
                intent.StaleReason.INVALID,
                ("identity.duplicate",),
                (global_create.operation, grouped_create.operation),
            ),
            intent.Verified((intent.Offer(other.token, other.operation),)),
        )
        assert rebuild.calls == [(other.operation,)]

    def test_two_tokens_of_one_operation_refuse_together(self, monkeypatch):
        clock = _Clock(1_900_000_000)
        monkeypatch.setattr(signing, "time", clock)
        first = intent.offer(_create(), CONTEXT).token
        clock.now += 1
        second = intent.offer(_create(), CONTEXT).token

        result = _only(intent.verify([first, second], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()))

        assert first != second
        assert result.reason is intent.StaleReason.INVALID
        assert result.paths == ("identity.duplicate",)


class TestComparison:
    def test_changed_facts_are_named_in_schema_order_once(self):
        approved = _update()
        changed = dataclasses.replace(
            approved,
            assumptions=VLANAssumptions(VLANTarget(9, 100, 5, "renamed")),
            effects=VLANUpdateEffects(9, "other"),
        )
        offered = intent.offer(approved, CONTEXT)

        result = _only(
            intent.verify(
                [offered.token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild({offered.operation: changed})
            )
        )

        assert result == intent.Stale(
            intent.StaleReason.ASSUMPTIONS_CHANGED,
            ("assumptions.target.name", "effects.name"),
            (approved,),
        )

    def test_a_rebuild_into_another_action_names_the_parts_that_differ(self):
        approved = _create(group=5)
        offered = intent.offer(approved, CONTEXT)
        rebuilt = _update(group=5, current="users", proposed="users")

        result = _only(
            intent.verify([offered.token], policy=POLICY, context=CONTEXT, rebuild=_Rebuild({approved: rebuilt}))
        )

        assert result.reason is intent.StaleReason.ASSUMPTIONS_CHANGED
        assert result.paths == ("assumptions.target", "effects")

    def test_an_unavailable_member_makes_its_group_stale(self):
        offered = intent.offer(_create(), CONTEXT)

        result = _only(
            intent.verify(
                [offered.token],
                policy=POLICY,
                context=CONTEXT,
                rebuild=_Rebuild({offered.operation: intent.Unavailable("source.row")}),
            )
        )

        assert result == intent.Stale(intent.StaleReason.UNAVAILABLE, ("source.row",), (offered.operation,))

    def test_a_group_without_a_primary_member_is_unreferenced(self):
        policy = intent.RoutePolicy(
            frozenset(
                {
                    (intent.Resource.VLAN, "create", intent.Role.PREREQUISITE),
                    (intent.Resource.VLAN, "update", intent.Role.PRIMARY),
                }
            )
        )
        offered = intent.offer(_create(), CONTEXT)

        result = _only(intent.verify([offered.token], policy=policy, context=CONTEXT, rebuild=_Rebuild()))

        assert result == intent.Stale(intent.StaleReason.INVALID, ("role.unreferenced",), (offered.operation,))

    @pytest.mark.parametrize(
        "groups",
        [lambda approved: [], lambda approved: [()], lambda approved: [((op, op),) for op in approved] * 2],
        ids=["drops-a-member", "empty-group", "repeats-a-member"],
    )
    def test_a_rebuild_that_does_not_cover_every_approval_once_is_a_programming_error(self, groups):
        token = intent.offer(_create(), CONTEXT).token

        with pytest.raises(TypeError):
            intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=groups)


class TestRegistry:
    def test_registration_refuses_an_unsupported_schema(self):
        @dataclasses.dataclass
        class Mutable:
            RESOURCE = intent.Resource.VLAN
            ACTION = "mutable"
            identity: VLANIdentity
            choices: VLANChoices
            assumptions: VLANAssumptions
            effects: VLANUpdateEffects

        @dataclasses.dataclass(frozen=True)
        class FreeDict:
            RESOURCE = intent.Resource.VLAN
            ACTION = "free_dict"
            identity: VLANIdentity
            choices: VLANChoices
            assumptions: dict
            effects: VLANUpdateEffects

        for cls in (Mutable, FreeDict, VLANCreate):
            with pytest.raises(TypeError):
                intent.register(cls)
        assert (intent.Resource.VLAN, "mutable") not in intent.registered_actions()
        assert (intent.Resource.VLAN, "free_dict") not in intent.registered_actions()

    def test_a_route_policy_names_registered_actions_with_one_role_each(self):
        with pytest.raises(ValueError):
            intent.RoutePolicy(frozenset({(intent.Resource.VLAN, "delete", intent.Role.PRIMARY)}))
        with pytest.raises(ValueError):
            intent.RoutePolicy(
                frozenset(
                    {
                        (intent.Resource.VLAN, "create", intent.Role.PRIMARY),
                        (intent.Resource.VLAN, "create", intent.Role.PREREQUISITE),
                    }
                )
            )
        with pytest.raises(ValueError):
            intent.RoutePolicy(frozenset())

    def test_signed_operations_read_authentic_tokens_without_the_context(self):
        offered = intent.offer(_create(group=5), intent.IntentContext(99, ("dcim.device", 1), "other"))

        assert intent.signed_operations([offered.token, "vlan:forged", ""], policy=POLICY) == (offered.operation,)
