def _export_responses(snapshot):
    # ruleid: recording-export-needs-response-keys
    for record in snapshot.responses.values():
        export(record)


def _recording_records(snapshot):
    # ruleid: recording-export-needs-response-keys
    yield from snapshot.responses.values()


def _export_responses(snapshot):
    # ok: recording-export-needs-response-keys
    for key, record in snapshot.responses.items():
        if key not in neighbour_keys:
            export(record)


def another_reader(snapshot):
    # ok: recording-export-needs-response-keys
    for record in snapshot.responses.values():
        read(record)
