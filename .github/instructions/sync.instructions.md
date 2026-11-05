---
applyTo: "**/views/base/**,**/views/object_sync/**,**/views/sync/**,**/tables/**,**/librenms_sync.js"
description: Sync page architecture, base views, and sync action patterns
---

# Sync Pages

## Three-Layer View Architecture
All five sync resources (interfaces, cables, IP addresses, modules, VLANs) follow the same pattern:

1. **Base views** (`views/base/`) — abstract classes that define the data pipeline:
   - `BaseLibreNMSSyncView` — tabbed sync page, orchestrates all tabs via abstract `get_*_context()` methods.
   - `BaseInterfaceTableView`: acquire the interfaces snapshot → assess it (`assessment.interfaces.assess_interfaces`) → compare with NetBox interfaces → render table.
   - `BaseCableTableView`: acquire the cables snapshot → assess it (`assessment.cables.assess`) → match remote devices → check cable status → render table.
   - `BaseIPAddressTableView` — acquire the IP snapshot → assess it for the user (`assess_snapshot`) → render table.
   - `BaseVLANTableView`: acquire the VLAN snapshot → assess it (`assessment.vlans.assess`) → render table.

2. **Object sync views** (`views/object_sync/`) — wire base views to NetBox models:
   - Use `@register_model_view(Device, name="librenms_sync", path="librenms-sync")` to inject as a tab on Device/VM detail pages.
   - Each `get_*_context()` method creates an instance of the concrete table view, copies `request`, and calls `get_context_data()`.
   - VMs skip cables and VLANs (return `None`).

3. **Sync action views** (`views/sync/`) — POST-only views that create/update/delete NetBox objects:
   - Follow a consistent pattern: check permissions → read selected rows from POST → load cached data → apply changes in `transaction.atomic()` → return the action's HTMX response, or redirect to the sync tab for a non-HTMX request.
   - Cable sync returns `_cable_sync_content.html` for HTMX so the response matches `#cable-sync-content`. Other actions can use `HX-Redirect` when they require full navigation.

## Data Pipeline (Base Views)
Every sync tab follows: **acquire → store → assess → render**.

- **Acquire:** `acquisition.coordinator.acquire(subject, server_key=..., resources=[...])` obtains the
  LibreNMS responses with no request or user, reads the mapping and writes nothing. It returns a
  `SourceSnapshot`: each response with its outcome. `snapshot.value(resource, part)` raises
  `IncompleteSource` for a part that was not obtained; never read a failed part as an empty list.
- **Store:** `source_cache.refresh(request, subject, resource, server_key)` runs after discovery and stores the snapshot under the
  `CacheMixin` key; `source_cache.read()` is the one reader for the tab, the verify views and the sync.
  The key includes the plugin version and NetBox release.
- **Assess:** derive the rows at read time, for the current user. The snapshot holds source facts only.
  The snapshot's `finished_at` is the tab's "last updated" time.
- **Scopes:** a refresh stores the snapshot when at least one scope is ready. The interfaces host scope
  needs the ports, the port_stack and, when port_stack has pairs, a device OS. A linked OOB controller
  is its own mapping: its failure blocks only the OOB rows.

- **Cables:** host and OOB rows each need their own links and ports. A failed neighbour ports request
  blocks only that neighbour's rows. The 15-second neighbour budget records omitted requests as failed.
  `source_cache.refresh()` checks ConsoleServerPort visibility before it enables serial acquisition.
  Each read checks the actor's visibility again. Serial rows need obtained sensors and a valid
  projection. Unrequested sensors do not produce a warning. A refresh never carries old serial rows
  forward. Names, aliases, port records and row IDs are assessed on each read.
  The tab, verify, sync, picker and far-end creation read through `source_cache.read()`.
  Sync and far-end creation use the stored port evidence. Missing evidence requires a refresh.
  Manual picks bind to `snapshot_id` and use the owner's snapshot key. An expired picker asks for a refresh.

- **Modules:** host rows need inventory, ports and transceivers. OOB rows need OOB inventory and a
  ready host scope. An OOB failure leaves host rows usable. Every tab, verify, install, replace and
  sync reader calls `source_cache.read()` and assesses the bound source on each read. Parent repair,
  transceiver merge, port identity and OOB index offsets exist only in `assessment/modules.py`.
  Notices name each failed source. The binding includes the owner, host id and OOB link.

## Sync Action View Pattern
```python
from django.http import HttpResponse


class SyncSomeResourceView(
    LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View
):
    required_object_permissions = {"POST": [("add", Model), ("change", Model)]}

    def post(self, request, object_type, object_id):
        if error := self.require_all_permissions("POST"):
            return error
        # 1. Resolve object (Device or VM)
        # 2. Read selected items from request.POST.getlist("select")
        server_key = self.resolve_posted_server_key_or_none(request.POST)
        if server_key is None:
            return HttpResponse(status=400)
        # 3. Read the bound source with source_cache.read() and assess its rows
        # 4. Apply changes inside transaction.atomic()
        # 5. Return the action's HTMX response, or redirect to ?tab=<resource>
```

## Table Conventions (`tables/*.py`)
- Tables define HTMX-enabled columns and checkboxes. Selection uses `ToggleColumn(attrs={"input": {"name": "select"}})`.
- Constructor takes contextual params (e.g., `device`, `interface_name_field`, `vlan_groups`) to customize rendering.
- Tables set `self.tab` and `self.prefix` for multi-table pagination via `get_table_paginate_count()`.
- Row attrs include `data-*` attributes for JavaScript filtering and identification.
- VLAN columns use `render_vlans()` with hidden inputs for per-row group selection and JSON data for modals.

## Key Mixins Used by Sync Views
- **`LibreNMSAPIMixin`** — lazy-creates `LibreNMSAPI` instance via `self.librenms_api` property. Also provides `get_server_info()` for template context.
- **`CacheMixin`**: generates consistent cache keys via `get_cache_key(obj, data_type, server_key)`. Also provides `get_vlan_overrides_key(obj, server_key)` for VLAN group override persistence.
- **`VlanAssignmentMixin`** — VLAN group scope resolution: Rack → Location → Site → SiteGroup → Region → Global. Used by interface and VLAN sync for auto-selecting the most-specific VLAN group and building lookup maps.

## JavaScript (`librenms_sync.js`)
- Not wrapped in an IIFE — functions are global. Master initializer `initializeScripts()` runs on `DOMContentLoaded` and `htmx:afterSwap`.
- **Key function groups:**
  - Checkbox management: `initializeTableCheckboxes()`, `updateBulkActionButton()`.
  - TomSelect dropdowns: `initializeVCMemberSelect()`, `initializeVRFSelects()`, `initializeVlanGroupSelects()`, `initializeVlanSyncGroupSelects()`. Uses `TOMSELECT_INIT_DELAY_MS = 100` for delayed initialization after HTMX swaps.
  - Verification: `handleInterfaceChange()`, `handleCableChange()`, `handleVRFChange()` — POST to single-item verify endpoints.
  - VLAN modals: `openVlanDetailModal()`, `verifyVlanInGroup()`, `verifyVlanSyncGroup()` — per-interface VLAN detail editing.
  - Bulk operations: `initializeBulkEditApply()`, `deleteSelectedInterfaces()`.
  - Table filters: the server filters each sync table (`sync_filters.py`, `sync_query.py`). The script keeps the
    filter panel state (`initializeFilterDisclosures()`) and marks a repainted row of a filtered table (`markFilterStaleRow()`).
  - URL/tab state: `activeSyncTab()`, `getDeviceIdFromUrl()`, `setInterfaceNameFieldFromURL()`.
    Tab activation is server-rendered: the swapped container carries `data-active-tab`, and
    `activeSyncTab()` reads it. There is no client-side tab initializer.
  - Cache countdowns: `initializeCountdown()`, `initializeCountdowns()`.
- CSRF token extracted via `document.querySelector('[name=csrfmiddlewaretoken]').value`.

IP rows are derived in `assessment/ip_addresses.py`. Pass the actor and the interface-name option.
The tab, verify and sync readers use the same assessor. The web adapter adds URLs. A GET does not
write an interface binding. VLAN source indexes and render comparisons are in `assessment/vlans.py`.
The group verify endpoint uses the same VLAN name comparison for its posted values.

Snapshot keys and tab-state keys use one versioned namespace. A failed refresh in another plugin
version or NetBox release does not change the current version's tab state.
