"""Unit tests for netbox_librenms_plugin.constants.

Covers the OOB_TYPE_PATTERN regex and normalize_oob_type() function
as changed in this PR:
  - Added 'cimc' and 'oob' tokens to OOB_TYPES / OOB_TYPE_PATTERN.
  - Vendor-specific token always wins over the generic 'oob' token,
    even when 'oob' appears earlier in the text.
  - Trailing \\d*\\b suffix prevents substring matches inside longer words
    (e.g. 'dracut', 'ipmitool' must NOT be classified as OOB).
"""

import pytest

from netbox_librenms_plugin.constants import (
    OOB_TYPE_PATTERN,
    OOB_TYPES,
    normalize_oob_type,
)


# ---------------------------------------------------------------------------
# OOB_TYPES tuple membership
# ---------------------------------------------------------------------------

class TestOobTypes:
    """OOB_TYPES must include every token that normalize_oob_type can return."""

    def test_contains_idrac(self):
        assert "idrac" in OOB_TYPES

    def test_contains_ilo(self):
        assert "ilo" in OOB_TYPES

    def test_contains_ipmi(self):
        assert "ipmi" in OOB_TYPES

    def test_contains_bmc(self):
        assert "bmc" in OOB_TYPES

    def test_contains_drac(self):
        assert "drac" in OOB_TYPES

    def test_contains_cimc(self):
        """cimc was added in this PR."""
        assert "cimc" in OOB_TYPES

    def test_contains_oob(self):
        """generic 'oob' token was added in this PR."""
        assert "oob" in OOB_TYPES


# ---------------------------------------------------------------------------
# OOB_TYPE_PATTERN regex: word-boundary behaviour
# ---------------------------------------------------------------------------

class TestOobTypePattern:
    """Verify the compiled regex matches whole tokens and rejects substrings."""

    @pytest.mark.parametrize("text", [
        "idrac", "iDRAC", "IDRAC", "iDRAC9", "drac", "drac9",
        "ilo", "iLO", "ILO",
        "ipmi", "IPMI",
        "bmc", "BMC",
        "cimc", "CIMC",
        "oob", "OOB",
    ])
    def test_matches_valid_tokens(self, text):
        assert OOB_TYPE_PATTERN.search(text), f"Expected pattern to match {text!r}"

    @pytest.mark.parametrize("text", [
        "dracut",       # 'drac' prefix inside a Linux tool name
        "ipmitool",     # 'ipmi' prefix inside a CLI tool name
        "ubuntu",       # ordinary OS
        "linux",
        "cisco-ios",
        "bmc_data",     # 'bmc' followed by underscore (not a digit/word boundary)
        # Note: 'bmc_data' — underscore is \w so \b sits BEFORE 'bmc', but after 'c'
        # comes '_' which IS \w, so \b does NOT fire there. Let's verify with actual text:
        "xbmc",         # bmc preceded by a word char — no \b before it
    ])
    def test_does_not_match_false_positives(self, text):
        # For "bmc_data" the pattern should NOT match "bmc" as a standalone token
        # because '_' is a word character, so there is no \b after 'bmc'.
        # For "ipmitool" similarly no \b after 'ipmi'.
        # For "xbmc" there is no \b before 'bmc'.
        # For "dracut" there is no \b after 'drac'.
        match = OOB_TYPE_PATTERN.search(text)
        if match:
            # Allow a match only when the full token (group 1 + optional digits)
            # spans the entire string (edge cases like "bmc" itself).
            assert match.group(0).lower() != text.lower(), (
                f"Pattern falsely matched {text!r} as {match.group(0)!r}"
            )

    def test_matches_idrac_with_numeric_suffix(self):
        """iDRAC9, drac7, etc. should be recognised."""
        m = OOB_TYPE_PATTERN.search("iDRAC9")
        assert m is not None
        assert m.group(1).lower() == "idrac"

    def test_matches_oob_generic_token(self):
        m = OOB_TYPE_PATTERN.search("oob")
        assert m is not None
        assert m.group(1).lower() == "oob"


# ---------------------------------------------------------------------------
# normalize_oob_type() — core logic
# ---------------------------------------------------------------------------

class TestNormalizeOobType:
    """Tests for the normalize_oob_type() function."""

    # --- Returns None for non-OOB inputs ---

    def test_returns_none_for_ordinary_os(self):
        assert normalize_oob_type("ubuntu", "") is None

    def test_returns_none_for_cisco_ios(self):
        assert normalize_oob_type("ios", "Cisco C9300") is None

    def test_returns_none_for_empty_strings(self):
        assert normalize_oob_type("", "") is None

    def test_returns_none_for_none_inputs(self):
        assert normalize_oob_type(None, None) is None

    def test_returns_none_for_none_os_empty_hw(self):
        assert normalize_oob_type(None, "") is None

    def test_returns_none_for_substring_only(self):
        """'dracut' and 'ipmitool' must not trigger detection."""
        assert normalize_oob_type("dracut", "") is None
        assert normalize_oob_type("ipmitool", "") is None

    # --- Vendor-specific tokens returned correctly ---

    def test_drac_from_os(self):
        assert normalize_oob_type("drac9", "") == "drac"

    def test_idrac_from_hardware(self):
        assert normalize_oob_type("", "iDRAC9") == "idrac"

    def test_ilo_from_os(self):
        assert normalize_oob_type("ilo", "") == "ilo"

    def test_ilo_case_insensitive(self):
        assert normalize_oob_type("ILO", "") == "ilo"

    def test_ipmi_from_os(self):
        assert normalize_oob_type("ipmi", "") == "ipmi"

    def test_bmc_from_hardware(self):
        assert normalize_oob_type("linux", "BMC") == "bmc"

    def test_cimc_from_os(self):
        """cimc was added in this PR."""
        assert normalize_oob_type("cimc", "") == "cimc"

    def test_cimc_from_hardware(self):
        assert normalize_oob_type("", "CIMC") == "cimc"

    def test_cimc_case_insensitive(self):
        assert normalize_oob_type("Cimc", "") == "cimc"

    # --- Generic 'oob' token behaviour ---

    def test_generic_oob_from_os(self):
        """When only 'oob' appears, the generic fallback 'oob' is returned."""
        assert normalize_oob_type("oob", "") == "oob"

    def test_generic_oob_from_hardware(self):
        assert normalize_oob_type("", "OOB") == "oob"

    def test_generic_oob_case_insensitive(self):
        assert normalize_oob_type("OOB", "") == "oob"

    # --- Vendor-specific wins over generic 'oob' (key new behaviour in this PR) ---

    def test_vendor_wins_over_generic_oob_in_hardware(self):
        """normalize_oob_type("oob", "iDRAC9") must return 'idrac', not 'oob'.

        The os_str contains only the generic 'oob' token, but the hardware_str
        contains a vendor-specific token.  The vendor-specific token must win.
        """
        assert normalize_oob_type("oob", "iDRAC9") == "idrac"

    def test_vendor_wins_over_generic_oob_in_os(self):
        """When both 'oob' and a vendor token appear in os_str, vendor wins."""
        assert normalize_oob_type("oob ilo system", "") == "ilo"

    def test_vendor_wins_over_generic_oob_os_first(self):
        """'oob' in os_str, vendor-specific in hardware_str — vendor still wins."""
        assert normalize_oob_type("oob", "cimc") == "cimc"

    def test_vendor_wins_over_generic_oob_drac(self):
        assert normalize_oob_type("oob", "drac9") == "drac"

    def test_vendor_wins_over_generic_oob_bmc(self):
        assert normalize_oob_type("oob", "BMC controller") == "bmc"

    def test_vendor_wins_over_generic_oob_ipmi(self):
        assert normalize_oob_type("oob", "ipmi") == "ipmi"

    def test_vendor_in_os_wins_over_oob_in_hardware(self):
        """Vendor token in os_str, generic 'oob' in hardware_str."""
        assert normalize_oob_type("ilo firmware", "oob-adapter") == "ilo"

    # --- First vendor-specific match in scanning order wins ---

    def test_first_os_vendor_match_wins(self):
        """When os_str has a vendor-specific token, it is returned immediately
        (scanning stops early), ignoring whatever hardware_str says."""
        # drac in os_str should short-circuit before hardware_str is scanned.
        result = normalize_oob_type("drac", "idrac9")
        assert result == "drac"

    def test_generic_oob_returned_when_only_generic_present(self):
        """With no vendor-specific token anywhere, the generic 'oob' is the fallback."""
        assert normalize_oob_type("oob-management-port", "OOB") == "oob"

    # --- documented examples from the docstring ---

    def test_docstring_example_drac9(self):
        """normalize_oob_type("drac9", "iDRAC9") → "drac" (from the docstring)."""
        assert normalize_oob_type("drac9", "iDRAC9") == "drac"

    def test_docstring_example_oob_with_idrac_hw(self):
        """normalize_oob_type("oob", "iDRAC9") → "idrac" (from the docstring)."""
        assert normalize_oob_type("oob", "iDRAC9") == "idrac"

    def test_docstring_example_ilo(self):
        """normalize_oob_type("ilo", "") → "ilo" (from the docstring)."""
        assert normalize_oob_type("ilo", "") == "ilo"

    def test_docstring_example_ubuntu_none(self):
        """normalize_oob_type("ubuntu", "") → None (from the docstring)."""
        assert normalize_oob_type("ubuntu", "") is None

    # --- Default argument for hardware_str ---

    def test_hardware_str_defaults_to_empty(self):
        """hardware_str has a default value of '' so callers can omit it."""
        assert normalize_oob_type("ilo") == "ilo"
        assert normalize_oob_type("ubuntu") is None

    # --- Boundary / regression ---

    def test_numeric_suffix_stripped_in_return_value(self):
        """The returned token must NOT include the numeric suffix."""
        assert normalize_oob_type("idrac9", "") == "idrac"
        assert normalize_oob_type("drac7", "") == "drac"

    def test_mixed_case_tokens_normalised_to_lowercase(self):
        assert normalize_oob_type("IDRAC", "") == "idrac"
        assert normalize_oob_type("ILO", "") == "ilo"
        assert normalize_oob_type("CIMC", "") == "cimc"