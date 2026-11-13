"""Runtime sync POST gates and the confirmation inventory. Mixin contract tests live in test_permissions.py."""

import pytest

from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

# Views that apply no assessed change carry no confirmation intent (section 4 of the #177 design).
EXEMPT_VIEWS = {
    "SetPreferredServerView": "the operator names the server",
    "EditServerMappingView": "the operator names the server and its IDs",
    "AddDeviceToLibreNMSView": "writes LibreNMS, outside NetBox reconciliation",
    "UpdateDeviceLocationView": "writes LibreNMS, outside NetBox reconciliation",
    "SyncSiteLocationView": "writes LibreNMS, outside NetBox reconciliation",
    "AddBayTemplateView": "an operator-authored command: it materializes the operator's own input",
    "_BaseRelationshipSyncView": "a base without an operation of its own; each subclass is covered",
    "BulkImportConfirmView": "read-only: it renders the import confirmation and writes nothing",
    "DeviceImportPlanUpdateView": "read-only: it revalidates one import row and renders it",
    "SaveUserPrefView": "an operator preference: it stores the operator's own toggle value",
    "AddDeviceTypeMappingView": "an operator-authored mapping: the operator picks the device type for the string",
    "AddPlatformMappingView": "an operator-authored mapping: the operator picks the platform for the OS string",
}

# The class of a confirmation-required view that the design has no resource for yet; it needs a design decision.
NO_RESOURCE_YET = "no resource yet"

# Confirmation-required views that a later increment migrates to the confirmation intent module.
MIGRATION_INVENTORY = {
    "SyncCablesView": "cable",
    "CableRemoteCreateView": "cable",
    "UpdateDeviceNameView": "device_field",
    "UpdateDeviceSerialView": "device_field",
    "UpdateDeviceTypeView": "device_field",
    "UpdateDevicePlatformView": "device_field",
    "UpdateVMPlatformView": "device_field",
    "CreateAndAssignPlatformView": "device_field",
    "CreateAndAssignVMPlatformView": "device_field",
    "AssignVCSerialView": "device_field",
    "RemoveServerMappingView": "mapping_lifecycle",
    "ConvertLegacyLibreNMSIdView": "mapping_lifecycle",
    "SyncInterfacesView": "interface",
    "SyncInterfaceLagView": "interface",
    "SyncInterfaceParentView": "interface",
    "SyncInterfaceBridgeView": "interface",
    "RebindInterfacePortView": "interface",
    "DeleteNetBoxInterfacesView": "interface",
    "SyncIPAddressesView": "ip_address",
    "CreateVRFFromIPRowView": "ip_address",
    "MoveInterfaceToWinnerView": "migration",
    "MoveIPAddressToWinnerView": "migration",
    "TransferDeviceIPView": "migration",
    "InstallModuleView": "module",
    "InstallBranchView": "module",
    "InstallSelectedView": "module",
    "UpdateModuleSerialView": "module",
    "UpdateModuleInterfaceView": "module",
    "ApplyModuleInterfaceTypesView": "module",
    "ReplaceModuleView": "module",
    "MoveModuleView": "module",
    "MarkBuiltInView": "module",
    "DeviceConflictActionView": "import_identity_replacement",
    "PromoteToHostView": "import_identity_replacement",
    "AddAsOOBView": "mapping_lifecycle",
    "CreatePlatformFromImportView": "device_field",
    "MergeNetBoxDevicesView": "migration",
    # It creates objects from the shown import plan, and the design names no import-create resource.
    "BulkImportDevicesView": NO_RESOURCE_YET,
}

# The inventory may only shrink: a new confirmation-required view declares its actions instead.
_INVENTORY_CEILING = frozenset(
    {
        "SyncCablesView",
        "CableRemoteCreateView",
        "UpdateDeviceNameView",
        "UpdateDeviceSerialView",
        "UpdateDeviceTypeView",
        "UpdateDevicePlatformView",
        "UpdateVMPlatformView",
        "CreateAndAssignPlatformView",
        "CreateAndAssignVMPlatformView",
        "AssignVCSerialView",
        "RemoveServerMappingView",
        "ConvertLegacyLibreNMSIdView",
        "SyncInterfacesView",
        "SyncInterfaceLagView",
        "SyncInterfaceParentView",
        "SyncInterfaceBridgeView",
        "RebindInterfacePortView",
        "DeleteNetBoxInterfacesView",
        "SyncIPAddressesView",
        "CreateVRFFromIPRowView",
        "MoveInterfaceToWinnerView",
        "MoveIPAddressToWinnerView",
        "TransferDeviceIPView",
        "InstallModuleView",
        "InstallBranchView",
        "InstallSelectedView",
        "UpdateModuleSerialView",
        "UpdateModuleInterfaceView",
        "ApplyModuleInterfaceTypesView",
        "ReplaceModuleView",
        "MoveModuleView",
        "MarkBuiltInView",
        "DeviceConflictActionView",
        "PromoteToHostView",
        "AddAsOOBView",
        "CreatePlatformFromImportView",
        "MergeNetBoxDevicesView",
        "BulkImportDevicesView",
    }
)


def _discovered_post_views(package=None):
    """Return ``(module name, class name, class)`` for every view class of *package* (views/sync) that handles POST."""
    import importlib
    import inspect
    import pkgutil

    from django.views import View

    from netbox_librenms_plugin.views import sync

    package = package or sync
    views = []
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        module = importlib.import_module(module_info.name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            # hasattr, not __dict__: a view that inherits its handler is still a POST view.
            if cls.__module__ == module.__name__ and issubclass(cls, View) and hasattr(cls, "post"):
                views.append((module_info.name.rsplit(".", 1)[-1], name, cls))
    return views


def pytest_generate_tests(metafunc):
    if "sync_case" not in metafunc.fixturenames:
        return

    cases = []
    for module_name, name, cls in _discovered_post_views():
        # The relationship base can deny before it needs subclass attributes. Cover it too.
        variants = [("device", {})]
        if name in {
            "SyncInterfacesView",
            "RebindInterfacePortView",
            "DeleteNetBoxInterfacesView",
            "SyncInterfaceParentView",
            "SyncIPAddressesView",
            "CreateVRFFromIPRowView",
            "AddDeviceToLibreNMSView",
            "RemoveServerMappingView",
            "SetPreferredServerView",
            "EditServerMappingView",
            "ConvertLegacyLibreNMSIdView",
        }:
            variants.append(("virtualmachine", {}))
        if name == "AddBayTemplateView":
            variants = [
                ("device", {"target_kind": "device_type"}),
                ("device", {"target_kind": "module_type"}),
                ("device", {"mode": "map_existing"}),
            ]
        if name == "CreateAndAssignPlatformView":
            variants.append(("device", {"platform_name": "Existing platform", "manufacturer": "existing"}))
        if name == "SyncVLANsView":
            variants.append(("device", {"confirmation_intent": "grouped"}))
        for object_type, data in variants:
            suffix = "-".join(f"{key}={value}" for key, value in data.items())
            cases.append(pytest.param((cls, object_type, data), id=f"{module_name}.{name}-{object_type}-{suffix}"))
    assert cases, "No sync POST views discovered"
    metafunc.parametrize("sync_case", cases)


def _post_without_writes(view, request, **kwargs):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.tests.view_test_helpers import domain_state, post

    before = domain_state()
    with CaptureQueriesContext(connection) as queries:
        try:
            response = post(view, request, **kwargs)
        finally:
            after = domain_state()
            changed = [table for table in before if before[table] != after[table]]
            assert changed == [], f"Denied POST changed tables: {changed}"
    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}
    ]
    assert writes == [], "Denied POST issued write SQL"
    return response


def _grouped_vlan_token(device):
    """Return an authentic VLAN token that names a VLAN group, so the VLAN route also requires VLANGroup view."""
    from ipam.models import VLANGroup

    from netbox_librenms_plugin import confirmation_intent as intent
    from netbox_librenms_plugin.assessment.vlans import (
        VLANAssumptions,
        VLANChoices,
        VLANCreate,
        VLANCreateEffects,
        VLANIdentity,
    )

    group = VLANGroup.objects.create(name="Permission VLAN group", slug="permission-vlans").pk
    operation = VLANCreate(
        VLANIdentity(100), VLANChoices(group), VLANAssumptions(None), VLANCreateEffects(100, group, "users", "active")
    )
    return intent.offer(operation, intent.IntentContext(1, (device._meta.label_lower, device.pk), "default")).token


def _assert_object_denial(view, request, response, *, htmx, expected_missing=None):
    import json

    from django.http import JsonResponse
    from django.urls import get_script_prefix
    from utilities.permissions import get_permission_for_model

    from netbox_librenms_plugin.tests.view_test_helpers import message_texts

    required = view.required_object_permissions.get("POST", [])
    missing = [
        get_permission_for_model(model, action)
        for action, model in required
        if not request.user.has_perm(get_permission_for_model(model, action))
    ]
    assert len(missing) > 0, "Case must lack a required object permission"
    if expected_missing is not None:
        assert missing == expected_missing
    message = f"Missing permissions: {', '.join(missing)}"
    if isinstance(response, JsonResponse):
        assert response.status_code == 403
        assert json.loads(response.content) == {"error": message}
    else:
        assert response.status_code == (200 if htmx else 302)
        assert response["HX-Redirect" if htmx else "Location"] == get_script_prefix()
        assert message_texts(request) == [message]


@pytest.mark.django_db
class TestSyncPostPermissionGates:
    @pytest.mark.parametrize("htmx", [False, True], ids=["normal", "htmx"])
    def test_missing_object_permissions_refuse_before_work(self, sync_case, htmx):
        import inspect

        from dcim.models import Platform
        from utilities.permissions import get_permission_for_model

        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN
        from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_interface, make_ip, make_vm
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view_class, object_type, variant = sync_case
        device = make_device("permission-device", serial="UNCHANGED")
        interface = make_interface(device, "eth0")
        ip = make_ip("198.18.0.1/24", assigned_object=interface)
        owner = (
            make_vm("permission-vm", make_cluster("permission-cluster")) if object_type == "virtualmachine" else device
        )
        data = {"object_type": object_type, **variant}
        if data.get("manufacturer") == "existing":
            Platform.objects.create(name=data["platform_name"], slug="existing-platform")
            data["manufacturer"] = str(device.device_type.manufacturer_id)
        if data.get("confirmation_intent") == "grouped":
            data["confirmation_intent"] = _grouped_vlan_token(device)
        pk = owner.pk
        if view_class.__name__ == "MoveInterfaceToWinnerView":
            pk = interface.pk
        elif view_class.__name__ == "MoveIPAddressToWinnerView":
            pk = ip.pk
        arguments = {"pk": pk, "object_id": owner.pk, "object_type": object_type, "ip_kind": "primary4"}
        parameters = inspect.signature(view_class.post).parameters
        kwargs = {key: value for key, value in arguments.items() if key in parameters}
        unknown = [
            key
            for key, parameter in parameters.items()
            if key not in {"self", "request", *kwargs}
            and parameter.default is inspect.Parameter.empty
            and parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        ]
        assert unknown == [], "Supply real route arguments for the new sync POST"
        user = make_user_with_perms("permission-reader", [])
        assert not user.is_superuser
        assert user.has_perm(PERM_CHANGE_PLUGIN)
        request = make_request(data=data, user=user, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
        view = view_class()

        response = _post_without_writes(view, request, **kwargs)

        _assert_object_denial(view, request, response, htmx=htmx)

        required = tuple(dict.fromkeys(view.required_object_permissions.get("POST", [])))
        assert required, "Sync POST must declare at least one object permission"
        for index, omitted in enumerate(required):
            granted = [requirement for requirement in required if requirement != omitted]
            user = make_user_with_perms(f"permission-omission-{index}", granted)
            assert all(user.has_perm(get_permission_for_model(model, action)) for action, model in granted)
            request = make_request(data=data, user=user, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
            view = view_class()

            response = _post_without_writes(view, request, **kwargs)

            action, model = omitted
            missing_permission = get_permission_for_model(model, action)
            assert not user.has_perm(missing_permission)
            _assert_object_denial(
                view,
                request,
                response,
                htmx=htmx,
                expected_missing=[missing_permission],
            )

    @pytest.mark.parametrize("allow_create", [False, True], ids=["missing-add-platform", "permitted-control"])
    def test_platform_creation_with_valid_payload(self, allow_create, settings, librenms_server):
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms, post
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        servers = {
            "permission-test": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}
        }
        configure_librenms_servers(settings, servers)
        server_key = next(iter(servers))
        device = seed_mapping(make_device("platform-permission-device"), server_key, own=7501)
        librenms_server.device_info_response(device_id=7501, hostname=device.name, os="permission-os")
        perms = [("change", Device)] + ([("add", Platform)] if allow_create else [])
        user = make_user_with_perms("platform-creator", perms)
        data = {"platform_name": "Permission platform", "librenms_os": "permission-os", "server_key": server_key}
        request = make_request(data=data, user=user)
        view = CreateAndAssignPlatformView()
        count = Platform.objects.count()

        if allow_create:
            response = post(view, request, pk=device.pk)
            assert response.status_code == 302
            assert Platform.objects.count() == count + 1
            device.refresh_from_db()
            assert device.platform.name == "Permission platform"
            # The OS and version come from LibreNMS, not from the form.
            assert librenms_server.requests
        else:
            response = _post_without_writes(view, request, pk=device.pk)
            _assert_object_denial(view, request, response, htmx=False)
            assert Platform.objects.count() == count
            device.refresh_from_db()
            assert device.platform_id is None
            assert librenms_server.requests == []


def _confirmation_views():
    """Return every discovered POST view of views/sync and views/imports, inherited handlers included."""
    from netbox_librenms_plugin.views import imports, sync

    return {name: cls for package in (sync, imports) for _module, name, cls in _discovered_post_views(package)}


class TestConfirmationInventory:
    """Every POST view declares its confirmation actions, a named exemption, or a migration inventory entry."""

    def test_every_post_view_has_exactly_one_confirmation_class(self):
        from netbox_librenms_plugin import confirmation_intent as intent

        unclassified = []
        for name, cls in _confirmation_views().items():
            classes = [
                isinstance(getattr(cls, "confirmation_policy", None), intent.RoutePolicy),
                name in EXEMPT_VIEWS,
                name in MIGRATION_INVENTORY,
            ]
            if classes.count(True) != 1:
                unclassified.append(name)
        assert unclassified == []

    def test_the_lists_name_only_discovered_views(self):
        from netbox_librenms_plugin import confirmation_intent as intent

        views = _confirmation_views()
        assert set(EXEMPT_VIEWS) - set(views) == set()
        assert set(MIGRATION_INVENTORY) - set(views) == set()
        assert all(
            resource in set(intent.Resource) or resource == NO_RESOURCE_YET for resource in MIGRATION_INVENTORY.values()
        )

    def test_the_views_without_a_resource_are_deferred_for_a_design_decision(self):
        # The last increment's empty inventory forces the decision on each of them.
        deferred = {name for name, resource in MIGRATION_INVENTORY.items() if resource == NO_RESOURCE_YET}
        assert deferred == {"BulkImportDevicesView"}

    def test_the_migration_inventory_only_shrinks(self):
        assert set(MIGRATION_INVENTORY) - _INVENTORY_CEILING == set()

    def test_every_declared_action_is_registered_and_has_a_contract_scenario(self):
        from netbox_librenms_plugin import confirmation_intent as intent
        from netbox_librenms_plugin.tests.test_confirmation_intent_contract import SCENARIOS

        declared = {
            (resource, action)
            for cls in _confirmation_views().values()
            if isinstance(policy := getattr(cls, "confirmation_policy", None), intent.RoutePolicy)
            for resource, action, _role in policy.entries
        }
        assert declared
        assert declared <= intent.registered_actions()
        assert declared <= set(SCENARIOS)
        # A resource with a registered action has no view left in the inventory.
        assert {resource for resource, _action in intent.registered_actions()} & set(
            MIGRATION_INVENTORY.values()
        ) == set()
