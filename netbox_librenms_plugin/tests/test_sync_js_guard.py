"""
Guard the sync page scripts: selection state, HTML writes and fetch calls stay where the design puts them.

A converted tab keeps its selection in the server draft, and only the draft writer of sync_draft.js sends
requests with fetch(). The tabs that are not converted yet keep their old code, named in the allowlists
below; each allowlist must equal what the scripts hold, so it shrinks as the tabs convert.

Limits: the scan reads one top-level statement at a time (a named function, a variable, or an anonymous
listener keyed by its event and the first selector it matches). It does not parse JavaScript, so a use
through an alias (``const store = window.sessionStorage``) is not seen.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "static" / "netbox_librenms_plugin" / "js"
SYNC_SCRIPT = SCRIPTS / "librenms_sync.js"
DRAFT_SCRIPT = SCRIPTS / "sync_draft.js"

PATTERNS = {
    "storage": re.compile(r"\b(?:sessionStorage|localStorage)\b"),
    "html": re.compile(r"\.(?:innerHTML|outerHTML)\s*\+?=(?!=)|\binsertAdjacentHTML\("),
    "fetch": re.compile(r"\bfetch\("),
}
_HEAD = re.compile(
    r"(?:async\s+)?function\s+(?P<function>\w+)"
    r"|(?:const|let|var)\s+(?P<variable>\w+)\s*="
    r"|(?P<target>document|window|document\.body)\.addEventListener\(\s*'(?P<event>[^']+)'"
)
_SELECTOR = re.compile(r"\.(?:closest|matches)\(\s*'([^']+)'")

# The unconverted tabs' code and the page's UI preferences; B2 and B3 take the tabs' entries out.
SYNC_SCRIPT_ALLOWED = {
    "fetch": {
        "checkSyncCacheStatus",
        "deleteSelectedInterfaces",
        "document:click .lag-sync-btn, .parent-sync-btn, .bridge-sync-btn",
        "handleCableChange",
        "handleInterfaceChange",
        "handleModuleChange",
        "initializeVCReportButtons",
        "initializeVlanModalSave",
        "saveInterfaceNameFieldPreference",
        "sendSyncOptionPreference",
        "verifyVlanInGroup",
        "verifyVlanSyncGroup",
    },
    "storage": {
        "pruneOtherSelectionScopes",
        "readFilterDisclosureState",
        "readStoredSelection",
        "writeFilterDisclosureState",
        "writeStoredSelection",
    },
    "html": {
        "deleteSelectedInterfaces",
        "document:click .lag-sync-btn, .parent-sync-btn, .bridge-sync-btn",
        "handleCableChange",
        "handleInterfaceChange",
        "handleModuleChange",
        "initializeSyncFormSpinners",
        "initializeVCReportButtons",
        "initializeVlanModalSave",
        "openVlanDetailModal",
        "syncConfirmModal",
        "verifyVlanInGroup",
        "verifyVlanSyncGroup",
        "wireCopyButton",
    },
}
# The draft writer sends its commands from one function.
DRAFT_SCRIPT_ALLOWED = {"fetch": {"sendWrite"}, "storage": set(), "html": set()}


def _statement_key(lines, start, indent):
    """Return the name of the top-level statement that starts at line *start*."""
    match = _HEAD.match(lines[start][indent:])
    if match is None:
        return f"line {start + 1}: {lines[start].strip()[:60]}"
    if match["function"] or match["variable"]:
        return match["function"] or match["variable"]
    selector = next((found.group(1) for line in lines[start : start + 4] if (found := _SELECTOR.search(line))), "")
    return f"{match['target']}:{match['event']} {selector}".strip()


def scan(source, indent):
    """
    Return, per pattern, the top-level statements whose code uses it.

    Args:
        source (str): The script.
        indent (int): The indent of the script's top-level statements (4 inside an IIFE).

    Returns:
        dict[str, set[str]]: The statement keys per pattern name.

    """
    lines = source.split("\n")
    found = defaultdict(set)
    current = None
    for number, line in enumerate(lines):
        stripped = line.strip()
        top_level = line.startswith(" " * indent) and not line[indent : indent + 1].isspace()
        if top_level and stripped and not stripped.startswith(("}", ")", "//", "*", "/*")):
            current = _statement_key(lines, number, indent)
        if stripped.startswith(("*", "//", "/*")):
            continue
        code = re.sub(r"\s//\s.*$", "", line)
        for name, pattern in PATTERNS.items():
            if pattern.search(code):
                found[name].add(current)
    return {name: found.get(name, set()) for name in PATTERNS}


@pytest.mark.parametrize(
    ("script", "indent", "allowed"),
    [(SYNC_SCRIPT, 0, SYNC_SCRIPT_ALLOWED), (DRAFT_SCRIPT, 4, DRAFT_SCRIPT_ALLOWED)],
    ids=["librenms_sync.js", "sync_draft.js"],
)
def test_storage_html_writes_and_fetch_stay_in_their_allowlists(script, indent, allowed):
    assert scan(script.read_text(encoding="utf-8"), indent) == allowed


def test_the_old_script_touches_the_ip_table_only_for_its_vrf_dropdowns():
    lines = SYNC_SCRIPT.read_text(encoding="utf-8").split("\n")
    owners = set()
    current = None
    for number, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith(("}", ")", "//", "*", "/*")):
            current = _statement_key(lines, number, 0)
        if "librenms-ipaddress-table" in line and not line.strip().startswith(("*", "//")):
            owners.add(current)
    assert owners == {"initializeVRFSelects"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("function store() {\n    window.sessionStorage.setItem('a', 1);\n}\n", {"storage": {"store"}}),
        ("const paint = (cell) => {\n    cell.innerHTML = '<b></b>';\n};\n", {"html": {"paint"}}),
        (
            "document.addEventListener('click', function (e) {\n    if (!e.target.closest('.go')) return;\n"
            "    fetch('/x');\n});\n",
            {"fetch": {"document:click .go"}},
        ),
        ("function read(cell) {\n    // cell.innerHTML = 'x' in a comment\n    return cell.innerHTML;\n}\n", {}),
    ],
    ids=["storage", "html-write", "fetch-in-listener", "read-and-comment"],
)
def test_the_scan_names_the_statement_of_each_use(source, expected):
    assert {name: keys for name, keys in scan(source, 0).items() if keys} == expected
