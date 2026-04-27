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
OPENWINDOW_PREFIX = b"openwindow>>"
CLOSEWINDOW_PREFIX = b"closewindow>>"
MOVEWINDOW_PREFIX = b"movewindowv2>>"
WORKSPACE_PREFIX = b"workspacev2>>"
FOCUSEDMON_PREFIX = b"focusedmon>>"
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


class WorldState:
    """Local mirror of windows + active workspace, kept fresh from socket2.

    Avoids races where hyprctl clients/activeworkspace lag behind the events
    (openwindow before workspace assignment is queryable, closewindow before
    the row is dropped, etc.).
    """

    def __init__(self):
        # address -> (workspace_id, class)
        self.windows = {}
        self.active_ws = None
        self.seed_from_hyprctl()

    def seed_from_hyprctl(self):
        try:
            ws = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"]))
            self.active_ws = ws.get("id")
            clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
            fresh = {}
            for c in clients:
                addr = c.get("address")
                wsid = c.get("workspace", {}).get("id")
                cls = c.get("class") or ""
                if addr:
                    fresh[self._norm_addr(addr.encode())] = (wsid, cls)
            self.windows = fresh
        except Exception:
            pass

    @staticmethod
    def _norm_addr(addr_bytes):
        s = addr_bytes.decode(errors="ignore").strip()
        return s if s.startswith("0x") else "0x" + s

    def on_openwindow(self, payload):
        # ADDR,WORKSPACENAME,CLASS,TITLE
        parts = payload.split(b",", 3)
        if len(parts) < 3:
            return
        addr = self._norm_addr(parts[0])
        # workspace name -> id is non-trivial; use active_ws as best guess and
        # let movewindowv2 correct it. New windows almost always open on the
        # active workspace anyway.
        cls = parts[2].decode(errors="ignore")
        self.windows[addr] = (self.active_ws, cls)

    def on_closewindow(self, payload):
        addr = self._norm_addr(payload)
        self.windows.pop(addr, None)

    def on_movewindow(self, payload):
        # movewindowv2: ADDR,WSID,WSNAME
        parts = payload.split(b",", 2)
        if len(parts) < 2:
            return
        addr = self._norm_addr(parts[0])
        try:
            wsid = int(parts[1])
        except ValueError:
            return
        cls = self.windows.get(addr, (None, ""))[1]
        self.windows[addr] = (wsid, cls)

    def on_workspace(self, payload):
        # workspacev2: WSID,WSNAME
        parts = payload.split(b",", 1)
        try:
            self.active_ws = int(parts[0])
        except (ValueError, IndexError):
            pass

    def is_kitty_only(self):
        if self.active_ws is None:
            return False
        on_ws = [cls for (wsid, cls) in self.windows.values() if wsid == self.active_ws]
        if not on_ws:
            return False
        return all(cls == TERMINAL_CLASS for cls in on_ws)


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
        self.world = WorldState()

    def set_blocked(self, want):
        if want and not self.grabbed:
            try:
                self.dev.grab()
            except OSError as ex:
                print(f"grab failed: {ex}", file=sys.stderr, flush=True)
                return
            self.synth = ScrollSynth()
            self.grabbed = True
            print("touchpad: grabbed (kitty-only)", file=sys.stderr, flush=True)
        elif not want and self.grabbed:
            try:
                self.dev.ungrab()
            except OSError:
                pass
            if self.synth is not None:
                self.synth.close()
                self.synth = None
            self.grabbed = False
            print("touchpad: released", file=sys.stderr, flush=True)

    def evaluate(self):
        self.set_blocked(self.world.is_kitty_only())

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

                if not r:
                    # idle tick: re-seed from hyprctl as a safety net so any
                    # local-state desync (missed/garbled event, address-format
                    # drift, etc.) self-corrects within ~1s.
                    self.world.seed_from_hyprctl()
                    self.evaluate()
                    continue

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
                        changed = False
                        if line.startswith(OPENWINDOW_PREFIX):
                            self.world.on_openwindow(line[len(OPENWINDOW_PREFIX):])
                            changed = True
                        elif line.startswith(CLOSEWINDOW_PREFIX):
                            self.world.on_closewindow(line[len(CLOSEWINDOW_PREFIX):])
                            changed = True
                        elif line.startswith(MOVEWINDOW_PREFIX):
                            self.world.on_movewindow(line[len(MOVEWINDOW_PREFIX):])
                            changed = True
                        elif line.startswith(WORKSPACE_PREFIX):
                            self.world.on_workspace(line[len(WORKSPACE_PREFIX):])
                            changed = True
                        elif line.startswith(FOCUSEDMON_PREFIX):
                            # focused monitor change can switch active ws;
                            # cheapest correct fix is a hyprctl re-seed.
                            self.world.seed_from_hyprctl()
                            changed = True
                        if changed:
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
