Just my repository for hyprland dotfiles.

This is barebones and what I use as my daily driver for some devices.

Clone or move all the files into `~/.conf/hypr/`

## Waybar service

Waybar runs via the systemd user service that ships with the `waybar` package
(`Restart=on-failure`), so it auto-restarts if it crashes. That unit hangs off
`graphical-session.target`, which `autostart.conf` brings up through a small
`hyprland-session.target` (in `systemd/`). On a fresh setup, do this once:

```
ln -sf ~/.config/hypr/systemd/hyprland-session.target ~/.config/systemd/user/hyprland-session.target
systemctl --user daemon-reload
systemctl --user enable waybar.service
```

