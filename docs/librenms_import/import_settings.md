# Import Settings

Configure how devices are named and what data is imported from LibreNMS to NetBox.

## Setting Defaults

To configure global defaults for all imports:

1. Navigate to **Plugins → LibreNMS Plugin → Settings**
2. Click **Plugin Settings**
3. Configure Use sysName and Strip Domain to your preferred defaults
4. Save changes

These defaults apply to all future imports unless overridden during the import process.

## User Preferences and Defaults

The plugin uses a two-tier preference system for the **Use sysName** and **Strip Domain** toggles:

1. **Plugin defaults** (set by admins on the Settings page) apply to all users who have not yet changed their own toggle settings.
2. **Per-user preferences** are saved automatically when a user changes a toggle on the import page. Once saved, the user's preference takes priority over the plugin default.

**Important notes:**

- Changing the plugin defaults does **not** override existing user preferences. Users who have previously changed a toggle keep their personal setting.
- When an admin saves import settings, only the admin's own preferences are updated to match the new defaults. Other users are unaffected.
- There is no "reset to defaults" for individual users. To revert to the plugin default, a user simply needs to toggle the setting to match.

## Device Naming Options

The plugin provides two settings that control how device names are created in NetBox. Both are configured in Plugin Settings under **Plugins → LibreNMS Plugin → Settings → Plugin Settings** and can be overridden on the LibreNMS import page.

### Use sysName

Controls which field from LibreNMS becomes the device name in NetBox.

- **Enabled** (default): Uses the SNMP sysName, falling back to LibreNMS hostname if sysName is not available
- **Disabled**: Uses the LibreNMS hostname field

### Strip Domain

Removes domain suffixes from device names to create shorter, cleaner names.

- **Enabled**: Removes domain suffixes (e.g., "router.example.com" becomes "router"). IP addresses are preserved without modification
- **Disabled**: Keeps the full name as-is

### Naming Examples

```
LibreNMS sysName: router-core-01.example.com
LibreNMS hostname: 10.0.0.1

Use sysName + Strip domain → "router-core-01"
Use sysName + Keep domain → "router-core-01.example.com"
Use hostname + Strip domain → "10.0.0.1" (IP preserved)
Use hostname + Keep domain → "10.0.0.1"
```

If neither sysName nor hostname exists, the plugin generates a name as `device-{librenms_id}`.



## Per-Import Overrides

On the import page, the **Use sysName** and **Strip Domain** toggles are pre-populated from your saved preference (or the plugin default if you haven't set one). Changing a toggle immediately saves your preference for next time and applies to the current import.

This allows you to:

- Import some devices with sysName and others with hostname
- Apply domain stripping selectively based on device type or location
- Test different naming conventions — your last choice is remembered automatically
