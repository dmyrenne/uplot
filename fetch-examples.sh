#!/bin/sh
# Lädt die Beispiel-SVGs aus github.com/brianlow/plotter nach examples/
set -e
cd "$(dirname "$0")"
mkdir -p examples
base=https://raw.githubusercontent.com/brianlow/plotter/main
for f in waves/waves.svg flower/flower.svg city/city.svg calibration/calibration.svg calibration/pen-width.svg; do
  curl -fsSL "$base/$f" -o "examples/$(basename "$f")"
done
echo "Beispiele liegen in examples/"
