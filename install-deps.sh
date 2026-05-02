#!/usr/bin/env bash
#
# Install all dependencies required by this hypr-dotfiles repo.
# Targets Arch Linux (pacman). AUR packages are installed via an
# AUR helper if available (yay or paru), otherwise they are
# skipped with a warning.

set -euo pipefail

if ! command -v pacman >/dev/null 2>&1; then
    echo "error: pacman not found — this script targets Arch Linux" >&2
    exit 1
fi

# Official repo packages grouped by purpose
pacman_pkgs=(
    # Compositor & core session
    hyprland
    hyprlock
    xdg-desktop-portal-hyprland

    # Bar, launcher, wallpaper, lock
    waybar
    wofi
    swaybg

    # Terminal & shell multiplexing
    kitty
    tmux

    # Applications launched from keybinds
    dolphin
    firefox
    speedcrunch
    code

    # Screenshot pipeline (grim/slurp bindings)
    grim
    slurp
    wl-clipboard
    jq

    # Audio / brightness / media keys
    wireplumber
    brightnessctl
    playerctl

    # Fonts / cursor basics
    ttf-dejavu
    noto-fonts
)

echo ":: Installing pacman packages"
sudo pacman -S --needed --noconfirm "${pacman_pkgs[@]}"

echo ":: Done"
