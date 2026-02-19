from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN

menu = PluginMenu(
    label="LibreNMS",
    icon_class="mdi mdi-network",
    groups=(
        (
            "Settings",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:settings",
                    link_text="Plugin Settings",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacetypemapping_list",
                    link_text="Interface Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacetypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:devicetypemapping_list",
                    link_text="Device Type Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:devicetypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:devicetypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:moduletypemapping_list",
                    link_text="Module Type Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:moduletypemapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:moduletypemapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:modulebaymapping_list",
                    link_text="Module Bay Mappings",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:modulebaymapping_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:modulebaymapping_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:interfacenamerule_list",
                    link_text="Interface Name Rules",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacenamerule_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:interfacenamerule_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:normalizationrule_list",
                    link_text="Normalization Rules",
                    permissions=[PERM_VIEW_PLUGIN],
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:normalizationrule_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_librenms_plugin:normalizationrule_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
            ),
        ),
        (
            "Import",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:librenms_import",
                    link_text="LibreNMS Import",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
        (
            "Status Check",
            (
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:site_location_sync",
                    link_text="Site & Location Sync",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:device_status_list",
                    link_text="Device Status",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
                PluginMenuItem(
                    link="plugins:netbox_librenms_plugin:vm_status_list",
                    link_text="VM Status",
                    permissions=[PERM_VIEW_PLUGIN],
                ),
            ),
        ),
    ),
)
