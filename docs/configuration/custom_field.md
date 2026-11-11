# Using the `librenms_id` Custom Field

## Overview

To enhance device identification and synchronization between NetBox and LibreNMS, this plugin supports using a custom field `librenms_id` on Device, Virtual Machine, Interface, and VM Interface objects. While the plugin works without it, using this custom field is recommended for LibreNMS API lookups, and to assist with matching the remote device and remote interfaces for cable creation in Netbox. It can also be entered manually if no primary IP or FQDN is available.

!!! info "Automatic Creation"
    The plugin **automatically creates** the `librenms_id` custom field when migrations run. The field is created for Device, Virtual Machine, Interface, and VM Interface objects with JSON type for per-server device tracking.

For Device and Virtual Machine objects, the plugin never stores the LibreNMS ID when a page loads. It
proposes the LibreNMS device that matches the object, and stores the ID only when you click **Link**. See
[Link a device or virtual machine](#link-a-device-or-virtual-machine).

For Interface and VM Interface objects, the plugin will automatically populate the LibreNMS ID custom field when the interface data is synced from LibreNMS.

## Benefits of Using `librenms_id`

- **Improved Device Matching:** Ensures accurate matching between NetBox and LibreNMS devices.
- **Fallback Identification:** Useful when devices lack a primary IP or FQDN.
- **Efficient Synchronization:** Enhances the reliability of API lookups.
- **Cable creation:** Allows better device identification for the creation of cables between NetBox devices.

## Manual Custom Field Setup

!!! note
    On 0.4.4+, rerun migrations first (`manage.py migrate`). If you need to recreate the field manually on current releases, use the JSON schema below. Pre-0.4.4 releases used an Integer field — do not use Integer for new entries.

If the field was not created automatically (fallback): follow these steps to create the `librenms_id` custom field in NetBox:

1. **Navigate to Custom Fields:**

    - Go to **Customization** in the NetBox sidebar.
    - Click on **Custom Fields**.

2. **Add a New Custom Field:**

    - Click the **Add a custom field** button.

3. **Configure the Custom Field:**

    - **Object Types:**
        - Check **dcim > device**
        - Check **virtualization > virtual machine**
        - Check **dcim > interface**
        - Check **virtualization > interface**
    - **Name:** `librenms_id`
    - **Label:** `LibreNMS ID`
    - **Description:** (Optional) Add a description like "LibreNMS Device ID for synchronization".
    - **Type:** JSON (object) — stores a per-server mapping.
      - Multi-server example:
        ```json
        {"production": 42, "staging": 17}
        ```
      - Out-of-band (OOB) form — when a device is linked to its OOB controller, the per-server value is an object holding the host id and the controller's id/type:

        ```json
        {"production": {"id": 42, "oob": {"id": 99, "type": "drac"}}}
        ```

        This shape is written automatically by the OOB flows — see [Out-of-Band Management](../device_import/oob_management.md). You don't normally edit it by hand.
      - Legacy single-server example (integer) — read-only/deprecated; do not use for new entries:
        ```
        42
        ```
        To convert a legacy value, open the device's LibreNMS Sync page and click **Convert ID**. Conversion is available only when the NetBox and LibreNMS serial numbers match. To create new entries manually, use the JSON format shown above.
    - **Required:** Leave unchecked (optional).
    - **Default Value:** Leave blank.


4. **Save the Custom Field:**

    - Click **Create** to save the custom field.



### Manually assign a value to `librenms_id`

Set or correct the mapping of a device or virtual machine on its LibreNMS Sync page. Do not edit the
JSON value of the custom field by hand.

1. **Open the sync page:**

    - Navigate to the device or virtual machine in NetBox.
    - Open the **LibreNMS Sync** tab.

2. **Open the editor:**

    - In the **LibreNMS Connections** card (or the **Active LibreNMS Server** card when the object has
      no mapping yet), click **Edit server mappings**.
    - The editor has one row for each configured LibreNMS server.

3. **Enter the IDs:**

    - **LibreNMS ID:** the LibreNMS device ID of the object on that server.
    - **OOB controller ID** and **OOB type:** the LibreNMS device ID of its out-of-band controller
      (iDRAC, iLO, IPMI and others), and the controller type. See
      [Out-of-Band Management](../device_import/oob_management.md).
    - Clear an ID to remove it. Clear both IDs to unlink the server.

4. **Save the row:**

    - Click **Save** on the row. Each row saves only its own server.
    - A new ID on an empty field saves at once. A change that replaces or clears an ID opens its
      confirmation first, and nothing changes until you confirm it. See
      [Unlink, replace or clear an ID](#unlink-replace-or-clear-an-id).
    - The plugin refuses an ID that another NetBox device or virtual machine already holds on that
      server. It names that object only when you have permission to view it.
    - When another user changed the mapping after the page loaded, the plugin refuses the save.
      Reload the page and enter the values again.

You need change permission on the object. A virtual chassis member shows the editor on the page of
the member that holds the mapping. Use **Convert ID** to convert a legacy value (a bare integer)
before you edit it. The editor does not repair a value with an invalid format.

To remove the mapping for a server, configured or not, click **Unlink** on its row in the
**LibreNMS Connections** card. When an object has usable mappings on two or more servers, use the
star control on the same card to select the preferred server. When you add a second mapping and
no preferred server is stored, the first mapping becomes the preferred server. The preference is
stored in the mapping as `_preferred_server`; do not use that name as a server key.

Interfaces get their `librenms_id` value from the interface sync, so the sync page has no editor for
them.

### Link a device or virtual machine

When an object has no LibreNMS ID on the active server, the sync page looks for it in LibreNMS by the
primary IP address, then the DNS name of the primary IP, then the object name. The **LibreNMS Status**
card shows the match: the LibreNMS ID, its hostname and what matched it. Click **Link** to store the ID.
When no device matches, the card offers **Add to LibreNMS**. When LibreNMS does not answer, the card
says that LibreNMS could not be reached, and nothing is proposed.

The **Device LibreNMS Status** and **Virtual Machine LibreNMS Status** lists show the stored link of
every listed object. For the unlinked objects on the visible page, they also show the match. Click
**Link** on a row, or select rows and click **Link selected**. Matching on one page stops after 15
seconds, and the rows that were not checked show **Not checked**. Reload the page to check them.

The plugin refuses a link when:

- the LibreNMS ID belongs to another object (the list names it only when you can view it);
- two rows on the page match the same LibreNMS device, or two selected rows link one ID;
- the object changed after the page showed the match, for example a new primary IP address, or it got
  a LibreNMS ID in the meantime. Reload the page and try again.

You need change permission on the object, and the plugin write permission.

### Unlink, replace or clear an ID

Three changes remove or replace a LibreNMS ID of an object on one server:

- **Unlink** removes the whole entry of the server (the LibreNMS ID and the OOB controller), and the
  preferred server when it names that server.
- A new or a cleared **LibreNMS ID** in the editor replaces or removes the own ID.
- A new or a cleared **OOB controller ID** in the editor replaces or removes the OOB controller.

Each change also clears the port IDs of that server on the interfaces of the object: the interfaces
of a VM, of a device, or of every member of the device's virtual chassis. The port IDs of other
servers stay. The confirmation names the IDs that it removes or replaces, and lists each interface
that it clears. Nothing changes until you confirm. After an unlink, the sync page proposes the
LibreNMS match again; nothing links by itself.

The cascade can also clear ports of another chassis member's link, or of the own ID when only the OOB
controller changes; an interface sync sets them again. It does not clear a port held outside these
interfaces, such as a cable far end created on another device.

The plugin refuses the change when:

- an interface of the scope has a legacy or invalid `librenms_id` value (the message names it);
- the stored value of the object is legacy or invalid, or the server records a merge marker;
- the new ID belongs to another object;
- your permissions do not include the view of every object of the scope and the view and change of
  every interface that it clears;
- the object, its chassis membership or any of those interfaces changed after the confirmation
  showed them. Reload the page and try again.

Add as OOB and the import replacement of a LibreNMS ID refuse a change that would clear interface
port IDs. Make that change on the sync page.

## Notes

- If `librenms_id` is set, the plugin will prioritize it over other identification methods.
- Ensure the `librenms_id` corresponds to the correct device ID in LibreNMS to prevent mismatches.
- The custom field is optional but recommended for optimal plugin performance.
- Using the custom field on interfaces will greatly improve the interface matching required for cable synchronization.
