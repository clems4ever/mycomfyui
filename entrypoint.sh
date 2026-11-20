#!/bin/sh
set -e

# ComfyUI-Manager blocks node/model installs when --listen isn't loopback,
# which it never is in a container. personal_cloud mode allows them.
# Only seeded on first start so later edits to config.ini are kept.
CONFIG=/opt/ComfyUI/user/__manager/config.ini
if [ ! -f "$CONFIG" ]; then
    mkdir -p "$(dirname "$CONFIG")"
    printf '[default]\nnetwork_mode = personal_cloud\n' > "$CONFIG"
fi

exec "$@"
