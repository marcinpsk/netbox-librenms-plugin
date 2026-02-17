from dcim.choices import InterfaceTypeChoices
from dcim.models import DeviceType
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
