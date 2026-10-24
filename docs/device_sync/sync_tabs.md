# Sync Tabs

The **LibreNMS Sync** page compares LibreNMS data with the selected NetBox Device or Virtual Machine. Refresh a tab to load current LibreNMS data before applying changes.

The Interfaces and Modules tables always fit the window. When the cells cannot wrap enough, the table hides its least important columns first (for example, LibreNMS ID and Description) and names them above the table. Widen the window to show them again.

Each tab remembers the **Per Page** size that you select, for your user and for that tab only. Until you select a size on a tab, the tab uses your NetBox page size.

## Filters

The Interfaces, Cables, IP Addresses and VLANs tabs have a filter on the right of the sync buttons. The server filters all rows of the device before it makes the pages, so a match on another page shows too, and the row count reads "Showing x-y of N (filtered from M)".

- **Search** finds the rows where any column shows the text: the cell text, the selected option of a dropdown, and the status and action labels. It does not search tooltips or options that are not selected. Upper and lower case and extra spaces do not matter.
- **Sync state** shows the matching, the mismatched, or the not-in-NetBox rows. Each tab below says what these states mean for its rows. A row in no state shows only under **All sync states**.
- **Filters** opens one field for each column. Search, Sync state and the column fields must all match.

Search filters the table while you type, and Sync state filters it when you select a state. The column fields filter when you select **Apply** or press Enter. Enter in a filter field never starts a sync. **Clear filters** removes the filters of that tab.

The filters are part of the page URL. Paging, sorting, the page size, refreshing, syncing and the confirmation dialogs keep the filters of every tab. A refresh or a sync also keeps the page, or shows the last page when the page no longer exists. A new filter starts at the first page. These changes reset more:

- A switch to another LibreNMS server removes the filters and pages of every tab. It keeps the page sizes and the sorts.
- A change of the interface name field loads the page again with the same filters, at the first page of every tab.
- A page load (paging, a filter change or a name field change) loses the changes in the table that you did not submit.

A row that changes after the page loads, for example when you select another Virtual Chassis member, is marked **Changed since filtering**, because it can be outside the filter now. Change a filter or load the page again to filter again. A row selection belongs to the tab, so a filter change keeps the selected rows. Selected rows that the page does not show, on another page or outside the filter, stay selected: the notice above the table counts them, and **Sync Selected** includes them. A change of the interface name field or of the ignored toggle starts with no rows selected.

A filter that is not valid, for example text longer than 256 characters, shows its errors in place of the table of that tab. The other tabs and the sync actions work as usual.

A tab with no data loaded shows no filter, as it shows no sync buttons. The filters stay in the page URL, so a refresh shows the table with them.

## Interfaces

Available for Devices and Virtual Machines. This tab creates or updates interfaces and can synchronize descriptions, status, type, speed, VLAN assignments, MAC addresses, and MTU values.

Interface names can come from different LibreNMS fields. Type mappings control how LibreNMS interface types become NetBox interface types. The tab also handles LAG and parent relationships, NetBox-only interfaces, and Virtual Chassis member assignment.

A refresh needs the device's ports and port stack from LibreNMS. When the port stack has pairs, the refresh also needs the device OS, because the OS selects the name patterns that classify the pairs. If one of these is missing, the refresh fails and keeps no data. If only the ports of a linked OOB controller are missing, the tab shows the host interfaces with a notice. The tab applies the current port stack patterns each time it shows the data, so a pattern change needs no refresh.

In the [filter](#filters), **Sync state** shows the interfaces that match NetBox, the interfaces with a different value in NetBox, or the interfaces that NetBox does not have. An ignored, ambiguous or incomplete row is in no state. The counts of ignored and NetBox-only interfaces describe the whole device, also under a filter.

A row cannot sync when its name is already used by a NetBox interface that is bound to a different LibreNMS port. The row shows that port. If the port is another row in the table, sync that row first, then sync this row. If LibreNMS no longer reports the port (for example, after the device was rediscovered), the row shows **Rebind**. Rebind moves only the LibreNMS binding of that interface to the row's port. The interface keeps its name, IP addresses, and cables. Rebind is not offered while the OOB controller data is incomplete.

## Cables

Available for Devices. This tab uses LibreNMS link data to create NetBox cable connections. Both devices and their interfaces should exist in NetBox first, so synchronize interfaces before cables.

The `librenms_id` custom field on interfaces improves matching when interface names differ between NetBox and LibreNMS.

A refresh reads the host links and ports together. A linked OOB controller has its own links and ports.
If either source fails, only that source's rows are blocked. A failed neighbour ports read blocks only
that neighbour's rows. Each notice names the source that failed. The tab reads its own ports, so it does
not need an Interfaces tab refresh.

The refresh reads serial sensors only when you can view a Console Server Port on the source device.
Each read checks this permission again, including serial rows with no NetBox port.
Serial rows need a valid sensor projection. A refresh with no sensor request removes old serial rows.
It does not show a warning when sensors were not requested.
The tab applies current serial naming rules each time it shows the snapshot. Remote-end picks belong to
you and to that snapshot. If the snapshot expires, refresh Cables before you pick a remote end.
Sync and remote-end creation use the same snapshot. Refresh Cables if an action needs port evidence
that the snapshot does not contain.

In the [filter](#filters), **Sync state** shows the rows whose cable NetBox has, directly or through a patch path; the rows whose cable goes to another port, or still needs the LibreNMS cable tag; and the rows whose cable NetBox does not have. A row whose cable state you cannot view, whose far end is not in NetBox, or that a rule or the breakout check refuses is in no state. A remote-end pick or a remote-end create also comes back to the same filters and page.

## IP Addresses

Available for Devices and Virtual Machines. This tab creates IP addresses and assigns them to matching interfaces. It can also create a missing interface before assigning an address.

A refresh reads the addresses, the ports and the device record from LibreNMS, and also the VRFs when a port is in a VRF. If one of these reads fails, the refresh fails and the tab shows no data. The table and the sync use the data of the last refresh, compared with NetBox as you can view it.

An [IP Mapping](../configuration/mappings.md#ip-mappings) keeps the addresses in a prefix out of the sync, for example the IPv6 link-local prefix `fe80::/10`. Such a row shows **Ignored** and cannot be synced.

**Set Primary IP** sets the NetBox Primary IP when the synchronized address matches the LibreNMS management address of the last refresh. Existing assignments or conflicting addresses require confirmation before they are changed.

When LibreNMS reports the address in a VRF, the row selects the NetBox VRF with the same route distinguisher, or else the same name. If no NetBox VRF has that route distinguisher or name, the row shows **+ VRF**. It creates the VRF with the LibreNMS name and route distinguisher, after a confirmation. It does not sync the address: every row in that VRF then selects the new VRF, and you sync them as usual.

In the [filter](#filters), **Sync state** shows the synced addresses, the addresses to update, or the addresses that NetBox does not have. An ignored address is in no state. **Set Primary IP** still selects the management address when the filter does not show it, and the notice above the table counts it.

## VLANs

Available for Devices. This tab creates VLANs reported by LibreNMS. Each VLAN can use a suitable VLAN group based on NetBox scope, or remain global when no group is selected.

Interface VLAN assignments are handled by the Interfaces tab rather than this tab.

In the [filter](#filters), **Sync state** shows the VLANs that NetBox has with the same name, with another name, or does not have. A VLAN with an invalid or duplicate VID is in no state. On the Interfaces tab, the VLAN modal option that applies the groups to all interfaces saves them for the whole device, on every page and in every filter.

## Modules

Available for Devices. This tab compares LibreNMS physical inventory with NetBox module bays and installed modules. Module matching has additional mapping and inventory rules, so it is covered in the [Module Sync guide](module_sync.md).

The Modules refresh needs host inventory, ports and transceivers. If a linked OOB controller's inventory
fails, host module rows remain available. A notice names each failed source. A host source failure clears
the table until a successful refresh. The tab and each module action apply current inventory rules to
the same source snapshot. Parent repair and OOB index offsets do not change the stored source facts.
