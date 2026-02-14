#!/bin/bash
# Generate a 2nd simulated SNMP device from the base public.snmprec.
# Changes sysName and all serial numbers to create a distinct device.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/../snmpsim-data"
BASE_FILE="$DATA_DIR/public.snmprec"
OUTPUT_FILE="$DATA_DIR/sw2.snmprec"

if [ ! -f "$BASE_FILE" ]; then
    echo "❌ Base snmprec file not found: $BASE_FILE"
    exit 1
fi

echo "🔧 Generating 2nd simulated device data..."

# Start with a copy of the base file
cp "$BASE_FILE" "$OUTPUT_FILE"

# Change sysName: sw1.libre → sw2.libre
sed -i 's/^1\.3\.6\.1\.2\.1\.1\.5\.0|4|sw1\.libre$/1.3.6.1.2.1.1.5.0|4|sw2.libre/' "$OUTPUT_FILE"

# Replace all SIM1xxx serial numbers with SIM2xxx equivalents
sed -i 's/SIM1001AAAA/SIM2001AAAB/g' "$OUTPUT_FILE"
sed -i 's/SIM1002BBBB/SIM2002BBBC/g' "$OUTPUT_FILE"
sed -i 's/SIM1003CCCC/SIM2003CCCD/g' "$OUTPUT_FILE"
sed -i 's/SIM1004DDDD/SIM2004DDDE/g' "$OUTPUT_FILE"
sed -i 's/SIM1005EEEE/SIM2005EEEF/g' "$OUTPUT_FILE"
sed -i 's/SIM1006FFFF/SIM2006FFFG/g' "$OUTPUT_FILE"
sed -i 's/SIM1007GGGG/SIM2007GGGH/g' "$OUTPUT_FILE"
sed -i 's/SIM1008HHHH/SIM2008HHHJ/g' "$OUTPUT_FILE"
sed -i 's/SIM1009JJJJ/SIM2009JJJK/g' "$OUTPUT_FILE"
sed -i 's/SIM1010KKKK/SIM2010KKKL/g' "$OUTPUT_FILE"
sed -i 's/SIM1011LLLL/SIM2011LLLM/g' "$OUTPUT_FILE"
sed -i 's/SIM1012MMMM/SIM2012MMMN/g' "$OUTPUT_FILE"
sed -i 's/SIM1013NNNN/SIM2013NNNP/g' "$OUTPUT_FILE"
sed -i 's/SIM1014PPPP/SIM2014PPPQ/g' "$OUTPUT_FILE"
sed -i 's/SIM1015QQQQ/SIM2015QQQR/g' "$OUTPUT_FILE"
sed -i 's/SIM1016RRRR/SIM2016RRRS/g' "$OUTPUT_FILE"
sed -i 's/SIM1017SSSS/SIM2017SSST/g' "$OUTPUT_FILE"
sed -i 's/SIM1018TTTT/SIM2018TTTU/g' "$OUTPUT_FILE"
sed -i 's/SIM1019UUUU/SIM2019UUUV/g' "$OUTPUT_FILE"
sed -i 's/SIM1020VVVV/SIM2020VVVW/g' "$OUTPUT_FILE"
sed -i 's/SIM1021WWWW/SIM2021WWWX/g' "$OUTPUT_FILE"
sed -i 's/SIM1022XXXX/SIM2022XXXY/g' "$OUTPUT_FILE"
sed -i 's/SIM1023YYYY/SIM2023YYYZ/g' "$OUTPUT_FILE"
sed -i 's/SIM1024ZZZZ/SIM2024ZZZ1/g' "$OUTPUT_FILE"
sed -i 's/SIM1025AB12/SIM2025AB13/g' "$OUTPUT_FILE"
sed -i 's/SIM00001026CD34EF/SIM00002026CD35EF/g' "$OUTPUT_FILE"

echo "✅ Generated $OUTPUT_FILE (sysName=sw2.libre, unique serials)"
