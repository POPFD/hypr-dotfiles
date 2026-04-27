#!/usr/bin/env bash
# Disables the touchpad when the active workspace contains only kitty windows.
# Re-enables as soon as any non-kitty window is present (or workspace is empty).

set -u

DEVICE="syna2ba6:00-06cb:cfd8-touchpad"
TERMINAL_CLASS="kitty"

SOCK="$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock"

state=""

set_touchpad() {
    local want="$1"
    [ "$want" = "$state" ] && return
    hyprctl keyword "device[$DEVICE]:enabled" "$want" >/dev/null
    state="$want"
}

# Never leave the touchpad stuck disabled if this script exits for any reason.
trap 'hyprctl keyword "device[$DEVICE]:enabled" true >/dev/null 2>&1' EXIT

evaluate() {
    local ws
    ws=$(hyprctl activeworkspace -j 2>/dev/null | jq -r '.id // empty')
    [ -z "$ws" ] && { set_touchpad true; return; }

    local non_terminal
    non_terminal=$(hyprctl clients -j 2>/dev/null | jq -r --argjson ws "$ws" --arg t "$TERMINAL_CLASS" '
        [.[] | select(.workspace.id == $ws) | .class] as $cs
        | if ($cs | length) == 0 then "yes"
          elif any($cs[]; . != $t) then "yes"
          else "no"
          end')

    if [ "$non_terminal" = "no" ]; then
        set_touchpad false
    else
        set_touchpad true
    fi
}

# Hyprland may launch this exec-once entry before the IPC socket is ready.
for _ in $(seq 1 50); do
    [ -S "$SOCK" ] && break
    sleep 0.2
done

# Keep listening; if socat ever exits, restart it rather than leaving the
# touchpad stuck in whatever state was last applied.
while :; do
    evaluate
    socat -U - "UNIX-CONNECT:$SOCK" 2>/dev/null | while read -r line; do
        case "$line" in
            openwindow*|closewindow*|movewindow*|workspace*|focusedmon*|changefloatingmode*)
                evaluate
                ;;
        esac
    done
    sleep 1
done
