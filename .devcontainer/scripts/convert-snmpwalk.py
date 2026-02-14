#!/usr/bin/env python3
"""Convert net-snmp snmpwalk output to snmpsim .snmprec format.

Usage: python convert-snmpwalk.py <input.snmpwalk> <output.snmprec>

Handles the following snmpwalk value types:
  STRING, INTEGER, OID, Counter32, Gauge32, Timeticks, Hex-STRING, IpAddress
  Empty quoted strings ("") are treated as empty OCTET STRINGs.
"""

import re
import sys

# snmprec ASN.1 type tags
TYPE_MAP = {
    "INTEGER": "2",
    "STRING": "4",
    "OID": "6",
    "IpAddress": "64",
    "Counter32": "65",
    "Gauge32": "66",
    "Timeticks": "67",
}


def convert_oid(oid: str) -> str:
    """Replace 'iso' prefix with '1' in OID strings."""
    return re.sub(r"^iso\b", "1", oid)


def parse_line(line: str) -> str | None:
    """Parse a single snmpwalk output line into snmprec format."""
    line = line.rstrip()
    if not line or line.startswith("#"):
        return None

    # Match: OID = TYPE: VALUE  or  OID = ""
    m = re.match(r"^(\S+)\s*=\s*(.*)$", line)
    if not m:
        return None

    oid_raw, rest = m.group(1), m.group(2).strip()
    oid = convert_oid(oid_raw)

    # Empty string: OID = ""
    if rest == '""':
        return f"{oid}|4|"

    # Hex-STRING: 00 22 56 B9 35 C0
    hm = re.match(r"^Hex-STRING:\s*(.*)$", rest)
    if hm:
        hex_val = hm.group(1).strip().replace(" ", "").lower()
        return f"{oid}|4x|{hex_val}"

    # Timeticks: (34798156) 4 days, 0:39:41.56
    tm = re.match(r"^Timeticks:\s*\((\d+)\)", rest)
    if tm:
        return f"{oid}|67|{tm.group(1)}"

    # OID: iso.3.6.1.4.1.9.12.3.1.3.703
    om = re.match(r"^OID:\s*(.+)$", rest)
    if om:
        return f"{oid}|6|{convert_oid(om.group(1).strip())}"

    # STRING: "some value" (may lack closing quote for multi-line values)
    sm = re.match(r'^STRING:\s*"(.*?)"?$', rest)
    if sm:
        return f"{oid}|4|{sm.group(1)}"

    # IpAddress: 10.0.0.1
    im = re.match(r"^IpAddress:\s*(.+)$", rest)
    if im:
        return f"{oid}|64|{im.group(1).strip()}"

    # INTEGER: 42 or Counter32: 123 or Gauge32: 456
    for snmp_type, tag in TYPE_MAP.items():
        nm = re.match(rf"^{re.escape(snmp_type)}:\s*(.+)$", rest)
        if nm:
            return f"{oid}|{tag}|{nm.group(1).strip()}"

    # Fallback: treat unknown as octet string
    return f"{oid}|4|{rest}"


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.snmpwalk> <output.snmprec>")
        sys.exit(1)

    input_file, output_file = sys.argv[1], sys.argv[2]
    converted = 0

    with open(input_file) as fin, open(output_file, "w") as fout:
        for line in fin:
            result = parse_line(line)
            if result:
                fout.write(result + "\n")
                converted += 1

    print(f"Converted {converted} lines → {output_file}")


if __name__ == "__main__":
    main()
