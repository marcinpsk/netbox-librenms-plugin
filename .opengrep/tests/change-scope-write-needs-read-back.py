def bad_view(self, Device, pk):
    # ruleid: change-scope-write-needs-read-back
    device = self.restricted_queryset(Device, "change").select_for_update(of=("self",)).get(pk=pk)
    device.name = "x"
    device.save()


def bad_manager(Device, user, pk, transaction):
    with transaction.atomic():
        # ruleid: change-scope-write-needs-read-back
        device = Device.objects.restrict(user, "change").filter(pk=pk).first()
        if device:
            device.save(update_fields=["name"])


def ok_read_back(self, Device, pk):
    # ok: change-scope-write-needs-read-back
    device = self.restricted_queryset(Device, "change").get(pk=pk)
    device.save()
    if not saved_in_scope(device, self.request.user, "change"):
        raise PermissionError


def ok_nested_read_back(self, Device, pk, transaction):
    with transaction.atomic():
        # ok: change-scope-write-needs-read-back
        device = self.restricted_queryset(Device, "change").get(pk=pk)
        device.save()
        if not saved_in_scope(device, self.request.user, "change"):
            raise PermissionError


def ok_view_scope(self, Device, pk):
    # ok: change-scope-write-needs-read-back
    device = self.restricted_queryset(Device).get(pk=pk)
    device.save()


def ok_refusal_helper(self, Device, pk, transaction):
    with transaction.atomic():
        # ok: change-scope-write-needs-read-back
        device = self.restricted_queryset(Device, "change").get(pk=pk)
        device.save()
        if refusal := self._refuse_rows_outside_change_scope(self.request, [device]):
            return refusal
