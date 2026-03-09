"""Comprehensive coverage tests for import_utils/bulk_import.py.

Targets the following uncovered lines (from coverage report):
  129, 140, 146-147, 183, 203-254, 310, 336-365, 373,
  393-395, 400-402, 420-421, 469, 489, 495-511, 520-537,
  548-566, 596-598, 604-641, 665, 687-694
"""

from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_job(logger=True):
    """Return a minimal JobRunner-like mock.

    Args:
        logger: If True (default) attach a MagicMock logger; if False set it
                to None so the ``else: logger.warning(...)`` branches fire.
    """
    job = MagicMock()
    job.job = MagicMock()
    job.job.job_id = "test-uuid-1234"
    job.job.pk = 1
    job.logger = MagicMock() if logger else None
    return job


def _make_rq_running():
    """RQ job that is actively running (not stopped/failed)."""
    rq_job = MagicMock()
    rq_job.is_stopped = False
    rq_job.is_failed = False
    rq_job.get_status.return_value = "started"
    return rq_job


def _make_rq_stopped():
    """RQ job that has been stopped."""
    rq_job = MagicMock()
    rq_job.is_stopped = True
    rq_job.is_failed = False
    rq_job.get_status.return_value = "stopped"
    return rq_job


def _make_validation(existing_device=None, import_as_vm=False, issues=None):
    """Minimal valid validation dict used throughout the tests."""
    return {
        "resolved_name": "test-device",
        "is_ready": True,
        "can_import": True,
        "status": "active",
        "existing_device": existing_device,
        "import_as_vm": import_as_vm,
        "existing_match_type": None,
        "virtual_chassis": {"is_stack": False},
        "site": {"found": True, "site": MagicMock()},
        "device_type": {"found": True, "device_type": MagicMock()},
        "device_role": {"found": True, "role": MagicMock()},
        "platform": {"found": True, "platform": MagicMock()},
        "cluster": {"found": True},
        "issues": issues or [],
    }


def _make_import_result(success=True, device=None, message="Imported", error=None):
    """Return value for mocked ``import_single_device``."""
    return {
        "success": success,
        "device": device or (MagicMock() if success else None),
        "message": message,
        "error": error or ("" if success else "Import failed"),
    }


# ===========================================================================
# 1. TestBulkImportDevices – covers line 310
# ===========================================================================


class TestBulkImportDevices:
    """Tests for the thin ``bulk_import_devices`` wrapper (line 310)."""

    def test_delegates_to_shared_with_job_none(self):
        """bulk_import_devices must call bulk_import_devices_shared with job=None."""
        with patch(
            "netbox_librenms_plugin.import_utils.bulk_import.bulk_import_devices_shared"
        ) as mock_shared:
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

            expected = {
                "total": 2,
                "success": [],
                "failed": [],
                "skipped": [],
                "virtual_chassis_created": 0,
            }
            mock_shared.return_value = expected
            user = MagicMock()

            result = bulk_import_devices(
                device_ids=[1, 2],
                server_key="default",
                sync_options={"use_sysname": True},
                manual_mappings_per_device={1: {"device_role_id": 5}},
                libre_devices_cache={1: {"hostname": "test"}},
                user=user,
            )

        mock_shared.assert_called_once_with(
            device_ids=[1, 2],
            server_key="default",
            sync_options={"use_sysname": True},
            manual_mappings_per_device={1: {"device_role_id": 5}},
            libre_devices_cache={1: {"hostname": "test"}},
            job=None,
            user=user,
        )
        assert result == expected


# ===========================================================================
# 2. TestBulkImportDevicesShared – covers lines 129, 140, 146-147,
#    183, 203-254
# ===========================================================================


class TestBulkImportDevicesShared:
    """Tests for ``bulk_import_devices_shared``."""

    # ------------------------------------------------------------------
    # Lines 129 & 140 – "else: logger.warning(...)" when job.logger=None
    # ------------------------------------------------------------------

    def test_rq_stopped_logs_via_module_logger_when_job_logger_none(self):
        """job.logger=None: module logger.warning fires on RQ stop (line 129)."""
        job = _make_job(logger=False)  # job.logger is None → else branch
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        mock_import.assert_not_called()
        mock_logger.warning.assert_called()
        assert result["success"] == []

    def test_db_fallback_logs_via_module_logger_when_job_logger_none(self):
        """job.logger=None: module logger.warning fires on DB fallback cancel (line 140)."""
        job = _make_job(logger=False)  # job.logger is None → else branch
        job.job.status = "failed"
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
            patch("django_rq.get_queue", side_effect=Exception("RQ unavailable")),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        mock_import.assert_not_called()
        mock_logger.warning.assert_called()
        job.job.refresh_from_db.assert_called_once()

    # ------------------------------------------------------------------
    # Lines 146-147 – libre_devices_cache hit path
    # ------------------------------------------------------------------

    def test_libre_devices_cache_hit_skips_api_call(self):
        """Devices in libre_devices_cache skip the API call (lines 146-147)."""
        libre_cache = {
            1: {"device_id": 1, "hostname": "cached-host"},
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI") as mock_api_cls,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
        ):
            mock_api = MagicMock()
            mock_api.server_key = "default"
            mock_api_cls.return_value = mock_api

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # API.get_device_info should NOT have been called for this device
        mock_api.get_device_info.assert_not_called()
        assert len(result["success"]) == 1

    # ------------------------------------------------------------------
    # Line 183 – job.logger.info("Imported device X of Y")
    # ------------------------------------------------------------------

    def test_successful_import_emits_job_progress_log(self):
        """job.logger.info('Imported device X of Y') on success (line 183)."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["success"]) == 1
        job.logger.info.assert_any_call("Imported device 1 of 1")

    # ------------------------------------------------------------------
    # Lines 203-254 – virtual chassis creation (is_stack=True)
    # ------------------------------------------------------------------

    def test_vc_creation_triggered_for_stack(self):
        """is_stack=True → create_virtual_chassis_with_members called (lines 203-238)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_device = MagicMock()
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [
                {"serial": "SN001", "position": 1},
                {"serial": "SN002", "position": 2},
            ],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(device=mock_device),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1
        assert len(result["success"]) == 1

    def test_vc_creation_with_job_logger(self):
        """VC creation with job → job.logger.info logged (lines 234-237)."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert result["virtual_chassis_created"] == 1
        # Confirm job logger was called for the VC creation
        assert any("VC" in str(c) for c in job.logger.info.call_args_list)

    def test_vc_creation_deduplicates_by_member_serials(self):
        """Two devices with identical member serials → VC created only once (lines 217-226)."""
        libre_cache = {
            1: {"device_id": 1, "hostname": "stack-1"},
            2: {"device_id": 2, "hostname": "stack-2"},
        }
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        # Both devices share the same physical stack (same member serials)
        shared_vc_data = {
            "is_stack": True,
            "members": [
                {"serial": "SN001", "position": 1},
                {"serial": "SN002", "position": 2},
            ],
        }
        v1 = _make_validation()
        v1["virtual_chassis"] = shared_vc_data
        v2 = _make_validation()
        v2["virtual_chassis"] = shared_vc_data

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=[v1, v2],
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1, 2],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        assert mock_create_vc.call_count == 1
        assert result["virtual_chassis_created"] == 1

    def test_vc_creation_failure_continues_import(self):
        """VC creation exception → import device still succeeds (lines 239-247)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                side_effect=Exception("VC creation failed"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # Import succeeded despite VC failure
        assert len(result["success"]) == 1
        assert result["virtual_chassis_created"] == 0
        mock_create_vc.assert_called_once()

    def test_vc_no_member_serials_uses_device_id_domain(self):
        """Members with no valid serials → vc_domain falls back to device_id (lines 219-221)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_vc = MagicMock()
        mock_vc.name = "VC-1"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [
                {"serial": None, "position": 1},
                {"serial": "-", "position": 2},
            ],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1

    def test_failed_import_with_job_logs_error(self):
        """result.success=False, device=None → job.logger.error called (lines 252-254)."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(success=False, device=None, error="Import failed"),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["failed"]) == 1
        job.logger.error.assert_called()

    def test_manual_mappings_applied_to_device(self):
        """manual_mappings_per_device overrides are applied for the matching device (line 183)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        captured_mappings = {}

        def capture_import(device_id, server_key, validation, sync_options, manual_mappings, libre_device):
            captured_mappings.update(manual_mappings or {})
            return _make_import_result()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                side_effect=capture_import,
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
                manual_mappings_per_device={1: {"device_role_id": 42}},
            )

        assert result["success"]
        assert captured_mappings.get("device_role_id") == 42

    def test_device_skipped_when_already_exists(self):
        """result.success=False, result.device is truthy → device skipped (line 250)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        existing_device = MagicMock()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(
                    success=False, device=existing_device, error="Device already exists"
                ),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "Device already exists"
        assert result["failed"] == []

    def test_vc_creation_failure_with_job_logs_warning(self):
        """VC failure with job.logger set → job.logger.warning fired (line 244)."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                side_effect=Exception("VC error"),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["success"]) == 1
        assert result["virtual_chassis_created"] == 0
        job.logger.warning.assert_called()


# ===========================================================================
# 3. TestRefreshExistingDevice – covers lines 336-365, 373, 393-395,
#    400-402, 420-421
# ===========================================================================


class TestRefreshExistingDevice:
    """Tests for ``_refresh_existing_device``."""

    # ------------------------------------------------------------------
    # Lines 336-341: Device path → refreshed device with role
    # ------------------------------------------------------------------

    def test_device_path_refreshes_role(self):
        """Non-VM existing device refreshed; role updated on result (lines 336-341)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 1
        refreshed = MagicMock()
        refreshed.role = MagicMock(name="switch")

        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "device_role": {},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            _refresh_existing_device(validation)

        assert validation["existing_device"] is refreshed
        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] is refreshed.role

    # ------------------------------------------------------------------
    # Lines 342-345: Device path → refreshed device without role
    # ------------------------------------------------------------------

    def test_device_path_refreshes_no_role(self):
        """Refreshed device has no role → device_role = {'found': False} (lines 342-345)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 2
        refreshed = MagicMock()
        refreshed.role = None  # role removed since caching

        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "device_role": {"found": True, "role": MagicMock()},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            _refresh_existing_device(validation)

        assert validation["existing_device"] is refreshed
        assert validation["device_role"] == {"found": False}

    # ------------------------------------------------------------------
    # Lines 346-365: Existing device was deleted (Device.objects returns None)
    # ------------------------------------------------------------------

    def test_deleted_device_clears_existing_and_recomputes_readiness(self):
        """Device deleted → existing_device=None, readiness recomputed (lines 346-365)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 3
        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "issues": [],
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": True},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = None  # deleted
            _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["device_role"] == {}
        assert validation["can_import"] is True   # no issues
        assert validation["is_ready"] is False    # device_role.found is now missing

    def test_deleted_vm_recomputes_readiness_from_cluster(self):
        """VM deleted → is_ready reflects cluster.found (lines 354-356)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 4
        validation = {
            "existing_device": existing,
            "import_as_vm": True,
            "issues": [],
            "cluster": {"found": True},
        }

        with patch("virtualization.models.VirtualMachine") as mock_VM:
            mock_VM.objects.filter.return_value.first.return_value = None  # deleted
            _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["can_import"] is True
        assert validation["is_ready"] is True  # cluster.found=True

    def test_deleted_vm_not_ready_when_no_cluster(self):
        """Deleted VM with no cluster → is_ready=False."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 5
        validation = {
            "existing_device": existing,
            "import_as_vm": True,
            "issues": ["some issue"],
            "cluster": {"found": False},
        }

        with patch("virtualization.models.VirtualMachine") as mock_VM:
            mock_VM.objects.filter.return_value.first.return_value = None
            _refresh_existing_device(validation)

        assert validation["can_import"] is False   # has issues
        assert validation["is_ready"] is False

    # ------------------------------------------------------------------
    # Lines 366-368: Exception caught → logger.error called
    # ------------------------------------------------------------------

    def test_exception_during_refresh_logs_error(self):
        """DB error during refresh is caught and logged (lines 366-368)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 99
        validation = {"existing_device": existing, "import_as_vm": False}

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            mock_Device.objects.filter.side_effect = Exception("DB down")
            _refresh_existing_device(validation)  # must not raise

        mock_logger.error.assert_called_once()
        assert "99" in mock_logger.error.call_args[0][0]

    # ------------------------------------------------------------------
    # Line 373: no existing_device, no libre_device → early return
    # ------------------------------------------------------------------

    def test_no_existing_device_no_libre_device_returns_early(self):
        """existing=None + libre_device=None → immediate return (line 373)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = {"existing_device": None}
        # No exception and validation unchanged
        _refresh_existing_device(validation, libre_device=None)
        assert validation == {"existing_device": None}

    # ------------------------------------------------------------------
    # Lines 393-395: existing=None, found via librenms_id
    # ------------------------------------------------------------------

    def test_no_existing_device_found_by_librenms_id(self):
        """existing=None: device found by librenms_id custom field (lines 393-395)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = MagicMock(name="switch")
        libre_device = {"device_id": 42, "hostname": "sw01", "sysName": "sw01"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw01",
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=new_device,
            ),
        ):
            mock_Device.objects.filter.return_value.first.return_value = None
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "librenms_id"
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        # Device has a role → device_role should be set
        assert validation["device_role"]["found"] is True

    # ------------------------------------------------------------------
    # Lines 400-402: existing=None, found by resolved_name
    # ------------------------------------------------------------------

    def test_no_existing_device_found_by_resolved_name(self):
        """existing=None: not found by librenms_id, but found by resolved_name (lines 400-402)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None  # no role
        libre_device = {"device_id": 43, "hostname": "sw02", "sysName": "sw02"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw02-resolved",
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=None,
            ),
        ):
            # resolved_name match
            mock_Device.objects.filter.return_value.first.return_value = new_device
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "resolved_name"
        assert validation["can_import"] is False

    # ------------------------------------------------------------------
    # Lines 420-421: exception in the "no existing device" lookup path
    # ------------------------------------------------------------------

    def test_exception_during_new_device_lookup_logs_error(self):
        """Exception in the newly-imported-device check is caught and logged (lines 420-421)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        libre_device = {"device_id": 44, "hostname": "sw03", "sysName": "sw03"}
        validation = {"existing_device": None, "import_as_vm": False, "resolved_name": None}

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                side_effect=Exception("lookup failed"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        mock_logger.error.assert_called()

    def test_no_existing_device_non_numeric_librenms_id_skips_id_lookup(self):
        """Non-numeric device_id raises ValueError → except pass (line 395), falls back to name."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None
        # Non-numeric device_id triggers ValueError inside int() → except (ValueError, TypeError): pass
        libre_device = {"device_id": "not-an-int", "hostname": "sw05", "sysName": "sw05"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw05",
        }

        with (
            patch("dcim.models.Device") as mock_Device,
        ):
            mock_Device.objects.filter.return_value.first.return_value = new_device
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Should find via resolved_name (librenms_id lookup was skipped due to ValueError)
        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "resolved_name"
        """existing=None, not found by id or resolved_name → hostname fallback."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None
        libre_device = {"device_id": 45, "hostname": "sw04", "sysName": "sw04-sysname"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": None,  # no resolved_name → fall through to hostname
        }

        # set up: filter returns new_device on the first call (hostname match)
        filter_results = iter([new_device])

        def filter_first(*args, **kwargs):
            m = MagicMock()
            m.first.return_value = next(filter_results, None)
            return m

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=None,
            ),
        ):
            mock_Device.objects.filter.side_effect = filter_first
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "hostname"


# ===========================================================================
# 4. TestProcessDeviceFilters – covers lines 469, 489, 495-511, 520-537,
#    548-566, 596-598, 604-641, 665, 687-694
# ===========================================================================


class TestProcessDeviceFilters:
    """Tests for ``process_device_filters``."""

    # Minimal set of patches required for every call
    _BASE_PATCHES = [
        "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
        "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
        "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
        "netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data",
        "netbox_librenms_plugin.import_utils.bulk_import.cache",
        "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
        "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
        "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
    ]

    def _make_api(self, server_key="default", cache_timeout=300):
        api = MagicMock()
        api.server_key = server_key
        api.cache_timeout = cache_timeout
        return api

    def _make_device(self, device_id=1, hostname="sw01"):
        return {"device_id": device_id, "hostname": hostname, "disabled": 0}

    # ------------------------------------------------------------------
    # Lines 469, 489: job logger on fetch and device-count messages
    # ------------------------------------------------------------------

    def test_job_logs_fetch_and_count_messages(self):
        """With job set, info logs for 'Fetching' and 'Found N devices' fire (lines 469, 489)."""
        job = _make_job()
        device = self._make_device()
        api = self._make_api()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert len(result) == 1
        # Verify the two job-specific log calls were made
        log_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("Fetching" in s for s in log_calls)
        assert any("Found" in s for s in log_calls)

    # ------------------------------------------------------------------
    # Lines 495-506: VC prefetch with job logging
    # ------------------------------------------------------------------

    def test_vc_prefetch_with_job_logs_prefetch_messages(self):
        """With vc_detection_enabled+job, prefetch job-log messages fire (lines 496-506)."""
        job = _make_job()
        device = self._make_device()
        api = self._make_api()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices"
            ) as mock_prefetch,
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        mock_prefetch.assert_called_once()
        log_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("Pre-fetch" in s or "pre-fetch" in s or "virtual chassis" in s.lower() for s in log_calls)

    # ------------------------------------------------------------------
    # Lines 507-511: BrokenPipeError during VC prefetch + request set
    # ------------------------------------------------------------------

    def test_vc_prefetch_client_disconnect_with_request_returns_empty(self):
        """BrokenPipeError during prefetch + request set → _empty_return (lines 507-510)."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=False,
                show_disabled=True,
                request=request,
            )

        assert result == []

    def test_vc_prefetch_client_disconnect_with_return_cache_status(self):
        """BrokenPipeError + request + return_cache_status=True → ([], False)."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=False,
                show_disabled=True,
                request=request,
                return_cache_status=True,
            )

        assert result == ([], False)

    def test_vc_prefetch_client_disconnect_no_request_reraises(self):
        """BrokenPipeError during prefetch with request=None → exception re-raised (line 511)."""
        import pytest

        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            with pytest.raises(BrokenPipeError):
                process_device_filters(
                    api,
                    filters={},
                    vc_detection_enabled=True,
                    clear_cache=False,
                    show_disabled=True,
                    request=None,
                )

    # ------------------------------------------------------------------
    # Lines 520-531: Job pre-loop RQ check → job was already stopped
    # ------------------------------------------------------------------

    def test_job_rq_stopped_before_validation_loop_returns_empty(self):
        """RQ job stopped before loop → empty result returned (lines 529-531)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []
        job.logger.warning.assert_called()

    def test_job_rq_stopped_before_loop_with_cache_status(self):
        """RQ job stopped + return_cache_status=True → ([], False) (line 531)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
                return_cache_status=True,
            )

        assert result == ([], False)

    # ------------------------------------------------------------------
    # Lines 532-537: Job pre-loop RQ raises → DB fallback → stopped
    # ------------------------------------------------------------------

    def test_job_db_fallback_stopped_before_validation_loop(self):
        """RQ raises → DB status says failed → return empty (lines 532-537)."""
        job = _make_job()
        job.job.status = "failed"
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue", side_effect=Exception("RQ down")),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []
        job.job.refresh_from_db.assert_called()

    def test_job_db_errored_before_validation_loop(self):
        """RQ raises → DB status 'errored' → return empty (line 535-537)."""
        job = _make_job()
        job.job.status = "errored"
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue", side_effect=Exception("RQ down")),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    # ------------------------------------------------------------------
    # Lines 548-560: Per-device loop RQ stop detected
    # ------------------------------------------------------------------

    def test_job_validation_loop_rq_stop_returns_empty(self):
        """RQ stop detected during loop at idx=1 → return empty (lines 548-560)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            # Pre-loop check: running; loop check: stopped
            mock_rq_cls.fetch.side_effect = [_make_rq_running(), _make_rq_stopped()]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    # ------------------------------------------------------------------
    # Lines 561-566: Per-device loop DB fallback stop
    # ------------------------------------------------------------------

    def test_job_validation_loop_db_fallback_stop(self):
        """RQ raises in loop → DB fallback detects failed status (lines 561-566)."""
        job = _make_job()
        job.job.status = "failed"
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            # Pre-loop check: running; loop check: raises
            mock_rq_cls.fetch.side_effect = [_make_rq_running(), Exception("RQ error in loop")]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    # ------------------------------------------------------------------
    # Lines 584-601: Cache-hit path (clear_cache=False + cache miss=None)
    # NOTE: cache hit with exclude_existing=False is in existing tests.
    # ------------------------------------------------------------------

    def test_cache_hit_uses_cached_validation(self):
        """Cache hit → device validation taken from cache (lines 585-601)."""
        api = self._make_api()
        device = self._make_device()

        cached_validation = _make_validation()  # existing_device=None → _refresh returns early
        cached_entry = dict(device)
        cached_entry["_validation"] = cached_validation

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import"
            ) as mock_validate,
        ):
            # First get → device cache hit; second get → metadata (truthy)
            mock_cache.get.side_effect = [cached_entry, MagicMock()]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,
                show_disabled=True,
            )

        # validate_device_for_import must NOT be called (cache hit)
        mock_validate.assert_not_called()
        assert len(result) == 1

    # ------------------------------------------------------------------
    # Lines 596-598: Cache-hit + exclude_existing → device skipped
    # ------------------------------------------------------------------

    def test_cache_hit_with_exclude_existing_skips_device(self):
        """Cache hit + exclude_existing + existing_device → device skipped (lines 596-598)."""
        api = self._make_api()
        device = self._make_device()

        existing = MagicMock()
        existing.pk = 1
        cached_validation = _make_validation(existing_device=existing)
        # Ensure _refresh_existing_device returns quickly without a real DB call
        cached_validation["existing_device"] = existing
        cached_entry = dict(device)
        cached_entry["_validation"] = cached_validation

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            # Patch _refresh_existing_device to be a no-op so we can control existing_device
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import._refresh_existing_device"
            ),
        ):
            mock_cache.get.return_value = cached_entry

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,
                show_disabled=True,
                exclude_existing=True,
            )

        # Device should be excluded because existing_device is set
        assert result == []

    # ------------------------------------------------------------------
    # Lines 604-641: Validate-and-cache path (clear_cache=True)
    # ------------------------------------------------------------------

    def test_validate_and_cache_path_no_vc_detection(self):
        """clear_cache=True → validate + set empty VC data + cache stored (lines 604-641)."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data",
                return_value={"is_stack": False},
            ) as mock_empty_vc,
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
            )

        assert len(result) == 1
        # empty_virtual_chassis_data should have been called for vc=False
        mock_empty_vc.assert_called()
        # cache.set called for the validated device and simple key
        assert mock_cache.set.call_count >= 2

    def test_validate_path_exclude_existing_skips_device(self):
        """validate path + exclude_existing + existing_device → device skipped (lines 624-626)."""
        api = self._make_api()
        device = self._make_device()

        validation_with_existing = _make_validation(existing_device=MagicMock())

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation_with_existing,
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=True,
            )

        assert result == []

    def test_validate_path_client_disconnect_with_request_returns_empty(self):
        """validate raises BrokenPipeError + request set → _empty_return (lines 614-617)."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=BrokenPipeError("client gone"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                request=request,
            )

        assert result == []

    def test_validate_path_client_disconnect_no_request_reraises(self):
        """validate raises BrokenPipeError, request=None → re-raised (line 618)."""
        import pytest

        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=BrokenPipeError("client gone"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            with pytest.raises(BrokenPipeError):
                process_device_filters(
                    api,
                    filters={},
                    vc_detection_enabled=False,
                    clear_cache=True,
                    show_disabled=True,
                    request=None,
                )

    # ------------------------------------------------------------------
    # Line 665: pass – metadata already exists and should_update=False
    # ------------------------------------------------------------------

    def test_cache_metadata_not_updated_when_from_cache_and_existing(self):
        """from_cache=True + existing metadata → metadata preserved (line 665 pass branch)."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),  # from_cache=True
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            existing_metadata = {"cached_at": "2024-01-01T00:00:00+00:00", "cache_timeout": 300}
            # First cache.get → None (no device cache hit → forces validation)
            # Second cache.get → existing metadata (truthy)
            mock_cache.get.side_effect = [None, existing_metadata]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,  # not clearing cache
                show_disabled=True,
            )

        assert len(result) == 1
        # cache.set for metadata should NOT have been called (existing preserved)
        set_calls = [c for c in mock_cache.set.call_args_list if "mkey" in str(c)]
        assert len(set_calls) == 0

    # ------------------------------------------------------------------
    # Lines 604-641: Cache metadata stored when clear_cache=False + from_cache=False
    # ------------------------------------------------------------------

    def test_cache_metadata_stored_when_fresh_data(self):
        """Fresh data (from_cache=False) → metadata and index stored (lines 666-684)."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),  # from_cache=False
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit anywhere

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
            )

        assert len(result) == 1
        # cache.set must have been called at least for metadata
        assert mock_cache.set.call_count >= 1

    # ------------------------------------------------------------------
    # Lines 687-694: Final job logging
    # ------------------------------------------------------------------

    def test_job_final_log_without_exclude_existing(self):
        """With job + validated devices (no exclude_existing) → final log (lines 693-694)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=False,
                job=job,
            )

        assert len(result) == 1
        final_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("Validation complete" in s for s in final_calls)

    def test_job_final_log_with_exclude_existing(self):
        """With job + exclude_existing + some devices filtered → extended final log (lines 688-692)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        # Device will be excluded because it has an existing_device
        validation_with_existing = _make_validation(existing_device=MagicMock())

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation_with_existing,
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.return_value = None
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=True,
                job=job,
            )

        # Device excluded → empty result; job should log the "filtered out" message
        assert result == []
        final_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("filtered out" in s for s in final_calls)

    # ------------------------------------------------------------------
    # Lines 698-699: return_cache_status=True → returns tuple
    # ------------------------------------------------------------------

    def test_return_cache_status_true_returns_tuple(self):
        """return_cache_status=True → (devices, from_cache) tuple (lines 698-699)."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                return_cache_status=True,
            )

        assert isinstance(result, tuple)
        devices, from_cache = result
        assert len(devices) == 1
        assert from_cache is True
