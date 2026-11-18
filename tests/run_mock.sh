#!/usr/bin/env bash
# End-to-end test against two mock sites: a Joomla 3 "live" site and a Joomla 6 "staging" site.
set -euo pipefail
cd "$(dirname "$0")"
export no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost
python3 mock_joomla.py 8801 old & P1=$!
python3 mock_joomla.py 8802 new & P2=$!
trap 'kill $P1 $P2' EXIT
sleep 1
rm -rf audit-live audit-staging diff-staging
python3 ../urlguard.py crawl http://127.0.0.1:8801 --out audit-live --delay 0 --check-images
python3 ../urlguard.py report audit-live
python3 ../urlguard.py crawl http://127.0.0.1:8802 --out audit-staging --delay 0
python3 ../urlguard.py diff audit-live http://127.0.0.1:8802 --out diff-staging --delay 0 \
  --check-images --new-inventory audit-staging || true
grep -q "T3 framework in use" audit-live/report.md
grep -q "/listings/18-old-creek-vic" diff-staging/redirects.htaccess
grep -q "TEMP REDIRECT" diff-staging/diff.md
echo "OK: all assertions passed"
