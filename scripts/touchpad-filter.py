#!/usr/bin/env python3
"""Hyprland touchpad filter.

Subscribes to the Hyprland event socket and, while the active workspace
contains only kitty windows, grabs the touchpad evdev device away from
libinput and synthesizes vertical wheel events from two-finger Y motion via
a uinput virtual device. Pointer motion, tap-to-click and single-finger
gestures are suppressed in that mode. Outside the condition, the device is
ungrabbed and libinput resumes normal handling.
"""

import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

from evdev import InputDevice, UInput, ecodes, list_devices


TOUCHPAD_NAME_FRAGMENT = "Touchpad"
TERMINAL_CLASS = "kitty"
HYPR_EVENT_PREFIXES = (
    b"openwindow", b"closewindow", b"movewindow",
    b"workspace", b"focusedmon", b"changefloatingmode",
)
SCROLL_THRESHOLD = 25      # touchpad Y units per wheel click; tune to taste
NATURAL_SCROLL = True


def find_touchpad():
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if TOUCHPAD_NAME_FRAGMENT.lower() in dev.name.lower():
            return dev
        dev.close()
    return None


def ws_is_kitty_only():
    try:
        ws = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"]))
        wsid = ws.get("id")
        if wsid is None:
            return False
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
        on_ws = [c for c in clients if c.get("workspace", {}).get("id") == wsid]
        if not on_ws:
            return False
        return all(c.get("class") == TERMINAL_CLASS for c in on_ws)
    except Exception:
        return False


class ScrollSynth:
    """Translate 2-finger touchpad Y motion into REL_WHEEL events."""

    def __init__(self):
        self.ui = UInput(
            {ecodes.EV_REL: [ecodes.REL_WHEEL]},
            name="hypr-touchpad-scroll",
        )
        self.slots = {}
        self.current_slot = 0
        self.accum = 0.0

    def _slot(self, idx):
        return self.slots.setdefault(idx, {"y": None, "prev_y": None, "active": False})

    def _active_count(self):
        return sum(1 for s in self.slots.values() if s["active"])

    def handle(self, e):
        if e.type == ecodes.EV_ABS:
            if e.code == ecodes.ABS_MT_SLOT:
                self.current_slot = e.value
                self._slot(self.current_slot)
            elif e.code == ecodes.ABS_MT_TRACKING_ID:
                s = self._slot(self.current_slot)
                if e.value == -1:
                    s["active"] = False
                else:
                    s["active"] = True
                s["y"] = None
                s["prev_y"] = None
                self.accum = 0.0  # finger-count change → reset
            elif e.code == ecodes.ABS_MT_POSITION_Y:
                self._slot(self.current_slot)["y"] = e.value
        elif e.type == ecodes.EV_SYN and e.code == ecodes.SYN_REPORT:
            if self._active_count() == 2:
                deltas = [
                    s["y"] - s["prev_y"]
                    for s in self.slots.values()
                    if s["active"] and s["y"] is not None and s["prev_y"] is not None
                ]
                if deltas:
                    avg = sum(deltas) / len(deltas)
                    sign = -1 if NATURAL_SCROLL else 1
                    self.accum += sign * avg
                    emitted = False
                    while self.accum >= SCROLL_THRESHOLD:
                        self.ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, 1)
                        self.accum -= SCROLL_THRESHOLD
                        emitted = True
                    while self.accum <= -SCROLL_THRESHOLD:
                        self.ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, -1)
                        self.accum += SCROLL_THRESHOLD
                        emitted = True
                    if emitted:
                        self.ui.syn()
            for s in self.slots.values():
                if s["active"]:
                    s["prev_y"] = s["y"]

    def close(self):
        self.ui.close()


class FilterDaemon:
    def __init__(self, dev):
        self.dev = dev
        self.synth = None
        self.grabbed = False

    def set_blocked(self, want):
        if want and not self.grabbed:
            try:
                self.dev.grab()
            except OSError as ex:
                print(f"grab failed: {ex}", file=sys.stderr)
                return
            self.synth = ScrollSynth()
            self.grabbed = True
        elif not want and self.grabbed:
            try:
                self.dev.ungrab()
            except OSError:
                pass
            if self.synth is not None:
                self.synth.close()
                self.synth = None
            self.grabbed = False

    def evaluate(self):
        self.set_blocked(ws_is_kitty_only())

    def run(self):
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        sock_path = runtime / "hypr" / sig / ".socket2.sock"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(sock_path))
        sock.setblocking(False)
        buf = b""

        self.evaluate()
        try:
            while True:
                rlist = [sock.fileno()]
                if self.grabbed:
                    rlist.append(self.dev.fileno())
                r, _, _ = select.select(rlist, [], [], 1.0)

                if sock.fileno() in r:
                    try:
                        data = sock.recv(4096)
                    except BlockingIOError:
                        data = b""
                    if data == b"":
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if any(line.startswith(p) for p in HYPR_EVENT_PREFIXES):
                            self.evaluate()

                if self.grabbed and self.dev.fileno() in r:
                    try:
                        for e in self.dev.read():
                            if self.synth is not None:
                                self.synth.handle(e)
                    except BlockingIOError:
                        pass
        finally:
            self.set_blocked(False)


def main():
    dev = find_touchpad()
    if dev is None:
        print("touchpad device not found", file=sys.stderr)
        sys.exit(1)
    FilterDaemon(dev).run()


if __name__ == "__main__":
    main()
