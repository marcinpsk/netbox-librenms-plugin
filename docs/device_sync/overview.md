# Device Sync

The **LibreNMS Sync** page is available from a NetBox Device or Virtual Machine. It compares the object with LibreNMS and lets you apply selected changes to NetBox.

The page shows whether the object is linked to LibreNMS and which LibreNMS server is active. An object without a link shows the LibreNMS device that matches it, with a **Link** button that stores the link. Each linked server has an **Unlink** action, which also clears that server's port IDs on the interfaces of the object; its confirmation lists them first ([details](../configuration/custom_field.md#unlink-replace-or-clear-an-id)). For Devices, it can also compare the name, device type, serial number, platform, and location. For Virtual Machines, it compares the platform. If the object is not found, the page can add it to LibreNMS.

The available sync tabs depend on the NetBox object:

| Tab | Device | Virtual Machine |
|---|---:|---:|
| Interfaces | Yes | Yes |
| Cables | Yes | No |
| IP Addresses | Yes | Yes |
| VLANs | Yes | No |
| Modules | Yes | No |

See [Sync Tabs](sync_tabs.md) for the purpose and key behavior of each tab. [Module Sync](module_sync.md) has a separate guide because physical inventory matching needs additional configuration. [Virtual Chassis](virtual_chassis.md) explains how the page handles multi-member devices.
