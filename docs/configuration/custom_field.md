# Using the `librenms_id` Custom Field

## Overview

To enhance device identification and synchronization between NetBox and LibreNMS, this plugin supports using a custom field `librenms_id` on Device, Virtual Machine, Interface, and VM Interface objects. While the plugin works without it, using this custom field is recommended for LibreNMS API lookups, and to assist with matching the remote device and remote interfaces for cable creation in Netbox. It can also be entered manually if no primary IP or FQDN is available.

!!! info "Automatic Creation"
    The plugin **automatically creates** the `librenms_id` custom field when migrations run. The field is created for Device, Virtual Machine, Interface, and VM Interface objects with JSON type for per-server device tracking.

For the Device and Virtual Machine objects the plugin will automatically populate the LibreNMS ID custom field when opening the LibreNMS Sync page if the device has been found in LibreNMS.

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
    - Clear a field to remove that ID. A row must keep a LibreNMS ID or an OOB controller ID.

4. **Save the row:**

    - Click **Save** on the row. Each row saves only its own server.
    - The plugin refuses an ID that another NetBox device or virtual machine already holds on that
      server. It names that object only when you have permission to view it.
    - When another user changed the mapping after the page loaded, the plugin refuses the save.
      Reload the page and enter the values again.

You need change permission on the object. A virtual chassis member shows the editor on the page of
the member that holds the mapping. Use **Convert ID** to convert a legacy value (a bare integer)
before you edit it. The editor does not repair a value with an invalid format.

To remove the mapping for a server that is no longer configured, click **Remove** on its row in the
**LibreNMS Connections** card. When an object has usable mappings on two or more servers, use the
star control on the same card to select the preferred server. The preference is stored in the
mapping as `_preferred_server`; do not use that name as a server key.

Interfaces get their `librenms_id` value from the interface sync, so the sync page has no editor for
them.

## Notes

- If `librenms_id` is set, the plugin will prioritize it over other identification methods.
- Ensure the `librenms_id` corresponds to the correct device ID in LibreNMS to prevent mismatches.
- The custom field is optional but recommended for optimal plugin performance.
- Using the custom field on interfaces will greatly improve the interface matching required for cable synchronization.
