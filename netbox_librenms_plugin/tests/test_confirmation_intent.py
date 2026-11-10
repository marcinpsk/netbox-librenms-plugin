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
    """Answer each approved operation with the operation it maps to, in *schedule* order, and record every call."""

    def __init__(self, rebuilt=None, schedule=None):
        self.rebuilt = rebuilt or {}
        self.schedule = schedule
        self.calls = []

    def __call__(self, approved):
        self.calls.append(approved)
        ordered = sorted(approved, key=self.schedule) if self.schedule else approved
        return [(operation, self.rebuilt.get(operation, operation)) for operation in ordered]


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
            # The signature still holds, so the expired operation is known and refuses its dependency group.
            assert result == intent.Stale(intent.StaleReason.EXPIRED, (intent.FIELD,), (_create(),))
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

    @pytest.mark.parametrize(
        "change",
        [
            lambda envelope: envelope.update(subject=["dcim.device", True]),
            lambda envelope: envelope.update(subject=["dcim.device", 11.0]),
            lambda envelope: envelope.update(subject=["dcim.device", "11"]),
            lambda envelope: envelope.update(subject=[7, 11]),
            lambda envelope: envelope.update(subject=["", 11]),
            lambda envelope: envelope.update(subject=["dcim.device"]),
            lambda envelope: envelope.update(subject="dcim.device:11"),
            lambda envelope: envelope.update(actor=True),
            lambda envelope: envelope.update(actor=7.0),
            lambda envelope: envelope.update(server=5),
            lambda envelope: envelope.update(server=""),
        ],
        ids=[
            "bool-subject-pk",
            "float-subject-pk",
            "string-subject-pk",
            "non-string-label",
            "empty-label",
            "short-subject",
            "flat-subject",
            "bool-actor",
            "float-actor",
            "non-string-server",
            "empty-server",
        ],
    )
    def test_a_malformed_context_field_is_invalid(self, change):
        # Each context compares equal by value (True == 1, 11.0 == 11), so only an exact type check refuses it.
        offered = intent.offer(_create(), CONTEXT)
        token = _resign(offered.token, change)
        rebuild = _Rebuild()

        for context in (CONTEXT, intent.IntentContext(1, ("dcim.device", 1), "default")):
            result = _only(intent.verify([token], policy=POLICY, context=context, rebuild=rebuild))
            assert result == intent.Stale(intent.StaleReason.INVALID, (intent.FIELD,))
        assert rebuild.calls == []


class TestDuplicates:
    def test_a_repeated_token_refuses(self):
        token = intent.offer(_create(), CONTEXT).token
        rebuild = _Rebuild()

        result = _only(intent.verify([token, token], policy=POLICY, context=CONTEXT, rebuild=rebuild))

        assert result.reason is intent.StaleReason.INVALID
        assert rebuild.calls == []

    def test_a_repeated_token_still_conflicts_with_another_token_of_its_identity(self):
        first = intent.offer(_create(group=None), CONTEXT)
        second = intent.offer(_create(group=5), CONTEXT)
        other = intent.offer(_create(vid=200), CONTEXT)
        rebuild = _Rebuild()

        verification = intent.verify(
            [first.token, first.token, second.token, other.token], policy=POLICY, context=CONTEXT, rebuild=rebuild
        )

        assert verification.results == (
            intent.Stale(
                intent.StaleReason.INVALID,
                (f"{intent.FIELD}.duplicate", "identity.duplicate"),
                (first.operation, second.operation),
            ),
            intent.Verified((intent.Offer(other.token, other.operation),)),
        )
        assert rebuild.calls == [(other.operation,)]

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
        [lambda approved: [], lambda approved: [()], lambda approved: [(op, op) for op in approved] * 2],
        ids=["drops-a-member", "malformed-pair", "repeats-a-member"],
    )
    def test_a_rebuild_that_does_not_cover_every_approval_once_is_a_programming_error(self, groups):
        token = intent.offer(_create(), CONTEXT).token

        with pytest.raises(TypeError):
            intent.verify([token], policy=POLICY, context=CONTEXT, rebuild=groups)


class TestDependencyGroups:
    """Operations that share a claim that one of them writes verify, and refuse, as one group."""

    # Rename VID 100 from users to staff, which frees the name that the create of VID 200 takes.
    RENAME = _update(vid=100, group=5, current="users", proposed="staff", pk=9)
    CREATE = _create(vid=200, group=5, name="users")
    OTHER = _create(vid=300, group=5, name="printers")

    def _offers(self, *operations):
        return [intent.offer(operation, CONTEXT) for operation in operations]

    def test_a_shared_written_claim_joins_one_group_in_the_writer_schedule(self):
        create, rename, other = self._offers(self.CREATE, self.RENAME, self.OTHER)
        # The writer renames before it creates.
        rebuild = _Rebuild(schedule=lambda operation: isinstance(operation, VLANCreate))

        verification = intent.verify(
            [create.token, rename.token, other.token], policy=POLICY, context=CONTEXT, rebuild=rebuild
        )

        assert verification.results == (
            intent.Verified((intent.Offer(rename.token, self.RENAME), intent.Offer(create.token, self.CREATE))),
            intent.Verified((intent.Offer(other.token, self.OTHER),)),
        )

    def test_a_stale_member_refuses_its_whole_group_and_no_other(self):
        rename, create, other = self._offers(self.RENAME, self.CREATE, self.OTHER)
        changed = dataclasses.replace(self.CREATE, effects=VLANCreateEffects(200, 5, "guests", "active"))

        verification = intent.verify(
            [rename.token, create.token, other.token],
            policy=POLICY,
            context=CONTEXT,
            rebuild=_Rebuild({self.CREATE: changed}),
        )

        assert verification.results == (
            intent.Stale(intent.StaleReason.ASSUMPTIONS_CHANGED, ("effects.name",), (self.RENAME, self.CREATE)),
            intent.Verified((intent.Offer(other.token, self.OTHER),)),
        )

    def test_an_unavailable_member_refuses_its_whole_group(self):
        rename, create = self._offers(self.RENAME, self.CREATE)

        (result,) = intent.verify(
            [rename.token, create.token],
            policy=POLICY,
            context=CONTEXT,
            rebuild=_Rebuild({self.CREATE: intent.Unavailable("source.row")}),
        ).results

        assert result == intent.Stale(intent.StaleReason.UNAVAILABLE, ("source.row",), (self.RENAME, self.CREATE))

    def test_operations_that_only_read_a_claim_stay_independent(self):
        first = _update(vid=100, group=5, current="users", proposed="users", pk=9)
        second = _update(vid=101, group=5, current="voice", proposed="voice", pk=10)
        offers = self._offers(first, second)

        verification = intent.verify(
            [offer.token for offer in offers], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()
        )

        assert [len(result.members) for result in verification.results] == [1, 1]

    @pytest.mark.parametrize("refusal", ["context", "expired", "repeated"])
    def test_a_refused_member_refuses_its_whole_group_before_any_rebuild(self, monkeypatch, refusal):
        clock = _Clock(1_900_000_000)
        monkeypatch.setattr(signing, "time", clock)
        rename = intent.offer(self.RENAME, CONTEXT)
        if refusal == "expired":
            clock.now -= intent.MAX_AGE_SECONDS + 1
        create = intent.offer(
            self.CREATE, intent.IntentContext(8, ("dcim.device", 11), "default") if refusal == "context" else CONTEXT
        )
        clock.now = 1_900_000_000
        tokens = [rename.token, create.token] + ([create.token] if refusal == "repeated" else [])
        rebuild = _Rebuild()

        (result,) = intent.verify(tokens, policy=POLICY, context=CONTEXT, rebuild=rebuild).results

        assert result.members == (self.RENAME, self.CREATE)
        assert (
            result.reason
            is {
                "context": intent.StaleReason.CONTEXT_CHANGED,
                "expired": intent.StaleReason.EXPIRED,
                "repeated": intent.StaleReason.INVALID,
            }[refusal]
        )
        assert rebuild.calls == []

    @pytest.mark.parametrize("order", ["first-takes", "first-releases"])
    def test_a_member_that_releases_a_name_runs_before_the_member_that_takes_it(self, order):
        # VID 100 takes beta, which VID 200 releases for gamma.
        takes = _update(vid=100, group=5, current="alpha", proposed="beta", pk=9)
        releases = _update(vid=200, group=5, current="beta", proposed="gamma", pk=10)
        offers = self._offers(takes, releases) if order == "first-takes" else self._offers(releases, takes)

        (result,) = intent.verify(
            [offer.token for offer in offers], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()
        ).results

        assert [member.operation for member in result.members] == [releases, takes]

    def test_a_cycle_of_names_refuses_its_group_with_a_named_path(self):
        # A swap needs a temporary name that nobody approved.
        first = _update(vid=100, group=5, current="alpha", proposed="beta", pk=9)
        second = _update(vid=200, group=5, current="beta", proposed="alpha", pk=10)
        other = _create(vid=300, group=5, name="printers")
        offers = self._offers(first, second, other)

        verification = intent.verify(
            [offer.token for offer in offers], policy=POLICY, context=CONTEXT, rebuild=_Rebuild()
        )

        assert verification.results == (
            intent.Stale(intent.StaleReason.INVALID, ("schedule.cycle",), (first, second)),
            intent.Verified((intent.Offer(offers[2].token, other),)),
        )

    def test_an_operation_without_claims_cannot_register(self):
        @dataclasses.dataclass(frozen=True)
        class Unclaimed:
            RESOURCE = intent.Resource.VLAN
            ACTION = "unclaimed"
            identity: VLANIdentity
            choices: VLANChoices
            assumptions: VLANAssumptions
            effects: VLANUpdateEffects

        with pytest.raises(TypeError):
            intent.register(Unclaimed)


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


class TestMechanicalGuards:
    """The opengrep allowlists of the confirmation guards only shrink, and name only files that still need them."""

    # The legacy signing sites of the actions that later increments migrate.
    LEGACY_SIGNING_SITES = frozenset(
        {
            "**/netbox_librenms_plugin/views/sync/ip_addresses.py",
            "**/netbox_librenms_plugin/module_grants.py",
            "**/netbox_librenms_plugin/identity_replacement.py",
        }
    )

    @staticmethod
    def _rule(rule_id):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[2]
        rules = yaml.safe_load((root / ".opengrep" / "librenms-rules.yaml").read_text())["rules"]
        return next(rule for rule in rules if rule["id"] == rule_id)

    def test_the_signing_allowlist_holds_only_the_module_tests_and_unmigrated_legacy_sites(self):
        from pathlib import Path

        excluded = set(self._rule("signing-only-in-confirmation-intent")["paths"]["exclude"])
        own = {"**/netbox_librenms_plugin/tests/**", "**/netbox_librenms_plugin/confirmation_intent.py"}
        legacy = excluded - own

        assert own <= excluded
        assert legacy <= self.LEGACY_SIGNING_SITES
        package = Path(intent.__file__).resolve().parent
        for pattern in legacy:
            source = (package / pattern.removeprefix("**/netbox_librenms_plugin/")).read_text()
            # A migrated site leaves the allowlist in the same change.
            assert "from django.core import signing" in source, pattern

    def test_offers_are_allowed_only_in_assessment_modules(self):
        excluded = set(self._rule("offer-only-from-assessment")["paths"]["exclude"])

        assert excluded == {"**/netbox_librenms_plugin/assessment/**", "**/netbox_librenms_plugin/tests/**"}
