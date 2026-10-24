"""Shared helpers for observing sync-cache invalidation in ORM tests."""


def snapshot_state(obj, server_key="default"):
    """Return which applicable tabs still hold a snapshot for *obj*."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    coordinator = SyncCacheConsistency(obj)
    return {
        tab: cache.get(coordinator.snapshot_key(tab, server_key)) is not None for tab in coordinator.applicable_tabs()
    }


def drain_pending_commit_callbacks():
    """Discard cleanups queued by fixture writes before a test seeds snapshots."""
    from django.db import transaction

    transaction.get_connection().run_on_commit.clear()


def plugin_commit_callbacks(callbacks):
    """Keep only this plugin's hooks. NetBox registers commit callbacks of its own (deferred search flush since 4.7)."""
    return [cb for cb in callbacks if getattr(cb, "__module__", None) == "netbox_librenms_plugin.cache_signals"]


def seed_every_tab(obj, server_key="default"):
    """Give *obj* a snapshot on every applicable tab so invalidation is observable."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    drain_pending_commit_callbacks()
    coordinator = SyncCacheConsistency(obj)
    keys = []
    for tab in coordinator.applicable_tabs():
        key = coordinator.snapshot_key(tab, server_key)
        cache.set(key, [{"seeded": tab.value}], timeout=300)
        keys.append(key)
    assert keys, f"no tab applies to {obj!r}, so nothing was seeded and every assertion would be vacuous"
    assert all(cache.get(key) is not None for key in keys), "the seed never landed"
    return keys


def seed_inventory(
    device, inventory, *, librenms_id, server_key="default", ports=(), transceivers=(), oob_inventory=None, oob_id=None
):
    """Store explicit raw module facts under the requested source binding."""
    from netbox_librenms_plugin import source_cache
    from netbox_librenms_plugin.acquisition.registry import Resource
    from netbox_librenms_plugin.tests.source_snapshot_helpers import seed_modules_snapshot
    from netbox_librenms_plugin.tests.mapping_fixtures import stored_mapping_for_test, seed_stored_mapping, seed_mapping

    if oob_id is not None:
        seed_mapping(device, server_key, oob=oob_id)
    mapping = stored_mapping_for_test(device)
    seed_mapping(device, server_key, own=librenms_id, save=False)
    seed_modules_snapshot(device, inventory, ports, transceivers, server_key=server_key, oob_inventory=oob_inventory)
    seed_stored_mapping(device, mapping)
    return source_cache.snapshot_key(device, Resource.MODULES, server_key)


def clear_snapshots(keys):
    """Delete snapshot keys seeded by a test."""
    from django.core.cache import cache

    for key in keys:
        cache.delete(key)
