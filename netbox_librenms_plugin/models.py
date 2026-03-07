import re

from dcim.choices import InterfaceTypeChoices
from dcim.models import DeviceType, Manufacturer, ModuleType
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
    When is_regex is True, librenms_name is treated as a regex pattern and
    netbox_bay_name can use backreferences (\\1, \\2, etc.).
    Mappings are global (not scoped to device type or manufacturer).
    """

    librenms_name = models.CharField(
        max_length=255,
        help_text="Name from LibreNMS inventory (entPhysicalName). "
        "When 'Use Regex' is enabled, this is a Python regex pattern.",
    )
    librenms_class = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional entPhysicalClass filter (e.g. 'powerSupply', 'fan', 'module')",
    )
    netbox_bay_name = models.CharField(
        max_length=255,
        help_text="NetBox module bay name to match. With regex, supports backreferences (\\1, \\2, etc.).",
    )
    is_regex = models.BooleanField(
        default=False,
        help_text="Treat LibreNMS Name as a regex pattern with backreferences in NetBox Bay Name",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def clean(self):
        """Validate that regex patterns compile when is_regex is True."""
        super().clean()
        if self.is_regex:
            try:
                pattern = re.compile(self.librenms_name)
            except re.error as e:
                raise ValidationError({"librenms_name": f"Invalid regex: {e}"})
            try:
                pattern.sub(self.netbox_bay_name, "")
            except (re.error, IndexError) as e:
                raise ValidationError({"netbox_bay_name": f"Invalid replacement: {e}"})

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
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="normalization_rules",
        help_text="Optional: only apply this rule to items from this manufacturer. "
        "Leave blank for vendor-agnostic rules.",
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
        """Validate that match_pattern compiles as a regex and replacement is a valid template."""
        super().clean()
        try:
            compiled = re.compile(self.match_pattern)
        except re.error as e:
            raise ValidationError({"match_pattern": f"Invalid regex: {e}"})
        # Validate the replacement template by running a dummy substitution
        try:
            compiled.sub(self.replacement, "")
        except re.error as e:
            raise ValidationError({"replacement": f"Invalid replacement template: {e}"})

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:normalizationrule_detail", args=[self.pk])

    class Meta:
        """Meta options for NormalizationRule."""

        ordering = ["scope", "priority", "pk"]

    def __str__(self):
        return f"[{self.get_scope_display()}] {self.match_pattern} → {self.replacement}"


class InventoryIgnoreRule(NetBoxModel):
    """Rule-based filter for ENTITY-MIB inventory items that should not appear as modules.

    Some vendors (e.g. Cisco IOS-XR) report phantom EEPROM/IDPROM child entities in
    ENTITY-MIB with the same model name and serial number as the real parent hardware.
    These are not installable modules and must be suppressed.

    This model replaces the hard-coded ``_is_idprom_entry()`` helper with an
    admin-configurable rule table so other vendor-specific patterns can be added
    without code changes.

    Example — Cisco IOS-XR IDPROM entries:
        match_type:                ends_with
        pattern:                   IDPROM
        require_serial_match_parent: True
        Skips ``Optics0/0/0/0-IDPROM``, ``0/FT0-FT IDPROM``, etc. when the serial
        number matches the parent entity.
    """

    MATCH_ENDS_WITH = "ends_with"
    MATCH_STARTS_WITH = "starts_with"
    MATCH_CONTAINS = "contains"
    MATCH_REGEX = "regex"

    MATCH_TYPE_CHOICES = [
        (MATCH_ENDS_WITH, "Ends with"),
        (MATCH_STARTS_WITH, "Starts with"),
        (MATCH_CONTAINS, "Contains"),
        (MATCH_REGEX, "Regex"),
    ]

    name = models.CharField(
        max_length=100,
        help_text="Short descriptive label for this rule",
    )
    match_type = models.CharField(
        max_length=20,
        choices=MATCH_TYPE_CHOICES,
        default=MATCH_ENDS_WITH,
        help_text="How to match the entPhysicalName value",
    )
    pattern = models.CharField(
        max_length=200,
        help_text="Pattern to match against entPhysicalName. "
        "Case-insensitive for ends_with / starts_with / contains; "
        "Python re syntax for regex.",
    )
    require_serial_match_parent = models.BooleanField(
        default=True,
        help_text="Only skip this entity if its serial number matches its direct parent's "
        "serial number. Recommended: prevents false positives when the pattern "
        "could match legitimate module names.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to temporarily disable this rule without deleting it",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional notes about this rule (vendor, firmware version, etc.)",
    )

    def clean(self):
        """Validate regex pattern compiles when match_type is 'regex'."""
        super().clean()
        if self.match_type == self.MATCH_REGEX:
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValidationError({"pattern": f"Invalid regex: {e}"})

    def matches_name(self, name: str) -> bool:
        """Return True if *name* matches this rule's pattern/match_type."""
        if not name:
            return False
        if self.match_type == self.MATCH_REGEX:
            return bool(re.search(self.pattern, name))
        name_up = name.upper()
        pat = self.pattern.upper()
        if self.match_type == self.MATCH_ENDS_WITH:
            return name_up.endswith(pat)
        if self.match_type == self.MATCH_STARTS_WITH:
            return name_up.startswith(pat)
        if self.match_type == self.MATCH_CONTAINS:
            return pat in name_up
        return False

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:inventoryignorerule_detail", args=[self.pk])

    class Meta:
        """Meta options for InventoryIgnoreRule."""

        ordering = ["name", "pk"]

    def __str__(self):
        serial_note = " [serial match]" if self.require_serial_match_parent else ""
        return f"{self.name}: {self.get_match_type_display()} '{self.pattern}'{serial_note}"
