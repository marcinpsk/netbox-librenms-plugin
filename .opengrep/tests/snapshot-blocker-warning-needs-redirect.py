def refresh(request, result):
    notice = source_cache.blocker_notice(result.blocked["oob"])
    # ruleid: snapshot-blocker-warning-needs-redirect
    messages.warning(request, f"Missing OOB rows: {notice}")


def sync(request, assessed):
    # ruleid: snapshot-blocker-warning-needs-redirect
    messages.warning(request, blocker_notice(assessed["blockers"]))
    if not request.htmx:
        # ok: snapshot-blocker-warning-needs-redirect
        messages.warning(request, blocker_notice(assessed["blockers"]))
    if assessed["blockers"] and not request.htmx:
        # ok: snapshot-blocker-warning-needs-redirect
        messages.warning(request, blocker_notice(assessed["blockers"]))
    # ok: snapshot-blocker-warning-needs-redirect
    messages.success(request, "Refreshed")
