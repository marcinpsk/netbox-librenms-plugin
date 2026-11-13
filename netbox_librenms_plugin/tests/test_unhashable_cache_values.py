"""A corrupt stored or request value must fail closed, never raise TypeError.

``x in {"a", "b"}`` raises ``TypeError: unhashable type: 'list'`` rather than returning False,
so a guard written to reject a bad value raises instead. A stored user preference and a JSON
body carry any type, so every one of these reads has to narrow before the membership test.
See ``constants.is_supported_interface_name_field``.
"""

import pytest


UNHASHABLE = ["ifName"]


class TestSupportedInterfaceNameFieldPredicate:
    """The one predicate every interface-name check routes through."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("ifName", True),
            ("ifDescr", True),
            ("ifAlias", False),
            (None, False),
            (5, False),
            (True, False),
            (["ifName"], False),
            ({"ifName": 1}, False),
            ({"ifName"}, False),
        ],
    )
    def test_returns_a_bool_for_every_input_shape(self, value, expected):
        from netbox_librenms_plugin.constants import is_supported_interface_name_field

        assert is_supported_interface_name_field(value) is expected

    def test_the_raw_membership_test_this_replaces_would_raise(self):
        """Positive control: prove the predicate is load-bearing, not decoration."""
        from netbox_librenms_plugin.constants import INTERFACE_NAME_FIELDS

        with pytest.raises(TypeError, match="unhashable"):
            UNHASHABLE in INTERFACE_NAME_FIELDS  # noqa: B015
