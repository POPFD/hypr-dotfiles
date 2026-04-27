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

evaluate() {
    local ws
    ws=$(hyprctl activeworkspace -j | jq -r '.id')

    local non_terminal
    non_terminal=$(hyprctl clients -j | jq -r --argjson ws "$ws" --arg t "$TERMINAL_CLASS" '
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

evaluate

exec socat -U - "UNIX-CONNECT:$SOCK" | while read -r line; do
    case "$line" in
        openwindow*|closewindow*|movewindow*|workspace*|focusedmon*|changefloatingmode*)
            evaluate
            ;;
    esac
done
