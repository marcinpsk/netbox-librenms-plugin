"""
The selection draft of one sync tab: the rows a user selected or excluded, and the choices set on rows.

The server keeps the draft in the Django cache, under the user, the sync subject, the tab, the table
variant, the LibreNMS server and the selection scope. A draft is bound to the snapshot generation (and an
optional fingerprint) it was opened for, so a data refresh starts an empty draft with a new id. Every
mutation is a compare-and-set on the draft id and its revision under one advisory lock: a stale writer
changes nothing and gets :class:`DraftConflict`.

Each row entry has a tri-state intent, its choices, and the evidence payload the user approved: the facts
of the row's signed evidence token at the time of the user's check. A token is compared by its payload
(:func:`evidence_changed`), never by its bytes, because a new signing time gives new bytes.
"""

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType

from django.core import signing
from django.core.cache import cache
from django.db import transaction

from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

# Each read and write of a draft starts this lifetime again.
DRAFT_TTL_SECONDS = 8 * 60 * 60
_KEY_PREFIX = "librenms_selection_draft"
_EVIDENCE_SALT = "netbox_librenms_plugin.selection_evidence"


class Intent(StrEnum):
    """What the user asked for one row: select it, exclude it, or nothing."""

    ON = "on"
    OFF = "off"
    NONE = "none"


class DraftConflict(Exception):
    """A mutation named another draft id or revision than the stored draft, so nothing changed."""


@dataclass(frozen=True)
class Assignment:
    """One row's new intent; a selection carries the evidence payload of the token the user checked."""

    intent: Intent
    evidence: dict | None = None

    def __post_init__(self):
        """Reject evidence on a row that is not selected: only a selection approves facts."""
        Intent(self.intent)
        if self.evidence is not None and self.intent != Intent.ON:
            raise ValueError("Only a selected row carries evidence.")


@dataclass(frozen=True)
class Entry:
    """One row of the draft. ``row_rev`` is the draft revision of the row's last change."""

    intent: Intent
    choices: MappingProxyType
    evidence: dict | None
    row_rev: int


@dataclass(frozen=True)
class Claim:
    """The entries one submit took from the draft, with the fence that each claimed row got."""

    draft_id: str
    revision: int
    entries: MappingProxyType
    fences: MappingProxyType


def draft_key(user, subject, tab, variant, server_key, scope):
    """
    Return the cache key of one user's draft for one table of a sync tab.

    Args:
        user (User): The signed-in user who owns the draft.
        subject (Device | VirtualMachine): The object of the sync page.
        tab (str): The sync tab, a :class:`SyncTab` value.
        variant (str): The table variant of the tab.
        server_key (str): The LibreNMS server of the tab.
        scope (str): The selection scope of the tab (``sync_filters.selection_scope``).

    Returns:
        str: The key.

    Raises:
        ValueError: If the user is not signed in, or the tab is not a sync tab.

    """
    if user.pk is None:
        raise ValueError("A selection draft belongs to a signed-in user.")
    parts = [user.pk, subject._meta.label_lower, subject.pk, SyncTab(tab).value, variant, server_key, scope]
    return f"{_KEY_PREFIX}:{sha256(json.dumps(parts).encode()).hexdigest()}"


@contextmanager
def _locked(key):
    """Hold the draft's advisory lock for one short transaction."""
    with transaction.atomic():
        acquire_advisory_transaction_lock(key)
        yield


def _new_record(generation, fingerprint):
    return {
        "draft_id": secrets.token_hex(16),
        "generation": generation,
        "fingerprint": fingerprint,
        "revision": 0,
        "entries": {},
        "fences": {},
    }


def _is_bound(record, generation, fingerprint):
    return record is not None and record["generation"] == generation and record["fingerprint"] == fingerprint


def _entry(record, row_key):
    stored = record["entries"][row_key]
    return Entry(
        intent=Intent(stored["intent"]),
        choices=MappingProxyType(dict(stored["choices"])),
        evidence=stored["evidence"],
        row_rev=record["fences"][row_key],
    )


def _stored(entry):
    return {"intent": entry.intent.value, "choices": dict(entry.choices), "evidence": entry.evidence}


class SelectionDraft:
    """One user's draft for one table of a sync tab; open it with :meth:`open`."""

    def __init__(self, key, record, cleared):
        """Hold the draft read from the cache; use :meth:`open` instead."""
        self._key = key
        self._record = record
        self.cleared = cleared

    @classmethod
    def open(cls, user, subject, tab, variant, server_key, scope, generation, fingerprint=None):
        """
        Return the user's draft for the generation, and start an empty one when the stored draft is for another.

        ``cleared`` is True only on the open that replaced a draft that had entries, so the page that
        opened it can tell the user once.

        Args:
            user (User): The signed-in user who owns the draft.
            subject (Device | VirtualMachine): The object of the sync page.
            tab (str): The sync tab, a :class:`SyncTab` value.
            variant (str): The table variant of the tab.
            server_key (str): The LibreNMS server of the tab.
            scope (str): The selection scope of the tab.
            generation (str): The snapshot generation that the rows come from.
            fingerprint (str | None): Other data the rows depend on; another value starts an empty draft too.

        Returns:
            SelectionDraft: The draft.

        """
        key = draft_key(user, subject, tab, variant, server_key, scope)
        record = cache.get(key)
        if _is_bound(record, generation, fingerprint):
            cache.touch(key, DRAFT_TTL_SECONDS)
            return cls(key, record, cleared=False)
        with _locked(key):
            # Another request can have replaced it while this one waited for the lock.
            record = cache.get(key)
            if _is_bound(record, generation, fingerprint):
                cache.touch(key, DRAFT_TTL_SECONDS)
                return cls(key, record, cleared=False)
            cleared = bool(record and record["entries"])
            record = _new_record(generation, fingerprint)
            cache.set(key, record, DRAFT_TTL_SECONDS)
        return cls(key, record, cleared)

    @property
    def draft_id(self):
        """Return the random id of this draft; a replaced draft has a new one."""
        return self._record["draft_id"]

    @property
    def revision(self):
        """Return the number of mutations of this draft."""
        return self._record["revision"]

    def entries(self):
        """Return the rows of the draft by row key."""
        return {row_key: _entry(self._record, row_key) for row_key in self._record["entries"]}

    def assign(self, draft_id, expected_revision, assignments):
        """
        Set the intent of each row, and return the new revision.

        Args:
            draft_id (str): The draft id the client read.
            expected_revision (int): The revision the client read.
            assignments (dict[str, Assignment]): The new intent of each row key.

        Raises:
            DraftConflict: If the stored draft has another id or revision.

        """

        def change(entries):
            for row_key, assignment in assignments.items():
                entry = entries.get(row_key, {"choices": {}})
                entry["intent"] = Intent(assignment.intent).value
                # Canonical JSON, so the stored payload compares like the one a new assessment builds.
                entry["evidence"] = None if assignment.evidence is None else json.loads(_canonical(assignment.evidence))
                _put(entries, row_key, entry)
            return assignments.keys()

        return self._mutate(draft_id, expected_revision, change)

    def choose(self, draft_id, expected_revision, row_key, field, value):
        """
        Set one choice of a row, or remove it when *value* is None, and return the new revision.

        Raises:
            DraftConflict: If the stored draft has another id or revision.

        """

        def change(entries):
            entry = entries.get(row_key, {"intent": Intent.NONE.value, "choices": {}, "evidence": None})
            if value is None:
                entry["choices"].pop(field, None)
            else:
                entry["choices"][field] = value
            _put(entries, row_key, entry)
            return [row_key]

        return self._mutate(draft_id, expected_revision, change)

    def claim(self, draft_id, expected_revision):
        """
        Take every entry out of the draft for one submit, and move the fence of each claimed row.

        Raises:
            DraftConflict: If the stored draft has another id or revision, also for a second claim of a revision.

        """
        claimed = {}

        def change(entries):
            claimed.update(entries)
            entries.clear()
            return claimed.keys()

        revision = self._mutate(draft_id, expected_revision, change)
        record = {"entries": claimed, "fences": dict.fromkeys(claimed, revision)}
        return Claim(
            draft_id=draft_id,
            revision=revision,
            entries=MappingProxyType({row_key: _entry(record, row_key) for row_key in claimed}),
            fences=MappingProxyType(record["fences"]),
        )

    def restore(self, claim, row_keys):
        """
        Put back the claimed rows of *row_keys* that nothing changed since the claim, and return them.

        A row is put back only into the same draft, and only while its fence is still the claim's: another
        change, claim or restore of the row moved it. Nothing is put back into a replaced draft.

        Args:
            claim (Claim): The claim that took the rows.
            row_keys (Iterable[str]): Rows of the claim that were not written.

        Returns:
            frozenset[str]: The rows put back.

        Raises:
            KeyError: If a row key is not in the claim.

        """
        row_keys = list(row_keys)
        if unknown := set(row_keys) - claim.fences.keys():
            raise KeyError(f"The claim has no rows {sorted(unknown)}.")
        with _locked(self._key):
            record = cache.get(self._key)
            if record is None or record["draft_id"] != claim.draft_id:
                return frozenset()
            restored = [row_key for row_key in row_keys if record["fences"].get(row_key) == claim.fences[row_key]]
            if not restored:
                return frozenset()
            revision = record["revision"] + 1
            for row_key in restored:
                record["entries"][row_key] = _stored(claim.entries[row_key])
                record["fences"][row_key] = revision
            record["revision"] = revision
            cache.set(self._key, record, DRAFT_TTL_SECONDS)
        self._record = record
        return frozenset(restored)

    def _mutate(self, draft_id, expected_revision, change):
        """Apply *change* to the stored entries when the draft is still the client's; it returns the changed keys."""
        with _locked(self._key):
            record = cache.get(self._key)
            if record is None or record["draft_id"] != draft_id or record["revision"] != expected_revision:
                raise DraftConflict(f"The draft is not {draft_id} at revision {expected_revision}.")
            revision = record["revision"] + 1
            for row_key in list(change(record["entries"])):
                record["fences"][row_key] = revision
            record["revision"] = revision
            cache.set(self._key, record, DRAFT_TTL_SECONDS)
        self._record = record
        return revision


def _put(entries, row_key, entry):
    """Store a row's entry, or drop it when it holds neither an intent nor a choice."""
    if entry["intent"] == Intent.NONE and not entry["choices"]:
        entries.pop(row_key, None)
    else:
        entries[row_key] = entry


def _evidence_salt(tab):
    return f"{_EVIDENCE_SALT}.{SyncTab(tab).value}"


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sign_evidence(tab, payload):
    """Return the signed evidence token of one row of the tab, for the rendered row."""
    return signing.dumps(payload, salt=_evidence_salt(tab), compress=True)


def read_evidence(tab, token):
    """
    Return the payload of an evidence token that :func:`sign_evidence` made for the tab.

    Raises:
        signing.BadSignature: If the token is not one, also when it was made for another tab.

    """
    return signing.loads(token, salt=_evidence_salt(tab))


def evidence_changed(approved, current):
    """Return whether the current evidence payload of a row is not the one the user approved."""
    return _canonical(approved) != _canonical(current)
