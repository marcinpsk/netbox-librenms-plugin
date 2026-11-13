"""
The required-row rule of the Interfaces tab: which rows a selection takes along.

A row can require other rows: a LAG member requires its aggregate and an aggregate its members, a
sub-interface requires its parent, and a bridged port its bridge. While the "Auto-select related
interfaces" option is on, a selected row takes every row it requires along, transitively, except a row
that the user excluded. A blocked row (the interface rules refuse it, or it has no owner the user can
pick) is never taken, and the walk does not go through it.

The server renders each row's requirements and the count of selected rows on other pages that require
it; the page (sync_draft.js) mirrors :func:`checked_rows` over those attributes for instant ticks, and the
submit decides with the same functions. The JSON case file ``tests/selection_rule_cases.json`` holds
the cases that both sides must agree on.
"""

from collections import defaultdict

# The intents of selection_draft.Intent (a StrEnum, so its members equal these texts). The module imports
# nothing of the plugin, so the browser test of the page's mirror can load it from its file.
_ON = "on"
_NONE = "none"


def requirement_edges(lag_members, sub_interfaces, bridge_members):
    """
    Return the rows that each row requires directly, from the relationship maps of a snapshot.

    Args:
        lag_members (dict[int, int]): The aggregate of each LAG member, by port ID.
        sub_interfaces (dict[int, int]): The parent of each sub-interface, by port ID.
        bridge_members (dict[int, int]): The bridge of each bridged port, by port ID.

    Returns:
        dict[str, set[str]]: The required row keys of each row key.

    """
    edges = defaultdict(set)
    for member, aggregate in lag_members.items():
        edges[str(member)].add(str(aggregate))
        edges[str(aggregate)].add(str(member))
    for child, parent in sub_interfaces.items():
        edges[str(child)].add(str(parent))
    for member, bridge in bridge_members.items():
        edges[str(member)].add(str(bridge))
    return dict(edges)


def requirements(edges, row_key, blocked):
    """Return every row that *row_key* requires, transitively, without a blocked row or a walk through one."""
    found = set()
    pending = [row_key]
    while pending:
        for required in edges.get(pending.pop(), ()):
            if required != row_key and required not in found and required not in blocked:
                found.add(required)
                pending.append(required)
    return found


def taken_rows(edges, intents, blocked):
    """
    Return the rows that the selected rows take along: each one required by a selected row and not excluded.

    Args:
        edges (dict[str, Iterable[str]]): The direct requirements of each row key.
        intents (dict[str, Intent]): The intent of each row key that has one; a selected row is ``Intent.ON``.
        blocked (set[str]): The row keys that no selection may take.

    Returns:
        set[str]: The taken row keys; a selected row is not among them.

    """
    taken = set()
    for row_key, intent in intents.items():
        if intent == _ON and row_key not in blocked:
            taken |= requirements(edges, row_key, blocked)
    return {row_key for row_key in taken if intents.get(row_key, _NONE) == _NONE}


def checked_rows(edges, intents, blocked, *, enabled):
    """Return the rows whose checkbox shows checked: the selected rows, and the taken rows while *enabled*."""
    selected = {row_key for row_key, intent in intents.items() if intent == _ON}
    return selected | taken_rows(edges, intents, blocked) if enabled else selected


def off_page_requirers(edges, intents, blocked, page_keys):
    """
    Return, for each row of the page, how many selected rows on no shown page require it.

    Args:
        edges (dict[str, Iterable[str]]): The direct requirements of each row key.
        intents (dict[str, Intent]): The intent of each row key that has one.
        blocked (set[str]): The row keys that no selection may take.
        page_keys (Iterable[str]): The row keys of the page.

    Returns:
        dict[str, int]: The count of each page row key that a selected row on another page requires.

    """
    page_keys = set(page_keys)
    counts = defaultdict(int)
    for row_key, intent in intents.items():
        if intent == _ON and row_key not in page_keys and row_key not in blocked:
            for required in requirements(edges, row_key, blocked) & page_keys:
                counts[required] += 1
    return dict(counts)
