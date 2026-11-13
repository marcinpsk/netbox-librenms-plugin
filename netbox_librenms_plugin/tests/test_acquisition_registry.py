"""The acquisition registry owns each resource's cache identity; the tab specs only present it."""


def test_every_tab_shows_exactly_one_resource():
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab

    assert set(TAB_SPECS) == set(SyncTab)
    assert {spec.resource for spec in TAB_SPECS.values()} == set(Resource)
    assert all(spec.resource.value == tab.value for tab, spec in TAB_SPECS.items())


def test_every_resource_has_one_cache_identity():
    from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, Resource

    assert set(RESOURCE_SPECS) == set(Resource)
    data_types = [spec.data_type for spec in RESOURCE_SPECS.values()]
    assert len(set(data_types)) == len(data_types)


def test_a_tab_spec_holds_no_cache_identity():
    from dataclasses import fields

    from netbox_librenms_plugin.sync_cache import SyncTabSpec

    assert {field.name for field in fields(SyncTabSpec)} == {
        "resource",
        "context_name",
        "content_id",
        "label",
        "table_context_key",
    }


def test_the_owner_policies_are_unchanged():
    from netbox_librenms_plugin.acquisition.registry import RESOURCE_SPECS, OwnerPolicy, Resource

    assert {
        resource for resource, spec in RESOURCE_SPECS.items() if spec.owner_policy is OwnerPolicy.VC_SYNC_OWNER
    } == {
        Resource.INTERFACES,
        Resource.CABLES,
        Resource.MODULES,
    }
    assert {resource for resource, spec in RESOURCE_SPECS.items() if spec.supports_virtual_machine} == {
        Resource.INTERFACES,
        Resource.IP_ADDRESSES,
    }
