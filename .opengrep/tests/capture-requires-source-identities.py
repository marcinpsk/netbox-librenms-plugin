def validate(snapshot, blockers):
    for blocker in blockers:
        for record in snapshot.responses.values():
            # ruleid: capture-requires-source-identities
            if record.request.path == blocker.path:
                require(record)
            # ruleid: capture-requires-source-identities
            if blocker.path == record.request.path:
                require(record)


def validate(snapshot, mapping, stack_request):
    # ok: capture-requires-source-identities
    if mapping.state is MappingState.INVALID:
        raise RuntimeError(INVALID_OOB_REASON)
    for part, key in mapping.parts:
        # ok: capture-requires-source-identities
        record = snapshot.responses[key]
        # ok: capture-requires-source-identities
        require(record, optional_status=record.request == stack_request)


def display(blocker):
    # ok: capture-requires-source-identities
    return blocker.path
