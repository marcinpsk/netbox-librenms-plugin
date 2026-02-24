# Mixins

Mixins in `views/mixins.py` provide reusable logic to keep views clean and DRY (Don't Repeat Yourself). They are designed to be combined with Django or NetBox views to add specific behaviors or shared functionality. When adding new views, consider using or extending these mixins to maintain consistency and reduce code duplication.

### Key Mixins

**LibreNMSAPIMixin**

  - Provides a `librenms_api` property for accessing the LibreNMS API from any view.
  - Ensures a single instance of the API client is reused per view instance.
  - Example usage: Add to views that need to fetch or sync data with LibreNMS.

**CacheMixin**

  - Supplies helper methods for generating cache keys related to objects and data types (e.g., ports, links, vlans).
  - Useful for views that cache data fetched from LibreNMS to improve performance.
  - Methods:
    - `get_cache_key(obj, data_type="ports")`: Returns a unique cache key for the object and data type.
    - `get_last_fetched_key(obj, data_type="ports")`: Returns a cache key for tracking when data was last fetched.
    - `get_vlan_overrides_key(obj)`: Returns a cache key for storing user VLAN group override selections.

**VlanAssignmentMixin**

  - Provides VLAN group resolution and assignment logic used by both the Interfaces tab (per-interface VLAN assignments) and the VLANs tab (VLAN object sync).
  - Resolves which VLAN groups are relevant to a device based on a scope hierarchy: Rack → Location → Site → SiteGroup → Region → Global.
  - Methods:
    - `get_vlan_groups_for_device(device)`: Returns all VLAN groups relevant to the device based on scope hierarchy.
    - `_build_vlan_lookup_maps(vlan_groups)`: Builds lookup dictionaries mapping VIDs to groups, VLANs, and names.
    - `_select_most_specific_group(groups, device)`: Resolves ambiguity when a VID exists in multiple groups by selecting the most specific scope.
    - `_find_vlan_in_group(vid, vlan_group_id, lookup_maps)`: Finds a VLAN by VID, preferring the specified group.
    - `_update_interface_vlan_assignment(interface, vlan_data, vlan_group_map, lookup_maps)`: Updates interface mode, untagged VLAN, and tagged VLANs in NetBox.

### How to Use Mixins

To use a mixin, simply add it to the inheritance list of your view class. For example:

```python
from .mixins import LibreNMSAPIMixin, CacheMixin

class MyCustomView(LibreNMSAPIMixin, CacheMixin, SomeBaseView):
    # ... your view logic ...
```

Mixins can be combined as needed. Place mixins before the main base view to ensure their methods and properties are available.
