"""
The selection draft of a sync tab, against the real cache and the real advisory lock.

Each test opens drafts as a page request does and changes them as the draft commands will; nothing is mocked.
Only the signing time is fixed, where a test needs two tokens of one payload from different seconds.
"""

from functools import partial
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.core.cache import cache
from django.db import connection

from netbox_librenms_plugin.selection_draft import (
    DRAFT_TTL_SECONDS,
    Assignment,
    DraftConflict,
    Intent,
    SelectionDraft,
    draft_key,
    evidence_changed,
    read_evidence,
    sign_evidence,
)
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_vm,
    run_in_threads,
    transactional_db_with_all_apps,
)

SCOPE = "interface_name_field=ifName&server_key=default"
EVIDENCE = {"action": "create", "target": {"address": "192.0.2.1/24", "vrf": None}}


def _user(username):
    return get_user_model().objects.create_user(username=username, password="x")


def _open(user, subject, generation="g1", **changes):
    arguments = {
        "tab": "ipaddresses",
        "variant": "device",
        "server_key": "default",
        "scope": SCOPE,
        "generation": generation,
        **changes,
    }
    return SelectionDraft.open(user, subject, **arguments)


def _intents(draft):
    return {row_key: entry.intent for row_key, entry in draft.entries().items()}


@pytest.mark.django_db
def test_a_new_draft_is_empty_and_every_open_returns_the_same_draft():
    user, device = _user("draft-open"), make_device("draft-open")

    draft = _open(user, device)

    assert (draft.revision, draft.entries(), draft.cleared) == (0, {}, False)
    again = _open(user, device)
    assert (again.draft_id, again.revision) == (draft.draft_id, 0)


@pytest.mark.django_db
def test_assignments_store_the_intent_and_the_approved_evidence_of_each_row():
    user, device = _user("draft-assign"), make_device("draft-assign")
    draft = _open(user, device)

    revision = draft.assign(
        draft.draft_id,
        0,
        {"ip-1": Assignment(Intent.ON, {"target": ("192.0.2.1/24",)}), "ip-2": Assignment(Intent.OFF)},
    )

    assert revision == 1
    stored = _open(user, device)
    assert stored.revision == 1
    assert _intents(stored) == {"ip-1": Intent.ON, "ip-2": Intent.OFF}
    # The draft keeps the payload in its JSON form, the form a verified token gives back.
    assert stored.entries()["ip-1"].evidence == {"target": ["192.0.2.1/24"]}
    assert stored.entries()["ip-2"].evidence is None
    assert {entry.row_rev for entry in stored.entries().values()} == {1}

    # "none" removes the row; an assignment is absolute, so a repeated one changes nothing else.
    assert stored.assign(stored.draft_id, 1, {"ip-1": Assignment(Intent.NONE), "ip-2": Assignment(Intent.OFF)}) == 2
    assert _intents(_open(user, device)) == {"ip-2": Intent.OFF}


@pytest.mark.django_db
def test_only_a_selected_row_carries_evidence():
    with pytest.raises(ValueError):
        Assignment(Intent.OFF, EVIDENCE)


@pytest.mark.django_db
def test_a_mutation_of_another_revision_or_draft_conflicts_and_changes_nothing():
    user, device = _user("draft-conflict"), make_device("draft-conflict")
    window_a = _open(user, device)
    window_b = _open(user, device)
    window_b.assign(window_b.draft_id, 0, {"ip-1": Assignment(Intent.ON, EVIDENCE)})

    with pytest.raises(DraftConflict):
        window_a.assign(window_a.draft_id, 0, {"ip-2": Assignment(Intent.ON, EVIDENCE)})
    with pytest.raises(DraftConflict):
        window_a.choose("another-draft", 1, "ip-2", "vrf", "7")

    stored = _open(user, device)
    assert (stored.revision, _intents(stored)) == (1, {"ip-1": Intent.ON})


@pytest.mark.django_db
def test_a_choice_is_kept_without_an_intent_and_removing_the_last_choice_removes_the_row():
    user, device = _user("draft-choice"), make_device("draft-choice")
    draft = _open(user, device)

    draft.choose(draft.draft_id, 0, "ip-1", "vrf", "7")
    draft.assign(draft.draft_id, 1, {"ip-1": Assignment(Intent.ON, EVIDENCE)})
    draft.assign(draft.draft_id, 2, {"ip-1": Assignment(Intent.NONE)})

    entry = _open(user, device).entries()["ip-1"]
    assert (entry.intent, dict(entry.choices), entry.evidence, entry.row_rev) == (Intent.NONE, {"vrf": "7"}, None, 3)
    draft.choose(draft.draft_id, 3, "ip-1", "vrf", None)
    assert _open(user, device).entries() == {}


@pytest.mark.django_db
def test_another_generation_or_fingerprint_starts_an_empty_draft_and_reports_it_once():
    user, device = _user("draft-generation"), make_device("draft-generation")
    draft = _open(user, device, fingerprint="f1")
    draft.assign(draft.draft_id, 0, {"ip-1": Assignment(Intent.ON, EVIDENCE)})

    refreshed = _open(user, device, generation="g2", fingerprint="f1")

    assert refreshed.draft_id != draft.draft_id
    assert (refreshed.revision, refreshed.entries(), refreshed.cleared) == (0, {}, True)
    assert _open(user, device, generation="g2", fingerprint="f1").cleared is False
    # The old draft id cannot change the new draft.
    with pytest.raises(DraftConflict):
        draft.assign(draft.draft_id, 1, {"ip-2": Assignment(Intent.ON, EVIDENCE)})

    refreshed.assign(refreshed.draft_id, 0, {"ip-3": Assignment(Intent.OFF)})
    replaced = _open(user, device, generation="g2", fingerprint="f2")
    assert (replaced.entries(), replaced.cleared) == ({}, True)
    # A draft with no rows is replaced without a notice.
    assert _open(user, device, generation="g3", fingerprint="f2").cleared is False


@pytest.mark.django_db
def test_each_user_subject_tab_variant_server_and_scope_has_its_own_draft():
    user, device = _user("draft-keys"), make_device("draft-keys")
    draft = _open(user, device)
    draft.assign(draft.draft_id, 0, {"ip-1": Assignment(Intent.ON, EVIDENCE)})

    others = [
        _open(_user("draft-keys-other"), device),
        _open(user, make_device("draft-keys-other")),
        _open(user, make_vm("draft-keys-vm")),
        _open(user, device, tab="interfaces"),
        _open(user, device, variant="virtual-chassis"),
        _open(user, device, server_key="secondary"),
        _open(user, device, scope="interface_name_field=ifDescr&server_key=default"),
    ]

    assert all(other.entries() == {} and other.draft_id != draft.draft_id for other in others)
    assert _intents(_open(user, device)) == {"ip-1": Intent.ON}


@pytest.mark.django_db
def test_a_draft_belongs_to_a_signed_in_user():
    with pytest.raises(ValueError):
        _open(AnonymousUser(), make_device("draft-anonymous"))


@pytest.mark.django_db
def test_each_open_starts_the_draft_lifetime_again():
    user, device = _user("draft-ttl"), make_device("draft-ttl")
    _open(user, device)
    key = draft_key(user, device, "ipaddresses", "device", "default", SCOPE)
    cache.touch(key, 60)

    _open(user, device)

    assert cache.ttl(key) > DRAFT_TTL_SECONDS - 60


@pytest.mark.django_db
def test_a_claim_takes_every_entry_once_and_fences_the_claimed_rows():
    user, device = _user("draft-claim"), make_device("draft-claim")
    draft = _open(user, device)
    draft.choose(draft.draft_id, 0, "ip-1", "vrf", "7")
    draft.assign(draft.draft_id, 1, {"ip-1": Assignment(Intent.ON, EVIDENCE), "ip-2": Assignment(Intent.OFF)})

    claim = draft.claim(draft.draft_id, 2)

    assert (claim.draft_id, claim.revision) == (draft.draft_id, 3)
    assert {row_key: entry.intent for row_key, entry in claim.entries.items()} == {
        "ip-1": Intent.ON,
        "ip-2": Intent.OFF,
    }
    assert (claim.entries["ip-1"].evidence, dict(claim.entries["ip-1"].choices)) == (EVIDENCE, {"vrf": "7"})
    assert dict(claim.fences) == {"ip-1": 3, "ip-2": 3}
    assert _open(user, device).entries() == {}
    # A second submit of the same revision claims nothing.
    with pytest.raises(DraftConflict):
        draft.claim(draft.draft_id, 2)


@pytest.mark.django_db
def test_restore_puts_back_only_refused_rows_that_nothing_changed_since_the_claim():
    user, device = _user("draft-restore"), make_device("draft-restore")
    draft = _open(user, device)
    rows = {f"ip-{index}": Assignment(Intent.ON, {"row": index}) for index in range(1, 5)}
    draft.assign(draft.draft_id, 0, rows)
    claim = draft.claim(draft.draft_id, 1)
    # During the writes, another window selects ip-2 again and changes a choice of ip-3.
    window = _open(user, device)
    window.assign(window.draft_id, 2, {"ip-2": Assignment(Intent.ON, {"row": "new"})})
    window.choose(window.draft_id, 3, "ip-3", "vrf", "9")

    restored = draft.restore(claim, ["ip-1", "ip-2", "ip-3"])

    assert restored == {"ip-1"}
    stored = _open(user, device)
    assert stored.revision == 5
    assert {row_key: entry.evidence for row_key, entry in stored.entries().items()} == {
        "ip-1": {"row": 1},
        "ip-2": {"row": "new"},
        "ip-3": None,
    }
    assert stored.entries()["ip-1"].row_rev == 5
    # A restore moves the fence too, so the same claim cannot put a row back twice.
    assert draft.restore(claim, ["ip-1"]) == frozenset()
    with pytest.raises(KeyError):
        draft.restore(claim, ["ip-9"])


@pytest.mark.django_db
def test_a_later_claim_of_a_row_moves_its_fence_past_an_earlier_claim():
    user, device = _user("draft-two-claims"), make_device("draft-two-claims")
    draft = _open(user, device)
    draft.assign(draft.draft_id, 0, {"ip-1": Assignment(Intent.ON, {"row": 1})})
    first = draft.claim(draft.draft_id, 1)
    draft.restore(first, ["ip-1"])
    second = draft.claim(draft.draft_id, 3)

    assert draft.restore(first, ["ip-1"]) == frozenset()
    assert draft.restore(second, ["ip-1"]) == {"ip-1"}


@pytest.mark.django_db
def test_nothing_is_restored_into_a_draft_that_a_refresh_replaced():
    user, device = _user("draft-restore-reset"), make_device("draft-restore-reset")
    draft = _open(user, device)
    draft.assign(draft.draft_id, 0, {"ip-1": Assignment(Intent.ON, EVIDENCE)})
    claim = draft.claim(draft.draft_id, 1)
    refreshed = _open(user, device, generation="g2")

    assert draft.restore(claim, ["ip-1"]) == frozenset()
    assert _open(user, device, generation="g2").entries() == {}
    assert refreshed.revision == 0


@transactional_db_with_all_apps()
def test_concurrent_assignments_of_one_revision_apply_once():
    user, device = _user("draft-race"), make_device("draft-race")
    draft_id = _open(user, device).draft_id
    calls = 8
    barrier = Barrier(calls, timeout=10)

    def assign(index):
        try:
            draft = _open(user, device)
            barrier.wait()
            try:
                draft.assign(draft_id, 0, {f"ip-{index}": Assignment(Intent.OFF)})
            except DraftConflict:
                return False
            return True
        finally:
            connection.close()

    results = run_in_threads(*(partial(assign, index) for index in range(calls)), barriers=(barrier,), timeout=30)

    assert results.count(True) == 1
    stored = _open(user, device)
    assert stored.revision == 1
    assert len(stored.entries()) == 1


def test_an_evidence_token_gives_back_its_payload_only_for_its_tab():
    token = sign_evidence("ipaddresses", EVIDENCE)

    assert read_evidence("ipaddresses", token) == EVIDENCE
    with pytest.raises(signing.BadSignature):
        read_evidence("cables", token)
    with pytest.raises(signing.BadSignature):
        read_evidence("ipaddresses", token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1])


def test_evidence_signed_in_another_second_is_unchanged_and_other_facts_are_changed(monkeypatch):
    def sign_at(seconds):
        monkeypatch.setattr(signing.TimestampSigner, "timestamp", lambda self: signing.b62_encode(seconds))
        return sign_evidence("ipaddresses", EVIDENCE)

    earlier, later = sign_at(1_700_000_000), sign_at(1_700_000_001)

    assert earlier != later
    approved = read_evidence("ipaddresses", earlier)
    assert not evidence_changed(approved, read_evidence("ipaddresses", later))
    # Key order and tuples do not count; another destination does.
    assert not evidence_changed(approved, {"target": {"vrf": None, "address": "192.0.2.1/24"}, "action": "create"})
    assert evidence_changed(approved, {**EVIDENCE, "target": {"address": "192.0.2.1/24", "vrf": 7}})
    assert evidence_changed(None, EVIDENCE)
