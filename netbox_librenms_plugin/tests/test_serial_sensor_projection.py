"""Serial projection rejects recognized malformed rows before link mapping."""

import pytest

TYPE_MAP = {"acsSerialPortTable": "ttyS{N}"}
ROW = {"sensor_type": "acsSerialPortTable", "sensor_id": "7", "sensor_index": "serial.3"}


@pytest.mark.parametrize("deleted", [False, True, "bad", 2, None, []])
def test_a_malformed_deletion_flag_invalidates_the_projection(deleted):
    from netbox_librenms_plugin.serial_utils import project_serial_sensors

    result = project_serial_sensors([{**ROW, "sensor_deleted": deleted}], TYPE_MAP)

    assert not result.ready
    assert "sensor_deleted" in result.reason
    assert result.type_map == TYPE_MAP


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"sensor_id": False}, "sensor_id"),
        ({"sensor_index": "bad"}, "sensor_index"),
        ({"sensor_index": 1.3}, "sensor_index"),
    ],
)
def test_invalid_active_sensor_identity_or_index_invalidates_projection(changes, reason):
    from netbox_librenms_plugin.serial_utils import project_serial_sensors

    result = project_serial_sensors([{**ROW, **changes}], TYPE_MAP)

    assert not result.ready
    assert reason in result.reason


@pytest.mark.parametrize("deleted", [1, "1"])
def test_deleted_rows_are_dropped_before_identity_and_index_validation(deleted):
    from netbox_librenms_plugin.serial_utils import project_serial_sensors

    removed = {**ROW, "sensor_deleted": deleted, "sensor_id": False, "sensor_index": "bad"}
    result = project_serial_sensors([ROW, removed], TYPE_MAP)

    assert result.ready
    assert result.rows == [{**ROW, "sensor_id": 7}]
    assert result.type_map == TYPE_MAP
    assert project_serial_sensors([removed], TYPE_MAP).rows == []


def test_unrecognized_types_do_not_participate_in_serial_validation():
    from netbox_librenms_plugin.serial_utils import project_serial_sensors

    unrelated = [{"sensor_type": "temperature", "sensor_deleted": []}, {"sensor_type": []}]
    result = project_serial_sensors(unrelated, TYPE_MAP)

    assert result.ready
    assert result.rows == []
