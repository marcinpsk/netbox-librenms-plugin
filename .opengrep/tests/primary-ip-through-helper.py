from netbox_librenms_plugin.ip_addressing import primary_ips


def identity(obj):
    # ruleid: primary-ip-through-helper
    address = obj.primary_ip.address.ip if obj.primary_ip else None
    # ruleid: primary-ip-through-helper
    row = getattr(obj, "primary_ip", None)
    # ruleid: primary-ip-through-helper
    if obj.virtual_chassis.master.primary_ip:
        return address, row
    # ok: primary-ip-through-helper
    addresses = [str(ip.address.ip) for ip in primary_ips(obj)]
    # ok: primary-ip-through-helper
    families = (obj.primary_ip4, obj.primary_ip6, obj.primary_ip4_id)
    # ok: primary-ip-through-helper
    row = getattr(obj, "primary_ip4", None)
    # ok: primary-ip-through-helper
    set_primary_ip = obj.oob_ip
    return addresses, families, row, set_primary_ip
