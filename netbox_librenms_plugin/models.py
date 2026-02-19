import re

from dcim.choices import InterfaceTypeChoices
from dcim.models import DeviceType, ModuleType
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel


class LibreNMSSettings(models.Model):
    """
    Model to store LibreNMS plugin settings, specifically which server to use
    when multiple servers are configured.
    """

    selected_server = models.CharField(
        max_length=100,
        default="default",
        help_text="The key of the selected LibreNMS server from configuration",
    )

    vc_member_name_pattern = models.CharField(
        max_length=100,
        default="-M{position}",
        help_text="Pattern for naming virtual chassis member devices. "
        "Available placeholders: {position}, {serial}. "
        "Example: '-M{position}' results in 'switch01-M2'",
    )

    use_sysname_default = models.BooleanField(
        default=True,
        help_text="Use SNMP sysName instead of LibreNMS hostname when importing devices",
    )

    strip_domain_default = models.BooleanField(
        default=False,
        help_text="Remove domain suffix from device names during import",
    )

    class Meta:
        """Meta options for LibreNMSSettings."""

        verbose_name = "LibreNMS Settings"
        verbose_name_plural = "LibreNMS Settings"

    def get_absolute_url(self):
        """Return the URL for the settings page."""
        return reverse("plugins:netbox_librenms_plugin:settings")

    def __str__(self):
        return f"LibreNMS Settings - Server: {self.selected_server}"


class InterfaceTypeMapping(NetBoxModel):
    """Map LibreNMS interface types and speeds to NetBox interface types."""

    librenms_type = models.CharField(max_length=100)
    netbox_type = models.CharField(
        max_length=50,
        choices=InterfaceTypeChoices,
        default=InterfaceTypeChoices.TYPE_OTHER,
    )
    librenms_speed = models.BigIntegerField(null=True, blank=True)
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this interface type mapping",
    )

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:interfacetypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for InterfaceTypeMapping."""

        unique_together = ["librenms_type", "librenms_speed"]
        ordering = ["librenms_type", "librenms_speed"]

    def __str__(self):
        return f"{self.librenms_type} + {self.librenms_speed} -> {self.netbox_type}"


class DeviceTypeMapping(NetBoxModel):
    """Map LibreNMS hardware strings to NetBox DeviceType objects."""

    librenms_hardware = models.CharField(
        max_length=255,
        unique=True,
        help_text="Hardware string as reported by LibreNMS (e.g., 'Juniper MX480 Internet Backbone Router')",
    )
    netbox_device_type = models.ForeignKey(
        DeviceType,
        on_delete=models.CASCADE,
        related_name="librenms_mappings",
        help_text="The NetBox DeviceType this hardware string maps to",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:devicetypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for DeviceTypeMapping."""

        ordering = ["librenms_hardware"]

    def __str__(self):
        return f"{self.librenms_hardware} -> {self.netbox_device_type}"


class ModuleTypeMapping(NetBoxModel):
    """Map LibreNMS inventory model names to NetBox ModuleType objects."""

    librenms_model = models.CharField(
        max_length=255,
        unique=True,
        help_text="Model name from LibreNMS inventory (entPhysicalModelName)",
    )
    netbox_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        related_name="librenms_mappings",
        help_text="The NetBox ModuleType this model name maps to",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:moduletypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for ModuleTypeMapping."""

        ordering = ["librenms_model"]

    def __str__(self):
        return f"{self.librenms_model} -> {self.netbox_module_type}"


class ModuleBayMapping(NetBoxModel):
    """Map LibreNMS inventory names to NetBox module bay names.

    Used when LibreNMS inventory names don't match NetBox bay names exactly.
    For example: LibreNMS "Power Supply 1" → NetBox "PS1".
    Mappings are global (not scoped to device type or manufacturer).
    """

    librenms_name = models.CharField(
        max_length=255,
        help_text="Name from LibreNMS inventory (entPhysicalName), e.g. 'Power Supply 1'",
    )
    librenms_class = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional entPhysicalClass filter (e.g. 'powerSupply', 'fan', 'module')",
    )
    netbox_bay_name = models.CharField(
        max_length=255,
        help_text="NetBox module bay name to match, e.g. 'PS1'",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:modulebaymapping_detail", args=[self.pk])

    class Meta:
        """Meta options for ModuleBayMapping."""

        unique_together = ["librenms_name", "librenms_class"]
        ordering = ["librenms_name"]

    def __str__(self):
        cls = f" [{self.librenms_class}]" if self.librenms_class else ""
        return f"{self.librenms_name}{cls} -> {self.netbox_bay_name}"


class InterfaceNameRule(NetBoxModel):
    """Post-install interface rename rule for module types.

    Handles cases where NetBox's position-based naming can't produce
    the correct interface name, such as converter offset (CVR-X2-SFP)
    or breakout transceivers (QSFP+ 4x10G).

    The name_template uses Python str.format() syntax with these variables:
      {slot}               - Slot number from parent module bay position
      {bay_position}       - Position of the bay this module is installed into
      {parent_bay_position} - Position of the parent module's bay
      {sfp_slot}           - Sub-bay index (1-based) within the parent module
      {base}               - Base interface name from NetBox position resolution
      {channel}            - Channel number (iterated for breakout)
    """

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        related_name="interface_name_rules",
        help_text="The module type whose installation triggers this rename rule",
    )
    parent_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="child_interface_name_rules",
        help_text="If set, rule only applies when installed inside this parent module type",
    )
    name_template = models.CharField(
        max_length=255,
        help_text="Interface name template expression, e.g. 'GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}'",
    )
    channel_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of breakout channels (0 = no breakout). Creates this many interfaces per template.",
    )
    channel_start = models.PositiveSmallIntegerField(
        default=0,
        help_text="Starting channel number for breakout interfaces (e.g., 0 for Juniper, 1 for Cisco)",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this rule",
    )

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:interfacenamerule_detail", args=[self.pk])

    class Meta:
        """Meta options for InterfaceNameRule."""

        unique_together = ["module_type", "parent_module_type"]
        ordering = ["module_type__model"]

    def __str__(self):
        parent = f" in {self.parent_module_type.model}" if self.parent_module_type else ""
        return f"{self.module_type.model}{parent} → {self.name_template}"


class NormalizationRule(NetBoxModel):
    """Regex-based string normalization applied before matching lookups.

    Generic building block: a single rule engine handles normalization
    for module types, device types, module bays, and future scopes.
    Rules are applied in priority order; each transforms the string
    for the next rule in the chain.

    Example – strip Nokia revision suffixes:
        scope:       module_type
        match_pattern:  ^(3HE\\w{5}[A-Z]{2})[A-Z]{2}\\d{2}$
        replacement:    \\1
        Result: 3HE16474AARA01 → 3HE16474AA
    """

    SCOPE_MODULE_TYPE = "module_type"
    SCOPE_DEVICE_TYPE = "device_type"
    SCOPE_MODULE_BAY = "module_bay"

    SCOPE_CHOICES = [
        (SCOPE_MODULE_TYPE, "Module Type"),
        (SCOPE_DEVICE_TYPE, "Device Type"),
        (SCOPE_MODULE_BAY, "Module Bay"),
    ]

    scope = models.CharField(
        max_length=50,
        choices=SCOPE_CHOICES,
        help_text="Which matching lookup this rule applies to",
    )
    match_pattern = models.CharField(
        max_length=500,
        help_text="Regex pattern to match against input string (Python re syntax)",
    )
    replacement = models.CharField(
        max_length=500,
        help_text="Replacement string (supports regex back-references \\1, \\2, …)",
    )
    priority = models.PositiveIntegerField(
        default=100,
        help_text="Lower values run first. Rules chain: each transforms the output of the previous.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this rule",
    )

    def clean(self):
        """Validate that match_pattern compiles as a regex."""
        super().clean()
        try:
            re.compile(self.match_pattern)
        except re.error as e:
            raise ValidationError({"match_pattern": f"Invalid regex: {e}"})

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:normalizationrule_detail", args=[self.pk])

    class Meta:
        """Meta options for NormalizationRule."""

        ordering = ["scope", "priority", "pk"]

    def __str__(self):
        return f"[{self.get_scope_display()}] {self.match_pattern} → {self.replacement}"
