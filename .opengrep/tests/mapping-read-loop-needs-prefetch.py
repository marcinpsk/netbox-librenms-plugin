from netbox_librenms_plugin.server_mappings import prefetch_mappings, read_mapping


def loop_without_prefetch(devices, server):
    ids = []
    for device in devices:
        # ruleid: mapping-read-loop-needs-prefetch
        ids.append(read_mapping(device).own_id(server))
    return ids


def comprehension_without_prefetch(members):
    # ruleid: mapping-read-loop-needs-prefetch
    return [read_mapping(member) for member in members]


def generator_without_prefetch(devices, server):
    # ruleid: mapping-read-loop-needs-prefetch
    return any(read_mapping(device).migrated_to(server) for device in devices if device is not None)


def tuple_loop_without_prefetch(pairs):
    for label, device in pairs:
        # ruleid: mapping-read-loop-needs-prefetch
        if read_mapping(device).legacy.is_legacy:
            return label
    return None


def loop_over_the_prefetched_list(devices, server):
    ids = []
    for device in prefetch_mappings(devices):
        # ok: mapping-read-loop-needs-prefetch
        ids.append(read_mapping(device).own_id(server))
    return ids


def prefetch_before_the_comprehension(members):
    members = prefetch_mappings(members)
    # ok: mapping-read-loop-needs-prefetch
    return [read_mapping(member) for member in members]


def prefetch_in_a_nested_statement(interfaces, related):
    if interfaces:
        prefetch_mappings([*interfaces, *related])
    # ok: mapping-read-loop-needs-prefetch
    return {interface.pk: read_mapping(interface) for interface in interfaces}


def single_object_read(device, server):
    # ok: mapping-read-loop-needs-prefetch
    return read_mapping(device).own_id(server)


def loop_reads_another_object(device, servers):
    # ok: mapping-read-loop-needs-prefetch
    return [read_mapping(device).own_id(server) for server in servers]
