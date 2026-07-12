#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-"$ROOT/assets/demo.gif"}
TMP=${TMPDIR:-/tmp}/kasm-demo-frames
mkdir -p "$TMP"
for item in "000:100" "001:1900" "002:2800" "003:3600" "004:4300" "005:5100"; do
  name=${item%%:*}; wait=${item#*:}
  playwright screenshot --viewport-size="1200,720" --wait-for-timeout="$wait" \
    "file://$ROOT/docs/demo.html" "$TMP/$name.png" >/dev/null
done
ffmpeg -y -loglevel error -framerate 1/1.15 -i "$TMP/%03d.png" \
  -vf "fps=12,scale=1200:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer" \
  -loop 0 "$OUT"
printf 'Rendered %s\n' "$OUT"
