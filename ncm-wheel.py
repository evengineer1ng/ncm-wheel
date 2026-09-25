#!/usr/bin/env python3
"""Reference force-feedback companion for NCM Online (IDEA-386).

**This drives no hardware.** It is the shape JoyMoCo's side should take, and a harness for watching the
telemetry arrive before any force is applied to a real wheel. It serves the bridge page, accepts the
same-origin WebSocket, decodes frames and prints what it would have commanded.

Architecture, proven live in MASTER_TEST section AJ (L129): a bundled OPEN//77 WebUI page has NO outbound
network, so the direction is inverted. The companion serves an HTML page; NCM creates a surface whose `entry`
is that URL; `Open77.webui.create` injects the Lua bridge into that origin; the page relays Lua events over a
same-origin socket. No TLS, because loopback is already a secure context.

    python tools/ffb-dev-companion.py [--port 38480] [--class belt]
    then in game:  /ncm.ffb on

--------------------------------------------------------------------------------------------------
SAFETY -- the part to read before connecting a real wheel
--------------------------------------------------------------------------------------------------
This mirrors `ncm/core/driver/feedback.lua`, which is the authority and is covered by the gate. Keep the two
in step; if they ever disagree, the Lua is right.

**SDL haptic magnitude is normalised, and normalised is not equal.** A 0.5 constant force is about a
newton-metre on a gear-drive G29 and about ten on a 20 Nm direct-drive base. The same number is a nudge on one
device and a wrist injury on another. So:

  * ceilings are per device CLASS, and the classes nobody here can test are held far below the ones we can;
  * an unidentified device is assumed to be the most dangerous thing it could be;
  * the rate of change is capped as well as the level, because a spike is what hurts, not a sustained level;
  * tuning is a LADDER, not a slider -- fixed rungs, fixed small step, hard last rung.

Owner's method, 2026-09-24: *"start at the lowest ffb settings, and increment slightly until further is
unnecessary then STOP. An important feature for us should not cost any wrists."*
"""
from __future__ import annotations

import argparse
import atexit
import base64
import collections
import io
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Everything this program prints, kept so the window can show it and the clipboard button can carry it.
# A packaged build has no console at all, so without this the output would go nowhere -- which is exactly
# how a tester ends up being asked to find a log file.
LOG = collections.deque(maxlen=400)
STATE = {
    "port": None, "listening": False, "connected": False, "armed": False,
    "wheel": None, "pedals": None, "rim": None, "gamepads": [],
    "device": None, "device_class": None, "rung": None, "problems": [],
    "frames": 0, "version": "0.2.0",
}


LOG_FILE = {"path": None}


class _Tee(object):
    """Write to the real stdout when there is one, always into LOG, and to a file when asked."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        for line in str(text).splitlines():
            if line.strip():
                LOG.append(line)
                if LOG_FILE["path"]:
                    try:
                        with io.open(LOG_FILE["path"], "a", encoding="utf-8") as fh:
                            fh.write(line + chr(10))
                    except Exception:                           # noqa: BLE001 - logging must never break us
                        LOG_FILE["path"] = None
        if self.stream is not None:
            try:
                self.stream.write(text)
            except Exception:                                   # noqa: BLE001 - a dead console must not kill us
                self.stream = None
        return len(text)

    def flush(self):
        if self.stream is not None:
            try:
                self.stream.flush()
            except Exception:                                   # noqa: BLE001
                self.stream = None


def problem(text):
    """Something the user has to act on. Kept separate from the log so it can be shown, not buried."""
    if text not in STATE["problems"]:
        STATE["problems"].append(text)
    print("[!!!] " + text, flush=True)
CRLF = chr(13) + chr(10)

FRAME_VERSION = 1

# Mirror of driver.feedback. The Lua is authoritative.
# Measured 2026-09-24 on a G29: the previous set was reasoned rather than tested, and a full-ladder run with
# the constant centring effect was "fairly strong from first rung to too strong by the end". Lower floor,
# finer steps, ceilings down by roughly 60%. Mirrors driver.feedback, which is authoritative.
CEILING = {"gear": 0.24, "belt": 0.20, "direct_drive": 0.06, "unknown": 0.04}
FLOOR = 0.02
STEP = 0.02
MAX_SLEW_PER_SECOND = 1.5


def ladder(device_class: str) -> list[float]:
    """Every permitted setting for a class, lowest first. A tuning session starts at [0] and stops the moment
    another rung adds nothing."""
    ceiling = CEILING.get(device_class)
    if ceiling is None:
        return []
    out, v = [], FLOOR
    while v <= ceiling + 1e-9:
        out.append(round(v, 3))
        v += STEP
    return out


def clamp(device_class: str, requested) -> float:
    """The only way a magnitude leaves this module."""
    ceiling = CEILING.get(device_class, CEILING["unknown"])
    try:
        v = float(requested)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return 0.0
    return min(v, ceiling)


def slew(previous: float, target, dt: float, max_per_second: float = MAX_SLEW_PER_SECOND) -> float:
    """No sudden full-scale step, ever. An unreadable target HOLDS rather than dropping to zero."""
    try:
        want = float(target)
    except (TypeError, ValueError):
        return previous
    if want != want:
        return previous
    step = max_per_second * max(0.0, dt)
    if step <= 0:
        return previous
    delta = want - previous
    if delta > step:
        return previous + step
    if delta < -step:
        return previous - step
    return want


# --------------------------------------------------------------------------------------------------
# Device discovery. Reporting only -- nothing here opens a haptic effect.
# --------------------------------------------------------------------------------------------------
#
# The companion is the only thing in the system that can see the hardware, so it is the only thing that may
# describe it. **Classification decides the ceiling**, so getting it wrong in the permissive direction is the
# one mistake that matters: anything not confidently recognised stays `unknown`, which carries the lowest cap.
#
# Name matching is crude on purpose. A curated list of substrings we can defend beats a clever heuristic that
# might promote an unknown direct-drive base into the `belt` ceiling.
BELT_OR_GEAR = {
    "g29": "gear", "g920": "gear", "g923": "gear", "g27": "gear", "g25": "gear", "driving force": "gear",
    "t300": "belt", "t150": "belt", "tmx": "belt", "t500": "belt", "thrustmaster": "belt",
    "csl elite": "belt", "clubsport": "belt",
}
DIRECT_DRIVE = ("moza", "simucube", "simagic", "fanatec dd", "podium", "csl dd", "vrs ", "asetek", "cammus")


def classify(name: str) -> str:
    """A device class, or `unknown`. **Never guesses upward.**"""
    n = (name or "").lower()
    for token in DIRECT_DRIVE:
        if token in n:
            return "direct_drive"
    for token, cls in BELT_OR_GEAR.items():
        if token in n:
            return cls
    return "unknown"


def discover():
    """The first haptic-capable device SDL can see, described but NOT opened for output.

    Returns (name, class, capabilities dict) or (None, "unknown", {}). An import failure is reported rather
    than crashing the bridge: telemetry is still worth watching on a machine with no wheel attached.
    """
    try:
        import sdl2
    except Exception as exc:                                    # noqa: BLE001 - diagnostic path
        print("[ffb] SDL unavailable (%s); running without hardware discovery" % exc, flush=True)
        return None, "unknown", {}
    try:
        sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_JOYSTICK | sdl2.SDL_INIT_HAPTIC)
        count = sdl2.SDL_NumHaptics()
        if count < 1:
            print("[ffb] SDL sees no haptic device. On Logitech wheels G HUB must be installed and running.",
                  flush=True)
            return None, "unknown", {}
        for i in range(count):
            raw_i = sdl2.SDL_HapticName(i)
            nm_i = raw_i.decode(errors="replace") if isinstance(raw_i, bytes) else str(raw_i)
            print("[ffb] haptic %d: %s" % (i, nm_i), flush=True)
        raw = sdl2.SDL_HapticName(0)
        name = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        dev = sdl2.SDL_HapticOpen(0)
        caps = {}
        if dev:
            bits = sdl2.SDL_HapticQuery(dev)
            for label, flag in (("constant", sdl2.SDL_HAPTIC_CONSTANT), ("sine", sdl2.SDL_HAPTIC_SINE),
                                ("leftright", sdl2.SDL_HAPTIC_LEFTRIGHT), ("damper", sdl2.SDL_HAPTIC_DAMPER),
                                ("spring", sdl2.SDL_HAPTIC_SPRING)):
                caps[label] = bool(bits & flag)
            # Closed again immediately. Discovery must not leave a device held open by a process that is not
            # going to drive it.
            sdl2.SDL_HapticClose(dev)
        return name, classify(name), caps
    except Exception as exc:                                    # noqa: BLE001 - diagnostic path
        print("[ffb] device discovery failed: %s" % exc, flush=True)
        return None, "unknown", {}


BRIDGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>NCM FFB bridge</title></head>
<body style="margin:0;background:transparent">
<script>
// The whole page. It owns no policy and makes no decisions -- it relays Lua events to a same-origin socket
// and reports the socket's state back to Lua, so the client knows whether a companion is actually listening.
(function () {
  var ws = null, ready = false;
  var tell = function (event, detail) {
    try { if (window.Open77 && Open77.emit) Open77.emit(event, { detail: String(detail) }); } catch (e) {}
  };
  var connect = function () {
    try {
      ws = new WebSocket("ws://" + location.host + "/telemetry");
    } catch (e) { tell("ncm:ffb.closed", "construct failed: " + e); return; }
    ws.onopen = function () { ready = true; tell("ncm:ffb.ready", location.origin); };
    // Upward: whatever the companion says about the hardware. Passed through untouched -- this page owns no
    // policy and must not be the place a device class quietly changes.
    ws.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.device && window.Open77 && Open77.emit) Open77.emit("ncm:ffb.device", msg.device);
      } catch (e) {}
    };
    ws.onclose = function () { ready = false; tell("ncm:ffb.closed", "socket closed"); setTimeout(connect, 2000); };
    ws.onerror = function () { ready = false; };
  };
  var relay = function (payload) {
    if (!ready || !ws) return;
    try { ws.send(JSON.stringify(payload)); } catch (e) {}
  };
  if (window.Open77 && Open77.on) {
    Open77.on("ncm:ffb.frame", relay);
    // `idle` is sent deliberately instead of going quiet: silence and "no force" are different instructions,
    // and a companion holding the last frame would keep pushing at a wheel whose car it can no longer see.
    Open77.on("ncm:ffb.idle", function (p) { relay({ v: (p && p.v) || 1, idle: true }); });
    // Downward: panel settings and the test pulse take the SAME socket the telemetry came up. One transport,
    // so the two sides cannot disagree about which is authoritative.
    Open77.on("ncm:ffb.settings", function (p) { relay({ cmd: "settings", settings: p || {} }); });
    Open77.on("ncm:ffb.test", function (p) { relay({ cmd: "test", test: p || {} }); });
  }
  connect();
})();
</script>
</body></html>
"""


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _read_frame(conn: socket.socket):
    head = conn.recv(2)
    if len(head) < 2:
        return None
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if masked else b"\x00\x00\x00\x00"
    payload = b""
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


# --------------------------------------------------------------------------------------------------
# Input: wheel and pedals -> one virtual Xbox 360 pad.
# --------------------------------------------------------------------------------------------------
#
# **This is what makes the support native rather than a companion hack.** Cyberpunk reads an ordinary Xbox
# controller; ViGEmBus lets us present one; SDL reads the real hardware. Nothing about it is NCM-specific,
# which is the point -- the wheel steers the car in menus, in free roam and in someone else's gamemode, not
# only while NCM is looking.
#
# Observed on this machine, game and JoyMoCo both closed:
#     joystick 0  "Sim Pedals"                                   3 axes, all resting at -32768
#     joystick 1  "Logitech G HUB G29 ... Racing Wheel USB"       4 axes, wheel centred at 0
# So pedals are a SEPARATE device from the wheel, and a pedal at rest reads minimum rather than centre.

# --------------------------------------------------------------------------------------------------
# Input: a rig, a gamepad, or both -> one virtual Xbox 360 pad.
# --------------------------------------------------------------------------------------------------
#
# **Nothing about anyone's hardware is hardcoded here.** This is going on a public server, so a rig that
# nobody here has ever seen has to work. Roles are decided by SHAPE, and a profile file only supplies what
# shape cannot tell you -- which button on an unlabelled rim is `a`.
#
#   steering  an axis that rests near centre and travels both ways
#   pedals    axes that rest at one END of travel
#   rim       many buttons and no usable axes
#   gamepad   anything SDL's own GameController database recognises
#
# That last one matters most: a standard Xbox pad needs **no profile and no mapping**, because SDL already
# knows it. The owner's custom rim needs a profile because no database will ever contain it.
#
# **The gamepad is never displaced by the rig.** Both feed the same virtual pad and the larger deflection
# wins, so a driver can hold a controller in their hands and keep the wheel in front of them. On foot the rig
# is suppressed entirely -- a wheel should not be nudging the walk axis while you are walking.

def config_path():
    """Where a user's own device choices live -- beside their data, not beside the program, because the
    program may be a read-only exe sitting in Downloads."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "NCM Wheel Support")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:                                           # noqa: BLE001
        folder = os.path.expanduser("~")
    return os.path.join(folder, "devices.json")


def load_overrides():
    try:
        # `utf-8-sig` because a file written by Notepad or PowerShell's Out-File carries a BOM, and
        # `json.load` refuses it. Silently ignoring somebody's hand-edited config is a bad failure.
        with io.open(config_path(), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if data:
            print("[in ] using your saved device choices (%s)" % config_path(), flush=True)
        return data or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        # Say so. A config that cannot be parsed is not the same as no config, and the difference is the
        # whole reason someone would be confused about why their choices were ignored.
        print("[in ] your saved device choices could not be read (%s); using detection" % exc, flush=True)
        return {}


def save_overrides(data):
    try:
        with io.open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        print("[in ] device choices saved", flush=True)
        return True
    except Exception as exc:                                    # noqa: BLE001
        print("[in ] could not save device choices: %s" % exc, flush=True)
        return False


def _bundle_dir():
    """Where our own data files live.

    PyInstaller unpacks a onefile build into a temporary folder and points `sys._MEIPASS` at it. Using
    `__file__` there resolves inside that folder too -- but only for files that were actually bundled, which
    is the trap this fell into: the path looked right and the file was not there."""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


PROFILE_DIR = os.path.join(_bundle_dir(), "wheel-profiles")

# A button box or rim reports no useful name on some hardware (the owner's reads as "axis 28 button device"),
# so it is recognised by shape as well: many buttons and no axes that behave like controls.
RIM_HINTS = ("evenracing", "button device", "button box", "rim", "wheelbase buttons")

BUTTON_NAMES = ("a", "b", "x", "y", "lb", "rb", "back", "start", "home", "ls", "rs",
                "dpad_up", "dpad_down", "dpad_left", "dpad_right")


def load_profiles(path=None):
    """Every profile we can find, plus the defaults block. Missing or broken files are not fatal: a rig
    still steers without a profile, and refusing to start over a JSON typo would be a poor trade."""
    path = path or os.path.join(PROFILE_DIR, "default.json")
    try:
        with io.open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("defaults", {}), data.get("profiles", [])
    except Exception as exc:                                    # noqa: BLE001 - diagnostic path
        print("[in ] no rig profiles loaded (%s); detection only" % exc, flush=True)
        return {}, []


def match_profile(name, profiles, role=None):
    low = (name or "").lower()
    best = None
    for p in profiles:
        if role and p.get("role") != role:
            continue
        for token in p.get("match", []):
            if token.lower() in low:
                # Longest match wins, so "fanatec dd" beats "fanatec".
                if best is None or len(token) > best[0]:
                    best = (len(token), p)
    return best[1] if best else None


class Device:
    """One physical thing, with the role we decided it plays."""

    def __init__(self, index, name, handle, role, profile=None):
        self.index, self.name, self.handle = index, name, handle
        self.role, self.profile = role, profile or {}
        self.rest = {}
        self.polled = False
        self.axis_count = 0
        self.button_count = 0
        self.centred_axes = []
        self.rest_axes = []


class Input:
    """Reads whatever is attached and drives a virtual Xbox 360 pad.

    **Steering range is the setting people will actually argue about.** A 900-degree wheel mapped one-to-one
    onto a thumbstick gives a car that barely turns -- full deflection would need half a turn. Only the middle
    is used: `steer_degrees` per side maps to full deflection, the rest clamps. 45 is one driver's comfort,
    not a fact, so it is adjustable live from the NCM panel.
    """

    def __init__(self, steer_degrees=45.0, wheel_range=900.0, profile_path=None):
        self.steer_degrees = max(5.0, float(steer_degrees))
        self.wheel_range = max(90.0, float(wheel_range))
        self.defaults, self.profiles = load_profiles(profile_path)
        self.steer_deadzone = float(self.defaults.get("steer_deadzone", 0.015))
        self.steer_curve = float(self.defaults.get("steer_curve", 1.3))
        self.pad_deadzone = float(self.defaults.get("gamepad_deadzone", 0.08))
        self.sdl = None
        self.vg = None
        self.pad = None
        self.devices = []
        self.controllers = []
        self.steering = self.pedals = self.rim = None
        self.steer_axis = 0
        self.pedal_axes = {}
        self.candidates = []
        self.overrides = load_overrides()
        self.detected_class = "unknown"
        self.running = False
        self.thread = None
        self.last = {}
        self.pressed = set()
        # **Seat gating.** None means "NCM has not told us anything", and the rig stays live -- the companion
        # must be useful on its own. Only an explicit `False` from a connected bridge suppresses it.
        self.seated = None

    @property
    def scale(self):
        return (self.wheel_range / 2.0) / self.steer_degrees

    def set_steer_degrees(self, degrees):
        self.steer_degrees = max(5.0, min(540.0, float(degrees)))
        return self.steer_degrees

    # ---------------------------------------------------------------- detection
    def _rest_values(self, sdl2, handle, n_axes):
        """Where every axis sits when nobody is touching it.

        **The first read after opening is a lie** -- SDL reports 0 for every axis until it has actually
        polled, and believing that made a released pedal look fully pressed, with the virtual pad holding
        full throttle and full brake at once. So poll until something is non-zero, or give up after two
        seconds and say so."""
        deadline = time.monotonic() + 2.0
        values = [0] * n_axes
        while time.monotonic() < deadline:
            sdl2.SDL_JoystickUpdate()
            values = [sdl2.SDL_JoystickGetAxis(handle, a) for a in range(n_axes)]
            if any(v != 0 for v in values):
                return values, True
            time.sleep(0.02)
        return values, False

    def _classify_axes(self, values):
        """Which axes are steering and which are pedals, by where they REST.

        A steering axis sits at centre and travels both ways. A pedal sits at one end of its travel. That
        distinction is what a device name cannot tell you and what every rig has in common, so it is the
        thing worth testing."""
        centred, pedals = [], []
        for axis, v in enumerate(values):
            if abs(v) < 4000:
                centred.append(axis)
            elif abs(v) > 24000:
                pedals.append(axis)
        return centred, pedals

    def open(self):
        try:
            import sdl2
            import vgamepad as vg
        except Exception as exc:                                # noqa: BLE001 - diagnostic path
            # Name the cause and the fix. "input unavailable" on its own sends someone hunting through a
            # stack trace for a driver they have never heard of.
            detail = str(exc)
            # **Ask before blaming.** This previously reported "ViGEmBus is not installed" whenever the
            # library failed for any reason -- including on a machine where the driver was installed and
            # running, where the real fault was a packaging bug of ours. A confidently wrong diagnostic
            # sends someone to fix something that was never broken.
            if not vigem_present():
                return ("ViGEmBus is not installed, so the wheel and pedals cannot drive the car. "
                        "Close and reopen this program and it will offer to install it for you. "
                        "(Force feedback does not need it and works either way.)")
            return ("The virtual controller could not start even though ViGEmBus is installed. "
                    "This is a fault on our side, not yours -- please send the diagnostics. (%s)" % detail)
        self.sdl, self.vg = sdl2, vg
        try:
            sdl2.SDL_Init(sdl2.SDL_INIT_JOYSTICK | sdl2.SDL_INIT_GAMECONTROLLER)
            sdl2.SDL_JoystickEventState(sdl2.SDL_IGNORE)
            sdl2.SDL_GameControllerEventState(sdl2.SDL_IGNORE)

            for i in range(sdl2.SDL_NumJoysticks()):
                raw = sdl2.SDL_JoystickNameForIndex(i)
                nm = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw))
                # **A recognised gamepad is taken as a gamepad and never taken apart.** SDL's own database
                # already knows which button is `a` on an Xbox pad; re-deriving that would be worse.
                if sdl2.SDL_IsGameController(i):
                    ctrl = sdl2.SDL_GameControllerOpen(i)
                    if ctrl:
                        self.controllers.append((nm, ctrl))
                        print("[in ] gamepad : %d %s (SDL mapping)" % (i, nm), flush=True)
                        continue
                handle = sdl2.SDL_JoystickOpen(i)
                if not handle:
                    continue

                profile = match_profile(nm, self.profiles)
                n_axes = sdl2.SDL_JoystickNumAxes(handle)
                n_buttons = sdl2.SDL_JoystickNumButtons(handle)
                values, polled = self._rest_values(sdl2, handle, n_axes)
                ignore = set((profile or {}).get("ignore_axes") or [])
                usable = [v if a not in ignore else 0 for a, v in enumerate(values)]
                centred, pedal_axes = self._classify_axes(usable)
                for a in ignore:
                    if a in centred:
                        centred.remove(a)
                dev = Device(i, nm, handle, "", profile)
                dev.rest = {a: values[a] for a in range(n_axes)}
                dev.polled = polled
                dev.axis_count, dev.button_count = n_axes, n_buttons
                dev.centred_axes, dev.rest_axes = list(centred), list(pedal_axes)
                # **Every device is a candidate for every role**, whatever detection decides next. The
                # window needs the whole list to offer a choice, not only the ones that got picked.
                self.candidates.append(dev)
                tag = (profile or {}).get("name")
                roles = []

                forced = self.overrides.get("roles") or {}
                if forced:
                    # A user's choice outranks detection completely -- including a choice of "none".
                    steer = forced.get("steering") or {}
                    if steer.get("name") == nm and self.steering is None:
                        self.steering = dev
                        self.steer_axis = int(steer.get("axis", centred[0] if centred else 0))
                        self.detected_class = steer.get("class") or (profile or {}).get(
                            "device_class", "unknown")
                        roles.append("steering ax%d (your choice)" % self.steer_axis)
                    ped = forced.get("pedals") or {}
                    if ped.get("name") == nm and self.pedals is None:
                        self.pedals = dev
                        for which in ("throttle", "brake", "clutch"):
                            if ped.get(which) is not None and int(ped[which]) >= 0:
                                self.pedal_axes[which] = int(ped[which])
                        roles.append("pedals (your choice) " + ", ".join(
                            "%s=ax%s" % (k, v) for k, v in sorted(self.pedal_axes.items())))
                    rim = forced.get("rim") or {}
                    if rim.get("name") == nm and self.rim is None:
                        self.rim = dev
                        roles.append("%d buttons (your choice)" % n_buttons)
                    # Anything the user did not name stays unassigned rather than being guessed into a role
                    # they deliberately left empty.
                    print("[in ] device  : %d %s -- %s"
                          % (i, nm, "; ".join(roles) if roles else "available, not assigned"), flush=True)
                    continue

                # Nothing is claimed here. Enumeration order used to decide who won a role, which is the
                # wrong judge: a 25-button G29 would take the rim role before a dedicated button box further
                # down the list was ever looked at. Collect now, choose in `_assign_roles`.
                print("[in ] found   : %d %s%s -- %d axes (%d centred, %d at rest), %d buttons"
                      % (i, nm, (" [%s]" % tag) if tag else "", n_axes, len(centred), len(pedal_axes),
                         n_buttons), flush=True)

            if not (self.overrides.get("roles") or {}):
                self._assign_roles()

            if self.steering is None and not self.controllers:
                return "nothing usable attached"

            self.pad = vg.VX360Gamepad()
            self._calibrate_pedals()
            return ""
        except Exception as exc:                                # noqa: BLE001 - diagnostic path
            return "input open failed: %s" % exc

    def _assign_roles(self):
        """Choose which device plays which role, looking at all of them together.

        Preference order, most specific first:
          * a profile that names the role outright
          * a device that can only be that thing (pedals with no centred axis, a button box with no axes)
          * the wheel itself, which on an all-in-one unit legitimately holds all three
        """
        cands = self.candidates

        def profiled(role):
            return [d for d in cands if (d.profile or {}).get("role") == role]

        # --- steering: an axis that rests at centre ---
        pool = profiled("steering") or [d for d in cands if d.centred_axes and d.button_count < 26] or [d for d in cands if d.centred_axes]
        if pool:
            dev = pool[0]
            self.steering = dev
            self.steer_axis = (dev.profile or {}).get("axes", {}).get(
                "steer", dev.centred_axes[0] if dev.centred_axes else 0)
            self.detected_class = (dev.profile or {}).get("device_class", "unknown")

        # --- pedals: axes that rest at an end of travel ---
        pool = profiled("pedals") or [d for d in cands if d.rest_axes and not d.centred_axes] or [d for d in cands if d.rest_axes]
        if pool:
            dev = pool[0]
            self.pedals = dev
            mapped = (dev.profile or {}).get("axes") or {}
            for which, nth in (("throttle", 0), ("brake", 1), ("clutch", 2)):
                if which in mapped:
                    self.pedal_axes[which] = mapped[which]
                elif nth < len(dev.rest_axes):
                    # Axis order is a convention, not a fact, so it is printed for a tester to correct.
                    self.pedal_axes[which] = dev.rest_axes[nth]

        # --- buttons: a dedicated box first, then whatever the wheel itself offers ---
        pool = (profiled("rim")
                or [d for d in cands if d.button_count >= 12 and not d.centred_axes and not d.rest_axes]
                or [d for d in cands if d is not self.steering and d.button_count >= 12]
                or [d for d in cands if d.button_count > 0])
        if pool:
            self.rim = pool[0]

        for dev in cands:
            roles = []
            if dev is self.steering:
                roles.append("steering ax%d" % self.steer_axis)
            if dev is self.pedals:
                roles.append("pedals " + ", ".join("%s=ax%s" % (k, v)
                                                   for k, v in sorted(self.pedal_axes.items())))
            if dev is self.rim:
                roles.append("%d buttons%s" % (dev.button_count,
                             "" if (dev.profile or {}).get("buttons") else " (default map)"))
            if roles:
                print("[in ] %-8s: %s -- %s" % ("assigned", dev.name, "; ".join(roles)), flush=True)

    def _calibrate_pedals(self):
        """Rest positions are learned during detection; this only reports them and covers the case where SDL
        never answered at all."""
        if self.pedals is None:
            return
        if not self.pedals.polled:
            fallback = int(self.pedals.profile.get("released", -32768))
            for axis in self.pedal_axes.values():
                self.pedals.rest[axis] = fallback
            print("[in ] pedal rest: SDL never reported; assuming %d released" % fallback, flush=True)
            return
        print("[in ] pedal rest: %s (feet off while this starts)"
              % ({k: self.pedals.rest.get(v) for k, v in sorted(self.pedal_axes.items())},), flush=True)

    # ---------------------------------------------------------------- reading
    def _pedal(self, which):
        if self.pedals is None:
            return 0.0
        axis = self.pedal_axes.get(which)
        if axis is None or axis not in self.pedals.rest:
            return 0.0
        v = self.sdl.SDL_JoystickGetAxis(self.pedals.handle, axis)
        rest = self.pedals.rest[axis]
        span = 65535.0 if abs(rest) > 16384 else 32767.0
        return max(0.0, min(1.0, abs(v - rest) / span))

    def _steer(self):
        if self.steering is None:
            return 0.0
        raw = self.sdl.SDL_JoystickGetAxis(self.steering.handle, self.steer_axis) / 32767.0
        value = max(-1.0, min(1.0, raw * self.scale))
        if abs(value) < self.steer_deadzone:
            return 0.0
        if self.steer_curve != 1.0:
            value = (1.0 if value > 0 else -1.0) * (abs(value) ** self.steer_curve)
        return value

    def _gamepad_state(self):
        """Whatever the handheld controller is doing, through SDL's own mapping."""
        sdl2 = self.sdl
        state = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": set()}
        axis_map = {"lx": sdl2.SDL_CONTROLLER_AXIS_LEFTX, "ly": sdl2.SDL_CONTROLLER_AXIS_LEFTY,
                    "rx": sdl2.SDL_CONTROLLER_AXIS_RIGHTX, "ry": sdl2.SDL_CONTROLLER_AXIS_RIGHTY}
        btn_map = {"a": sdl2.SDL_CONTROLLER_BUTTON_A, "b": sdl2.SDL_CONTROLLER_BUTTON_B,
                   "x": sdl2.SDL_CONTROLLER_BUTTON_X, "y": sdl2.SDL_CONTROLLER_BUTTON_Y,
                   "lb": sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER,
                   "rb": sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER,
                   "back": sdl2.SDL_CONTROLLER_BUTTON_BACK, "start": sdl2.SDL_CONTROLLER_BUTTON_START,
                   "home": sdl2.SDL_CONTROLLER_BUTTON_GUIDE,
                   "ls": sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK,
                   "rs": sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK,
                   "dpad_up": sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP,
                   "dpad_down": sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN,
                   "dpad_left": sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT,
                   "dpad_right": sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT}
        for _, ctrl in self.controllers:
            for key, axis in axis_map.items():
                v = sdl2.SDL_GameControllerGetAxis(ctrl, axis) / 32767.0
                # **Stick drift is not steering.** A resting Xbox pad reported 0.032 here, and because the
                # merge takes the larger deflection that drift beat a perfectly centred wheel and put a
                # permanent lean into the car. Deadzone first, merge second.
                if abs(v) < self.pad_deadzone:
                    v = 0.0
                if abs(v) > abs(state[key]):
                    state[key] = max(-1.0, min(1.0, v))
            for key, axis in (("lt", sdl2.SDL_CONTROLLER_AXIS_TRIGGERLEFT),
                              ("rt", sdl2.SDL_CONTROLLER_AXIS_TRIGGERRIGHT)):
                v = max(0.0, sdl2.SDL_GameControllerGetAxis(ctrl, axis) / 32767.0)
                if v < self.pad_deadzone:
                    v = 0.0
                state[key] = max(state[key], v)
            for name, btn in btn_map.items():
                if sdl2.SDL_GameControllerGetButton(ctrl, btn):
                    state["buttons"].add(name)
        return state

    # Used when a wheel has buttons and no profile. DirectInput wheels vary, but the first few buttons are
    # face buttons and shoulders far more often than not, so this is a reasonable start.
    #
    # **`home` is deliberately absent.** It is the Guide button: binding it to an unknown paddle could drop
    # somebody out of the game mid-corner, and an unmapped button is a much smaller problem than that. `ls`
    # and `rs` are left out for the same reason -- a stick click bound to a paddle is a surprise nobody asked
    # for. Anything wrong here is fixable in Devices...
    DEFAULT_RIM_BUTTONS = {
        0: "a", 1: "b", 2: "x", 3: "y",
        4: "lb", 5: "rb", 6: "back", 7: "start",
        8: "dpad_up", 9: "dpad_down", 10: "dpad_left", 11: "dpad_right",
    }

    def rim_pressed(self):
        """Which buttons are held on the button device right now. Used by the binding dialog, which needs
        to see a press rather than be told about one."""
        out = []
        if self.rim is None or self.sdl is None:
            return out
        try:
            self.sdl.SDL_JoystickUpdate()
            for i in range(self.rim.button_count):
                if self.sdl.SDL_JoystickGetButton(self.rim.handle, i):
                    out.append(i)
        except Exception:                                       # noqa: BLE001
            pass
        return out

    def button_map(self):
        """The map in force, and where it came from. Precedence: what the user bound, then a shipped
        profile, then the default guess."""
        user = ((self.overrides.get("roles") or {}).get("rim") or {}).get("buttons")
        if user:
            return {int(k): v for k, v in user.items()}, "yours"
        if self.rim is not None and (self.rim.profile or {}).get("buttons"):
            return {int(k): v for k, v in self.rim.profile["buttons"].items()}, "profile"
        count = self.rim.button_count if self.rim else 0
        return {k: v for k, v in self.DEFAULT_RIM_BUTTONS.items() if k < max(1, count)}, "default guess"

    def _rim_buttons(self):
        """Rim buttons. A profile is authoritative; without one, a conservative default map applies so an
        unrecognised wheel is not left unable to press anything."""
        out = set()
        if self.rim is None:
            return out
        mapping, _ = self.button_map()
        ignored = set(self.rim.profile.get("ignore_buttons") or [])
        for raw_index, target in mapping.items():
            index = int(raw_index)
            if index in ignored or target not in BUTTON_NAMES:
                continue
            if self.sdl.SDL_JoystickGetButton(self.rim.handle, index):
                out.add(target)
        return out

    # ---------------------------------------------------------------- output
    def _tick(self):
        sdl2 = self.sdl
        sdl2.SDL_JoystickUpdate()
        sdl2.SDL_GameControllerUpdate()

        pad_state = self._gamepad_state()
        # On foot, the rig contributes nothing. Only an explicit `False` from a connected NCM suppresses it;
        # `None` means nobody has said, and the companion has to work on its own.
        rig_live = self.seated is not False
        steer = self._steer() if rig_live else 0.0
        throttle = self._pedal("throttle") if rig_live else 0.0
        brake = self._pedal("brake") if rig_live else 0.0
        rim = self._rim_buttons() if rig_live else set()

        # Larger deflection wins, so holding a controller never fights the wheel in front of you.
        lx = steer if abs(steer) >= abs(pad_state["lx"]) else pad_state["lx"]
        rt = max(throttle, pad_state["rt"])
        lt = max(brake, pad_state["lt"])
        held = rim | pad_state["buttons"]

        self.pad.left_joystick_float(x_value_float=lx, y_value_float=-pad_state["ly"])
        self.pad.right_joystick_float(x_value_float=pad_state["rx"], y_value_float=-pad_state["ry"])
        self.pad.right_trigger_float(value_float=rt)
        self.pad.left_trigger_float(value_float=lt)

        for name in BUTTON_NAMES:
            target = getattr(self.vg.XUSB_BUTTON, "XUSB_GAMEPAD_" + {
                "lb": "LEFT_SHOULDER", "rb": "RIGHT_SHOULDER", "home": "GUIDE",
                "ls": "LEFT_THUMB", "rs": "RIGHT_THUMB",
                "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN",
                "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
            }.get(name, name.upper()))
            if name in held and name not in self.pressed:
                self.pressed.add(name)
                self.pad.press_button(button=target)
            elif name not in held and name in self.pressed:
                self.pressed.discard(name)
                self.pad.release_button(button=target)

        self.pad.update()
        self.last = {"steer": lx, "throttle": rt, "brake": lt, "rig": rig_live,
                     "buttons": sorted(self.pressed)}

    def start(self):
        if self.running or self.pad is None:
            return
        self.running = True

        def loop():
            # 120 Hz. Steering latency is the one thing a driver feels directly.
            while self.running:
                try:
                    self._tick()
                except Exception:                               # noqa: BLE001 - never let input die silently
                    pass
                time.sleep(1.0 / 120.0)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        print("[in ] steering %.0f deg/side (range %.0f, x%.1f, curve %.1f) | %d gamepad(s) merged"
              % (self.steer_degrees, self.wheel_range, self.scale, self.steer_curve, len(self.controllers)),
              flush=True)

    def describe(self):
        return {
            "steering": self.steering.name if self.steering else None,
            "pedals": self.pedals.name if self.pedals else None,
            "rim": self.rim.name if self.rim else None,
            "gamepads": [n for n, _ in self.controllers],
            "steerDegrees": self.steer_degrees,
            "wheelRange": self.wheel_range,
            "rigLive": self.seated is not False,
            "pedalAxes": dict(self.pedal_axes),
            "steerAxis": self.steer_axis,
        }

    def layout(self):
        """Everything the window needs to draw its dropdowns: what exists, and what is currently assigned."""
        return {
            "devices": [{"name": d.name, "axes": d.axis_count, "buttons": d.button_count,
                         "centred": d.centred_axes, "atRest": d.rest_axes} for d in self.candidates],
            "steering": self.steering.name if self.steering else None,
            "steerAxis": self.steer_axis,
            "pedals": self.pedals.name if self.pedals else None,
            "pedalAxes": dict(self.pedal_axes),
            "rim": self.rim.name if self.rim else None,
            "deviceClass": self.detected_class,
        }

    def stop(self):
        """Release everything. A virtual pad left holding a trigger is a car left accelerating."""
        self.running = False
        try:
            if self.pad is not None:
                self.pad.reset()
                self.pad.update()
        except Exception:                                       # noqa: BLE001
            pass
        # Release the devices too, so re-opening with different roles is possible without a restart.
        for dev in self.candidates:
            try:
                self.sdl.SDL_JoystickClose(dev.handle)
            except Exception:                                   # noqa: BLE001
                pass
        for _, ctrl in self.controllers:
            try:
                self.sdl.SDL_GameControllerClose(ctrl)
            except Exception:                                   # noqa: BLE001
                pass
        self.candidates, self.controllers = [], []
        self.steering = self.pedals = self.rim = None


class Output:
    """The only thing in this program that can move a wheel.

    **Two effects, because a wheel is not a gamepad.**

      * `CONSTANT` -- a directional torque, used as *self-aligning torque*: the wheel loading up as you turn
        and wanting to return to centre. This is what hands actually read, and a sine alone reads as nothing.
        The direction always points BACK TOWARD CENTRE, so it assists the driver the way a real car does
        rather than fighting them. That is why a constant effect is safe here despite being refused in the
        first draft: the danger was never the effect type, it was an arbitrary direction.
      * `SINE` at 125 Hz -- surface texture and slip, layered on top. 25 Hz was the first attempt and it is
        slow enough that a wheel's inertia swallows it; 125 Hz is what `anyffb` uses on this exact hardware.

    **Cleanup is the part that went wrong in the first version and must not go wrong again.** The owner's
    wheel was left powered, pulled fully to one side, resisting return to centre -- after `stop()` had been
    called, the process had exited, and the device had been closed. `SDL_HapticNumEffectsPlaying` still read
    1. `SDL_HapticStopAll` does not reliably end an infinite effect; the effect must be zeroed, stopped AND
    destroyed, and the whole thing registered with `atexit` so a crash cannot skip it.
    """

    def __init__(self, device_class):
        self.device_class = device_class
        self.sdl = None
        self.haptic = None
        self.name = None
        self.caps = {}
        self.index = None
        self.opened_index = None
        self.dev = None
        self.mode = "none"
        self.sine = self.sine_id = None
        self.const = self.const_id = None
        self.conditions = {}
        self.axes = 1
        self.last_texture = -1.0
        self.last_torque = None
        self._registered = False

    # ---------------------------------------------------------------- lifecycle
    def open(self):
        try:
            import sdl2
            import sdl2.haptic as haptic
            from ctypes import c_long
        except Exception as exc:                                # noqa: BLE001 - diagnostic path
            return "SDL unavailable: %s" % exc
        self.sdl, self.haptic = sdl2, haptic
        try:
            if sdl2.SDL_Init(sdl2.SDL_INIT_HAPTIC) != 0:
                return "SDL_Init(HAPTIC) failed: %s" % sdl2.SDL_GetError()
            count = haptic.SDL_NumHaptics()
            if count < 1:
                return "no haptic device"

            attempts = []
            for i in range(count):
                if self.index is not None and i != self.index:
                    continue
                raw = haptic.SDL_HapticName(i)
                nm = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
                dev = haptic.SDL_HapticOpen(i)
                if not dev:
                    err = sdl2.SDL_GetError()
                    err = err.decode(errors="replace") if isinstance(err, bytes) else str(err)
                    attempts.append("%d:%s (%s)" % (i, nm, err.strip()))
                    print("[ffb] haptic %d: %s -- refused (%s)" % (i, nm, err.strip()), flush=True)
                    if not self.name:
                        self.name = nm
                    continue
                # **Opening is not the test; creating an effect is.** This wheel enumerates three times and
                # the middle one opens while being able to create nothing at all -- which looks like success
                # and is worse than a refusal.
                probe = self._sine_effect(c_long, 0)
                pid = haptic.SDL_HapticNewEffect(dev, probe)
                if pid >= 0:
                    haptic.SDL_HapticDestroyEffect(dev, pid)
                    self.name, self.dev, self.opened_index = nm, dev, i
                    print("[ffb] haptic %d: %s -- USABLE" % (i, nm), flush=True)
                    break
                haptic.SDL_HapticClose(dev)
                attempts.append("%d:%s (opens, cannot create effects)" % (i, nm))
                print("[ffb] haptic %d: %s -- opens but creates nothing" % (i, nm), flush=True)
                if not self.name:
                    self.name = nm
            if not self.dev:
                return "no haptic index would open -- tried %s" % "; ".join(attempts)

            bits = haptic.SDL_HapticQuery(self.dev)
            self.caps = {
                "constant": bool(bits & haptic.SDL_HAPTIC_CONSTANT),
                "sine": bool(bits & haptic.SDL_HAPTIC_SINE),
                "leftright": bool(bits & haptic.SDL_HAPTIC_LEFTRIGHT),
                "damper": bool(bits & haptic.SDL_HAPTIC_DAMPER),
                "spring": bool(bits & haptic.SDL_HAPTIC_SPRING),
                "friction": bool(bits & haptic.SDL_HAPTIC_FRICTION),
            }
            haptic.SDL_HapticSetGain(self.dev, 100)
            self.axes = max(1, haptic.SDL_HapticNumAxes(self.dev))
            print("[ffb] device axes: %d | effects: %d stored, %d playing"
                  % (self.axes, haptic.SDL_HapticNumEffects(self.dev),
                     haptic.SDL_HapticNumEffectsPlaying(self.dev)), flush=True)

            modes = []
            if self.caps["sine"]:
                self.sine = self._sine_effect(c_long, 0)
                self.sine_id = haptic.SDL_HapticNewEffect(self.dev, self.sine)
                if self.sine_id >= 0:
                    haptic.SDL_HapticRunEffect(self.dev, self.sine_id, 1)
                    modes.append("sine")
                else:
                    self.sine = self.sine_id = None
            if self.caps["constant"]:
                self.const = self._constant_effect(c_long, 0)
                self.const_id = haptic.SDL_HapticNewEffect(self.dev, self.const)
                if self.const_id >= 0:
                    haptic.SDL_HapticRunEffect(self.dev, self.const_id, 1)
                    modes.append("constant")
                else:
                    self.const = self.const_id = None
            # **Condition effects are what stiffness IS.** The device computes these from its own position
            # and velocity, continuously. Nothing we send per frame can imitate that, which is why the first
            # version felt floaty no matter how the force channels were tuned.
            for kind, attr in (("spring", "SPRING"), ("damper", "DAMPER")):
                if not self.caps.get(kind):
                    continue
                eff = self._condition_effect(c_long, getattr(haptic, "SDL_HAPTIC_" + attr), 0.0)
                eid = haptic.SDL_HapticNewEffect(self.dev, eff)
                if eid >= 0:
                    haptic.SDL_HapticRunEffect(self.dev, eid, 1)
                    self.conditions[kind] = (eff, eid)
                    modes.append(kind)

            if not modes:
                return "device created no usable effect"
            self.mode = "+".join(modes)

            if not self._registered:
                atexit.register(self.close)
                self._registered = True
            return ""
        except Exception as exc:                                # noqa: BLE001 - diagnostic path
            return "haptic open failed: %s" % exc

    def _sine_effect(self, c_long, magnitude):
        h = self.haptic
        e = h.SDL_HapticEffect()
        e.type = h.SDL_HAPTIC_SINE
        e.periodic.type = h.SDL_HAPTIC_SINE
        e.periodic.direction = h.SDL_HapticDirection(h.SDL_HAPTIC_CARTESIAN, (c_long * 3)(1, 0, 0))
        e.periodic.length = 0xFFFFFFFF
        e.periodic.period = 8                                   # 125 Hz: texture. 25 Hz was swallowed whole.
        e.periodic.magnitude = int(magnitude)
        e.periodic.offset = 0
        e.periodic.phase = 0
        return e

    def _condition_effect(self, c_long, kind, strength):
        """A SPRING or DAMPER at `strength` 0..1.

        **Configured for the axes the device actually has, which on a wheel is ONE.** The first version wrote
        parameters for three axes on the assumption that a spare axis is harmless. It is not: force feedback
        worked before spring and damper were added and was completely silent afterwards, because writing
        condition parameters for axes that do not exist takes the whole device down. `SDL_HapticNumAxes`
        reports 1 for this G29, and asking is free."""
        h = self.haptic
        e = h.SDL_HapticEffect()
        e.type = kind
        e.condition.type = kind
        e.condition.direction = h.SDL_HapticDirection(h.SDL_HAPTIC_CARTESIAN, (c_long * 3)(1, 0, 0))
        e.condition.length = 0xFFFFFFFF
        sat = int(max(0.0, min(1.0, strength)) * 32767)
        for axis in range(self.axes):
            e.condition.right_sat[axis] = sat
            e.condition.left_sat[axis] = sat
            e.condition.right_coeff[axis] = sat
            e.condition.left_coeff[axis] = sat
            e.condition.deadband[axis] = 0
            e.condition.center[axis] = 0
        return e

    def set_condition(self, kind, strength):
        """Move a spring or damper. Called when a slider moves, never per frame -- the whole point is that
        the device does this work itself."""
        entry = self.conditions.get(kind)
        if not entry or self.haptic is None or self.dev is None:
            return False
        eff, eid = entry
        sat = int(max(0.0, min(1.0, strength)) * 32767)
        for axis in range(self.axes):
            eff.condition.right_sat[axis] = sat
            eff.condition.left_sat[axis] = sat
            eff.condition.right_coeff[axis] = sat
            eff.condition.left_coeff[axis] = sat
        try:
            self.haptic.SDL_HapticUpdateEffect(self.dev, eid, eff)
            return True
        except Exception:                                       # noqa: BLE001
            return False

    def _constant_effect(self, c_long, level):
        h = self.haptic
        e = h.SDL_HapticEffect()
        e.type = h.SDL_HAPTIC_CONSTANT
        e.constant.type = h.SDL_HAPTIC_CONSTANT
        e.constant.direction = h.SDL_HapticDirection(h.SDL_HAPTIC_CARTESIAN, (c_long * 3)(1, 0, 0))
        e.constant.length = 0xFFFFFFFF
        e.constant.level = int(level)
        return e

    # ---------------------------------------------------------------- output
    def write(self, texture, torque=0.0):
        """`texture` 0..1 of buzz; `torque` -1..1 of centring force, sign carrying which way to push.

        Both are clamped here as well as by the caller. The redundant `min()` costs nothing and this is the
        last place a mistake is still cheap."""
        if self.mode == "none" or self.sdl is None:
            return
        texture = clamp(self.device_class, texture)
        ceiling = CEILING.get(self.device_class, CEILING["unknown"])
        torque = max(-ceiling, min(ceiling, torque if torque == torque else 0.0))
        try:
            if self.sine_id is not None and abs(texture - self.last_texture) >= 0.005:
                self.last_texture = texture
                self.sine.periodic.magnitude = int(texture * 32767)
                self.haptic.SDL_HapticUpdateEffect(self.dev, self.sine_id, self.sine)
            if self.const_id is not None and (self.last_torque is None
                                              or abs(torque - self.last_torque) >= 0.005):
                self.last_torque = torque
                self.const.constant.level = int(torque * 32767)
                self.haptic.SDL_HapticUpdateEffect(self.dev, self.const_id, self.const)
        except Exception:                                       # noqa: BLE001 - never let output kill the loop
            self.stop()

    def stop(self):
        """Silence. **Zero, then stop** -- an infinite effect that is merely 'stopped' has been observed to
        keep playing, so the magnitude is set to zero first and the effect is halted second."""
        self.last_texture, self.last_torque = -1.0, None
        if self.haptic is None or self.dev is None:
            return
        try:
            if self.sine_id is not None:
                self.sine.periodic.magnitude = 0
                self.haptic.SDL_HapticUpdateEffect(self.dev, self.sine_id, self.sine)
                self.haptic.SDL_HapticStopEffect(self.dev, self.sine_id)
            if self.const_id is not None:
                self.const.constant.level = 0
                self.haptic.SDL_HapticUpdateEffect(self.dev, self.const_id, self.const)
                self.haptic.SDL_HapticStopEffect(self.dev, self.const_id)
            for _, eid in self.conditions.values():
                self.haptic.SDL_HapticStopEffect(self.dev, eid)
            self.haptic.SDL_HapticStopAll(self.dev)
        except Exception:                                       # noqa: BLE001
            pass

    def close(self):
        """**Destroy, do not merely close.** The first version called `stop()` and `SDL_HapticClose`, the
        process exited, and `SDL_HapticNumEffectsPlaying` still reported 1 -- the wheel stayed pulled to one
        side, powered, resisting return to centre. An effect outlives the process that created it unless it
        is destroyed. Registered with `atexit`, so a crash cannot skip this."""
        self.stop()
        try:
            ids = [self.sine_id, self.const_id] + [eid for _, eid in self.conditions.values()]
            for eid in ids:
                if eid is not None:
                    self.haptic.SDL_HapticDestroyEffect(self.dev, eid)
            if self.haptic and self.dev:
                self.haptic.SDL_HapticStopAll(self.dev)
                self.haptic.SDL_HapticClose(self.dev)
        except Exception:                                       # noqa: BLE001
            pass
        self.sine_id = self.const_id = None
        self.conditions = {}
        self.dev, self.mode = None, "none"


class Mixer:
    """Telemetry in, a would-be magnitude out. Every channel is optional and ABSENCE IS NOT ZERO: a missing
    channel contributes nothing and leaves the previous level to decay through the slew limiter, rather than
    asserting that the car is idling, straight and gripping."""

    def __init__(self, device_class: str, rung: float, armed: bool = False):
        self.device_class = device_class
        self.rung = rung
        self.level = 0.0
        self.torque = 0.0
        self.last = time.monotonic()
        self.frames = 0
        self.idles = 0
        self.device_name = None
        self.caps = {}
        # **Every one of these is a slider, not a constant.** The owner should never need an editor to change
        # how their wheel feels, and one person's preference is not a fact about the hardware.
        self.tune = {
            "spring": 0.55,     # self-centring weight. The cure for "floaty".
            "damper": 0.35,     # resistance to being whipped around. Stiffness.
            "texture": 0.60,    # road and slip rumble
            "engine": 0.45,     # engine hum, felt at idle as well as at speed
            "road": 1.00,       # suspension movement
        }
        self.out = None                 # set by main() only when --arm was given
        self.rig = None                 # set by main(); the mixer gates it on seat state
        # **Force output is opt-in and off.** Everything else in this file runs identically either way, so the
        # difference between watching telemetry and moving a wheel is one flag rather than a code path nobody
        # exercised until the day it mattered.
        self.armed = armed

    def set_rung(self, index) -> float:
        """Move to a rung BY INDEX on this class's ladder. There is deliberately no way to set an arbitrary
        magnitude: the ladder is the safety property, and a free value would be a slider with extra steps."""
        rungs = ladder(self.device_class)
        if not rungs:
            return self.rung
        try:
            i = int(index)
        except (TypeError, ValueError):
            return self.rung
        i = max(1, min(i, len(rungs)))
        self.rung = rungs[i - 1]
        return self.rung

    def describe(self) -> dict:
        rungs = ladder(self.device_class)
        try:
            idx = rungs.index(self.rung) + 1
        except ValueError:
            idx = 1
        out = {
            "name": self.device_name or "no haptic device",
            "class": self.device_class,
            "rung": idx, "rungs": rungs, "armed": self.armed, "haptics": self.caps,
        }
        out["tune"] = dict(self.tune)
        out["caps"] = dict(self.caps)
        if self.rig is not None:
            out["rig"] = self.rig.describe()
        return out

    def feed(self, f: dict) -> float:
        now = time.monotonic()
        dt, self.last = now - self.last, now

        if f.get("idle"):
            # An `idle` frame means NCM has a driver who is NOT in a car. That is also the on-foot signal for
            # the rig: a wheel must not nudge the walk axis while someone is walking.
            if self.rig is not None:
                self.rig.seated = False
            self.idles += 1
            self.level = slew(self.level, 0.0, dt)
            self.torque = slew(self.torque, 0.0, dt)
            self._emit()
            return self.level

        # A real frame means a driver in a car, so the rig is live again.
        if self.rig is not None:
            self.rig.seated = True
        self.frames += 1

        # **Rebalanced after the first drive felt like nothing.** Engine RPM used to dominate, and at half
        # redline that is a fifth of the scale -- so a car being driven hard produced a whisper. Engine is
        # now a floor of presence rather than the signal, and what the hands should actually notice is the
        # road: slip and suspension movement, both of which swing far harder while cornering and braking.
        contributions = []
        num = lambda key: f[key] if isinstance(f.get(key), (int, float)) else None
        engine = num("engine")
        if engine is not None:
            # **A car should hum at idle.** Engine contribution used to scale straight from the RPM ratio, so
            # an idling engine at 15% of redline produced almost nothing. A floor means the machine is always
            # perceptibly running, and the rest still rises with revs.
            hum = 0.25 + 0.75 * engine
            contributions.append(1.2 * self.tune["engine"] * hum)
        for key, weight in (("slipTotal", 2.5), ("slipLong", 1.5), ("slipLat", 2.0)):
            v = num(key)
            if v is not None:
                contributions.append(weight * self.tune["texture"] * min(1.0, abs(v)))
        for key in ("suspLong", "suspLat"):
            v = num(key)
            if v is not None:
                contributions.append(1.2 * self.tune["road"] * min(1.0, abs(v)))
        if not contributions:
            self.level = slew(self.level, 0.0, dt)
            self._emit()
            return self.level

        # The rung IS the scale. Channel weights sum to roughly 0..1.8, so the lowest rung (0.05) puts a
        # fully-loaded car at about a twentieth of the device maximum -- which is where tuning starts.
        intensity = min(1.0, sum(contributions))
        target = clamp(self.device_class, intensity * self.rung)
        self.level = slew(self.level, target, dt)

        # **Self-aligning torque: the thing that makes a wheel feel like a wheel.** Magnitude grows with how
        # far the wheel is turned and how fast the car is going, and the sign always points BACK TOWARD
        # CENTRE -- it assists, the way a real car's castor does, and never fights the driver toward lock.
        steer = num("steering")
        speed = num("speed") or 0.0
        if steer is None:
            self.torque = slew(self.torque, 0.0, dt)
        else:
            loading = min(1.0, abs(speed) / 20.0)               # full effect by roughly 70 km/h
            want = -(1.0 if steer > 0 else -1.0) * min(1.0, abs(steer)) * loading * self.rung
            self.torque = slew(self.torque, want, dt)
        self._emit()
        return self.level

    def _emit(self) -> None:
        """The single point where a level becomes force. Nothing else in this class touches the device, so
        'is force being applied?' has exactly one answer to check."""
        if self.out is not None:
            self.out.write(self.level, self.torque)

    def silence(self) -> None:
        self.level = 0.0
        self.torque = 0.0
        if self.out is not None:
            self.out.stop()

    def pulse(self) -> None:
        """A test pulse is a RAMP UP AND BACK DOWN, never a step.

        The slew limiter exists precisely so nothing arrives as a hammer blow, and a 'test' that bypassed it
        would be testing something we never ship. So this walks the same limiter the telemetry path uses, at
        the current rung, and returns to silence."""
        if self.out is None:
            return
        step = 1.0 / 60.0
        level = 0.0
        for target in (self.rung, 0.0):
            for _ in range(90):
                level = slew(level, target, step)
                # Texture and a centring push together, so the pulse feels like what driving will feel like
                # rather than like a buzzer.
                self.out.write(level, -level)
                time.sleep(step)
                if abs(level - target) < 0.002:
                    break
        self.out.stop()


def serve(conn: socket.socket, addr, mixer: Mixer, port: int) -> None:
    request = b""
    while CRLF.encode() * 2 not in request:
        chunk = conn.recv(4096)
        if not chunk:
            return
        request += chunk
    lines = request.decode(errors="replace").split(CRLF)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

    if not headers.get("sec-websocket-key"):
        body = BRIDGE_HTML.encode()
        head = CRLF.join([
            "HTTP/1.1 200 OK",
            "Content-Type: text/html; charset=utf-8",
            "Content-Length: " + str(len(body)),
            "Cache-Control: no-store",
            "Connection: close",
        ]) + CRLF + CRLF
        conn.sendall(head.encode() + body)
        print("[ffb] served bridge page to %s" % (lines[0],), flush=True)
        return

    conn.sendall((CRLF.join([
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Accept: " + _accept_key(headers["sec-websocket-key"]),
    ]) + CRLF + CRLF).encode())
    STATE["connected"] = True
    print("[ffb] telemetry socket open from %s (origin %s)"
          % (headers.get("user-agent", "?")[:40], headers.get("origin", "?")), flush=True)

    def say(payload: dict) -> None:
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(len(data))
        elif len(data) < (1 << 16):
            header.append(126)
            header += struct.pack(">H", len(data))
        else:
            header.append(127)
            header += struct.pack(">Q", len(data))
        try:
            conn.sendall(bytes(header) + data)
        except OSError:
            pass

    # The panel cannot draw a wheel it has not been told about, and it must not invent one. Announced on
    # connect and again whenever a setting changes, so the tab is correct without polling.
    say({"device": mixer.describe()})

    last_print = 0.0
    while True:
        frame = _read_frame(conn)
        if frame is None:
            break
        opcode, payload = frame
        if opcode == 0x8:
            break
        if opcode == 0x9:
            conn.sendall(b"\x8a\x00")
            continue
        if opcode != 0x1:
            continue
        try:
            f = json.loads(payload.decode(errors="replace"))
        except Exception:
            continue
        cmd = f.get("cmd")
        if cmd == "settings":
            settings = f.get("settings") or {}
            if settings.get("rung") is not None:
                rung = mixer.set_rung(settings["rung"])
                print("[ffb] rung -> %.2f (%s)" % (rung, mixer.device_class), flush=True)
            if settings.get("armed") is not None:
                want = bool(settings["armed"])
                if want and mixer.out is None:
                    out = Output(mixer.device_class)
                    why = out.open()
                    if why:
                        print("[ffb] arm refused: %s" % why, flush=True)
                    else:
                        mixer.out, mixer.armed = out, True
                        if out.name:
                            mixer.device_name, mixer.caps = out.name, out.caps
                        for kind in ("spring", "damper"):
                            out.set_condition(kind, mixer.tune[kind])
                        STATE["armed"], STATE["device"] = True, out.name
                        print("[ffb] ARMED from the panel via %s" % out.mode, flush=True)
                elif not want and mixer.out is not None:
                    mixer.silence()
                    mixer.out.close()
                    mixer.out, mixer.armed = None, False
                    STATE["armed"] = False
                    print("[ffb] disarmed from the panel; wheel released", flush=True)
            for key in ("spring", "damper", "texture", "engine", "road"):
                if settings.get(key) is not None:
                    try:
                        mixer.tune[key] = max(0.0, min(1.0, float(settings[key])))
                    except (TypeError, ValueError):
                        continue
                    if key in ("spring", "damper") and mixer.out is not None:
                        mixer.out.set_condition(key, mixer.tune[key])
                    print("[ffb] %s -> %.2f" % (key, mixer.tune[key]), flush=True)
            if settings.get("steerDegrees") is not None and mixer.rig is not None:
                deg = mixer.rig.set_steer_degrees(settings["steerDegrees"])
                print("[in ] steering -> %.0f deg/side (x%.1f)" % (deg, mixer.rig.scale), flush=True)
            say({"device": mixer.describe()})
            continue
        if cmd == "test":
            # A test pulse is a RAMP, never a step: the slew limiter exists precisely so nothing arrives as a
            # hammer blow, and a "test" that bypassed it would be testing something we never ship.
            print("[ffb] TEST pulse at rung %.2f, ceiling %.2f%s" %
                  (mixer.rung, CEILING.get(mixer.device_class, CEILING["unknown"]),
                   "" if mixer.armed else "  (NOT ARMED -- no force applied)"), flush=True)
            if mixer.armed:
                threading.Thread(target=mixer.pulse, daemon=True).start()
            continue
        if f.get("v") != FRAME_VERSION:
            # A companion that does not recognise the version refuses the stream rather than guessing at it.
            print("[ffb] REFUSED frame version %r (expected %d)" % (f.get("v"), FRAME_VERSION), flush=True)
            continue
        level = mixer.feed(f)
        now = time.monotonic()
        if now - last_print >= 0.5:
            last_print = now
            print("[ffb] %s | thr=%-5s brk=%-5s str=%-6s eng=%-5s slip=%-5s -> magnitude %.3f (cap %.2f)"
                  % ("IDLE " if f.get("idle") else "frame",
                     f.get("throttle"), f.get("brake"), f.get("steering"),
                     None if f.get("engine") is None else round(f["engine"], 2),
                     f.get("slipTotal"), level, CEILING.get(mixer.device_class, CEILING["unknown"])),
                  flush=True)

    # **The game went away. Stop.** A wheel still buzzing after the client closed is the worst outcome
    # this program can produce, and a dropped socket is the likeliest way to reach it.
    mixer.silence()
    STATE["connected"] = False
    print("[ffb] socket closed after %d frame(s), %d idle -- output silenced" % (mixer.frames, mixer.idles),
          flush=True)


# --------------------------------------------------------------------------------------------------
# ViGEmBus: detect it, and offer to install it.
# --------------------------------------------------------------------------------------------------
#
# ViGEmBus is a driver by Nefarius Software Solutions that lets a program present a virtual game controller.
# Cyberpunk has no native wheel support, so the rig has to arrive as a controller -- without this driver the
# wheel and pedals do not drive the car at all.
#
# **This code downloads and runs a third-party installer, so it does so carefully**: nothing happens without
# an explicit click, the exact URL is shown before the download, and the file's Authenticode signature is
# checked and the signer's name displayed before it is executed. A download that is not validly signed is
# refused outright rather than run with a warning.

VIGEM_RELEASES = "https://github.com/nefarius/ViGEmBus/releases"
VIGEM_API = "https://api.github.com/repos/nefarius/ViGEmBus/releases/latest"


def vigem_present():
    """Is the driver installed? Asked of Windows, not inferred from an exception."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["sc", "query", "ViGEmBus"], capture_output=True, text=True, timeout=10)
        if "RUNNING" in out.stdout or "STOPPED" in out.stdout:
            return True
    except Exception:                                           # noqa: BLE001 - detection must never throw
        pass
    # A service query can fail for reasons unrelated to the driver, so fall back to asking the library
    # whether it can actually create a pad -- which is the thing we care about.
    try:
        import vgamepad as vg
        pad = vg.VX360Gamepad()
        del pad
        return True
    except Exception:                                           # noqa: BLE001
        return False


def vigem_latest_installer():
    """(url, filename) of the latest x64 installer, or (None, why)."""
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(VIGEM_API, headers={"User-Agent": "ncm-wheel"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if name.lower().endswith(".exe"):
                return asset.get("browser_download_url"), name
        return None, "no installer in the latest release"
    except Exception as exc:                                    # noqa: BLE001 - offline is an ordinary case
        return None, str(exc)


def verify_signature(path):
    """(ok, description). **A download that is not validly signed is never run.**"""
    try:
        ps = ("$s = Get-AuthenticodeSignature -LiteralPath '%s'; "
              "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject" % path)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if not lines:
            return False, "no signature information"
        status = lines[0]
        subject = lines[1] if len(lines) > 1 else "(unknown signer)"
        cn = subject
        for part in subject.split(","):
            if part.strip().upper().startswith("CN="):
                cn = part.strip()[3:]
                break
        return (status == "Valid"), "%s -- signed by %s" % (status, cn)
    except Exception as exc:                                    # noqa: BLE001
        return False, "could not check the signature: %s" % exc


class VigemDialog(object):
    """Missing-driver consent. Blocks until the user chooses; every choice is legitimate."""

    def __init__(self, parent_tk):
        self.tk = parent_tk
        self.result = None

    def show(self):
        tk = self.tk
        bg, fg, dim = "#0d1117", "#d8e0e8", "#8b98a5"
        win = tk.Toplevel()
        win.title("ViGEmBus is required")
        win.configure(bg=bg)
        win.geometry("560x340")
        win.grab_set()

        tk.Label(win, text="ONE THING IS MISSING", bg=bg, fg="#f0a04b",
                 font=("Consolas", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        body = ("Your wheel and pedals cannot drive the car without ViGEmBus.\n\n"
                "Cyberpunk has no built-in wheel support, so the rig has to be presented to it as a game "
                "controller. ViGEmBus is the free, open-source driver that makes that possible. It is made "
                "by Nefarius Software Solutions and is widely used.\n\n"
                "Force feedback does not need it and will work either way.")
        tk.Label(win, text=body, bg=bg, fg=fg, font=("Consolas", 9), justify="left",
                 wraplength=520, anchor="w").pack(anchor="w", padx=16)
        tk.Label(win, text=VIGEM_RELEASES, bg=bg, fg=dim, font=("Consolas", 8),
                 anchor="w").pack(anchor="w", padx=16, pady=(10, 0))

        self.status = tk.Label(win, text="", bg=bg, fg=dim, font=("Consolas", 8),
                               justify="left", wraplength=520, anchor="w")
        self.status.pack(anchor="w", padx=16, pady=(8, 0))

        bar = tk.Frame(win, bg=bg)
        bar.pack(side="bottom", fill="x", padx=16, pady=14)

        def choose(value):
            self.result = value
            if value == "install":
                self.install(win)
            else:
                win.destroy()

        mk = lambda text, val, accent: tk.Button(
            bar, text=text, command=lambda: choose(val), relief="flat",
            bg=("#1f6feb" if accent else "#1c2530"), fg="#ffffff" if accent else fg,
            font=("Consolas", 9), padx=12, pady=6)
        mk("Download and install it for me", "install", True).pack(side="left")
        mk("Open the page, I'll do it", "open", False).pack(side="left", padx=8)
        mk("Continue without", "skip", False).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", lambda: choose("skip"))
        win.wait_window()
        if self.result == "open":
            import webbrowser
            webbrowser.open(VIGEM_RELEASES)
        return self.result

    def install(self, win):
        """Fetch, verify, then hand to the official installer -- which will raise its own UAC prompt."""
        import tempfile
        import urllib.request

        def say(text):
            self.status.configure(text=text)
            win.update_idletasks()
            print("[vig] " + text, flush=True)

        say("Finding the latest release...")
        url, name = vigem_latest_installer()
        if not url:
            say("Could not reach GitHub (%s). Use the button above to open the page instead." % name)
            return
        say("Downloading %s" % name)
        try:
            target = os.path.join(tempfile.gettempdir(), name)
            req = urllib.request.Request(url, headers={"User-Agent": "ncm-wheel"})
            with urllib.request.urlopen(req, timeout=120) as r, io.open(target, "wb") as fh:
                fh.write(r.read())
        except Exception as exc:                                # noqa: BLE001
            say("Download failed (%s). Use the button above to open the page instead." % exc)
            return

        say("Checking the signature...")
        ok, detail = verify_signature(target)
        if not ok:
            # Refused, not warned. An unsigned or tampered installer is not something to run on a user's
            # machine on our say-so.
            say("REFUSED: %s. Nothing was run. Please download it yourself from the page above." % detail)
            return
        say("%s. Starting the installer -- Windows will ask for permission." % detail)
        try:
            subprocess.Popen([target], shell=False)
            say("Installer started. When it finishes, close and reopen NCM Wheel Support.")
        except Exception as exc:                                # noqa: BLE001
            say("Could not start the installer (%s). It is saved at %s" % (exc, target))


# --------------------------------------------------------------------------------------------------
# The window.
# --------------------------------------------------------------------------------------------------
#
# Tkinter, because it is in the standard library: no extra dependency, nothing else to install, and
# PyInstaller bundles it without special handling. It is a status display and not a control panel -- every
# setting lives in the game, where the driver already is.


class ButtonsDialog(object):
    """Bind each Xbox control by pressing the button you want for it.

    **`home` is offered but marked**, because binding the Guide button to a paddle you brush mid-corner drops
    you out of the game. It is available for anyone who genuinely wants it and is not something to assign by
    accident.
    """

    TARGETS = (
        ("a", "A"), ("b", "B"), ("x", "X"), ("y", "Y"),
        ("lb", "LB"), ("rb", "RB"),
        ("back", "Back / View"), ("start", "Start / Menu"),
        ("dpad_up", "D-pad up"), ("dpad_down", "D-pad down"),
        ("dpad_left", "D-pad left"), ("dpad_right", "D-pad right"),
        ("ls", "Left stick click"), ("rs", "Right stick click"),
        ("home", "Guide  (careful)"),
    )

    def __init__(self, tk, rig, on_apply):
        self.tk, self.rig, self.on_apply = tk, rig, on_apply
        self.learning = None

    def show(self):
        tk = self.tk
        bg, fg, dim = "#0d1117", "#d8e0e8", "#8b98a5"
        current, source = self.rig.button_map()
        self.bindings = dict(current)

        win = tk.Toplevel()
        win.title("Buttons")
        win.configure(bg=bg)
        win.geometry("560x620")
        win.grab_set()

        rim = self.rig.rim
        tk.Label(win, text="BUTTONS", bg=bg, fg=fg,
                 font=("Consolas", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(win, text=("%s — %d buttons.  Current map: %s.\nPress LEARN, then press the button on "
                            "your wheel." % (rim.name if rim else "no button device",
                                             rim.button_count if rim else 0, source)),
                 bg=bg, fg=dim, font=("Consolas", 9), justify="left",
                 anchor="w", wraplength=520).pack(anchor="w", padx=16, pady=(0, 6))

        self.live = tk.Label(win, text="held: none", bg=bg, fg="#5dd39e", font=("Consolas", 9), anchor="w")
        self.live.pack(anchor="w", padx=16, pady=(0, 8))

        canvas = tk.Canvas(win, bg=bg, highlightthickness=0, height=380)
        scroll = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        grid = tk.Frame(canvas, bg=bg)
        grid.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=grid, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="top", fill="both", expand=True, padx=(16, 0))
        scroll.pack(side="top", fill="y", anchor="e")

        self.rows = {}
        for r, (key, label) in enumerate(self.TARGETS):
            tk.Label(grid, text=label, bg=bg, fg=dim, font=("Consolas", 9),
                     anchor="w", width=18).grid(row=r, column=0, sticky="w", pady=2)
            val = tk.Label(grid, text="-", bg=bg, fg=fg, font=("Consolas", 9), anchor="w", width=12)
            val.grid(row=r, column=1, sticky="w")
            tk.Button(grid, text="Learn", command=lambda k=key: self._learn(k), relief="flat",
                      bg="#1c2530", fg=fg, font=("Consolas", 8), padx=8).grid(row=r, column=2, padx=3)
            tk.Button(grid, text="Clear", command=lambda k=key: self._clear(k), relief="flat",
                      bg="#1c2530", fg=fg, font=("Consolas", 8), padx=8).grid(row=r, column=3)
            self.rows[key] = val

        self.note = tk.Label(win, text="", bg=bg, fg="#f0a04b", font=("Consolas", 9), anchor="w")
        self.note.pack(fill="x", padx=16, pady=(6, 0))

        bar = tk.Frame(win, bg=bg)
        bar.pack(side="bottom", fill="x", padx=16, pady=12)

        def apply():
            data = load_overrides()
            roles = data.get("roles") or {}
            rim_entry = roles.get("rim") or {}
            if rim and not rim_entry.get("name"):
                rim_entry["name"] = rim.name
            rim_entry["buttons"] = {str(i): t for i, t in self.bindings.items()}
            roles["rim"] = rim_entry
            data["roles"] = roles
            if save_overrides(data):
                self.note.configure(text="Saved. Re-reading...")
                win.update_idletasks()
                self.on_apply()
                win.after(700, win.destroy)
            else:
                self.note.configure(text="Could not save.")

        def use_default():
            self.bindings = dict(self.rig.DEFAULT_RIM_BUTTONS)
            self._redraw()
            self.note.configure(text="Default guess restored — not yet saved.")

        tk.Button(bar, text="Save", command=apply, relief="flat", bg="#1f6feb", fg="#ffffff",
                  font=("Consolas", 9), padx=12, pady=6).pack(side="left")
        tk.Button(bar, text="Reset to default guess", command=use_default, relief="flat", bg="#1c2530",
                  fg=fg, font=("Consolas", 9), padx=12, pady=6).pack(side="left", padx=8)
        tk.Button(bar, text="Close", command=win.destroy, relief="flat", bg="#1c2530", fg=fg,
                  font=("Consolas", 9), padx=12, pady=6).pack(side="right")

        self.win = win
        self._redraw()
        self._poll()
        win.wait_window()

    def _redraw(self):
        for key, lbl in self.rows.items():
            idx = next((i for i, t in self.bindings.items() if t == key), None)
            lbl.configure(text=("button %d" % idx) if idx is not None else "-",
                          fg="#d8e0e8" if idx is not None else "#5a6672")

    def _learn(self, key):
        self.learning = key
        self.note.configure(text="Press the button you want for %s..." % key.upper())

    def _clear(self, key):
        for i, t in list(self.bindings.items()):
            if t == key:
                del self.bindings[i]
        self._redraw()
        self.note.configure(text="%s unbound — not yet saved." % key.upper())

    def _poll(self):
        held = self.rig.rim_pressed()
        self.live.configure(text="held: " + (", ".join(str(h) for h in held) if held else "none"))
        if self.learning and held:
            index = held[0]
            # One button, one target: clear anything else this index was bound to, and anything else bound
            # to this target, or a rim quietly ends up sending two things at once.
            self.bindings = {i: t for i, t in self.bindings.items()
                             if i != index and t != self.learning}
            self.bindings[index] = self.learning
            self.note.configure(text="%s = button %d" % (self.learning.upper(), index))
            self.learning = None
            self._redraw()
        try:
            self.win.after(120, self._poll)
        except Exception:                                       # noqa: BLE001 - window closed
            pass


class DevicesDialog(object):
    """Reassign roles by hand.

    **Detection is a proposal, not a diagnosis.** It reads a wheel by where its axes rest, which works well
    until it meets a load cell that rests mid-travel, a handbrake that looks like a pedal, or a rim that
    enumerates as something else. Rather than grow the heuristic forever, this lets the person who can see
    the hardware say what it is -- and remembers it.
    """

    ROLES = ("steering", "pedals", "rim")

    def __init__(self, tk, rig, on_apply):
        self.tk, self.rig, self.on_apply = tk, rig, on_apply

    def show(self):
        tk = self.tk
        layout = self.rig.layout()
        devices = layout["devices"]
        names = ["(none)"] + [d["name"] for d in devices]
        by_name = {d["name"]: d for d in devices}

        bg, fg, dim = "#0d1117", "#d8e0e8", "#8b98a5"
        win = tk.Toplevel()
        win.title("Devices")
        win.configure(bg=bg)
        win.geometry("640x460")
        win.grab_set()

        tk.Label(win, text="WHICH DEVICE IS WHICH", bg=bg, fg=fg,
                 font=("Consolas", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(win, text=("These are filled in by detection. Change anything it got wrong -- your choice "
                            "is saved and used from now on."),
                 bg=bg, fg=dim, font=("Consolas", 9), justify="left",
                 wraplength=600, anchor="w").pack(anchor="w", padx=16, pady=(0, 10))

        grid = tk.Frame(win, bg=bg)
        grid.pack(fill="x", padx=16)
        self.vars = {}
        row = 0

        def label(text, hint=None):
            nonlocal row
            tk.Label(grid, text=text, bg=bg, fg=dim, font=("Consolas", 9),
                     anchor="w", width=16).grid(row=row, column=0, sticky="w", pady=3)
            if hint:
                tk.Label(grid, text=hint, bg=bg, fg="#5a6672", font=("Consolas", 8),
                         anchor="w").grid(row=row, column=2, sticky="w", padx=(10, 0))

        def device_menu(role, current):
            var = tk.StringVar(value=current or "(none)")
            tk.OptionMenu(grid, var, *names).grid(row=row, column=1, sticky="we", pady=3)
            self.vars[role] = var
            return var

        def axis_menu(key, current, count):
            var = tk.StringVar(value=str(current) if current is not None else "-")
            choices = ["-"] + [str(i) for i in range(max(count, 8))]
            tk.OptionMenu(grid, var, *choices).grid(row=row, column=1, sticky="we", pady=3)
            self.vars[key] = var
            return var

        steer_axes = by_name.get(layout["steering"], {}).get("axes", 8)
        pedal_axes_count = by_name.get(layout["pedals"], {}).get("axes", 8)

        label("STEERING", "the wheel itself")
        device_menu("steering", layout["steering"]); row += 1
        label("  steering axis", "rests at centre")
        axis_menu("steer_axis", layout["steerAxis"], steer_axes); row += 1

        label("PEDALS", "may be the same device")
        device_menu("pedals", layout["pedals"]); row += 1
        for which, hint in (("throttle", "rests at one end"), ("brake", ""), ("clutch", "optional")):
            label("  " + which, hint)
            axis_menu(which, layout["pedalAxes"].get(which), pedal_axes_count); row += 1

        label("BUTTONS", "rim or button box")
        device_menu("rim", layout["rim"]); row += 1

        label("WHEEL TYPE", "sets the force ceiling")
        cls = tk.StringVar(value=layout.get("deviceClass") or "unknown")
        tk.OptionMenu(grid, cls, "gear", "belt", "direct_drive", "unknown").grid(
            row=row, column=1, sticky="we", pady=3)
        self.vars["class"] = cls
        row += 1
        grid.columnconfigure(1, weight=1)

        # What each device looks like, so somebody can tell two identical names apart or spot the one whose
        # axes are all resting at an end.
        detail = "\n".join(
            "%-44s %d axes, %d buttons%s" % (
                d["name"][:44], d["axes"], d["buttons"],
                ("  centred: %s" % d["centred"]) if d["centred"] else "")
            for d in devices) or "no joystick devices found"
        box = tk.Label(win, text=detail, bg="#11161d", fg=dim, font=("Consolas", 8),
                       justify="left", anchor="w")
        box.pack(fill="x", padx=16, pady=12)

        note = tk.Label(win, text="", bg=bg, fg="#f0a04b", font=("Consolas", 9), anchor="w")
        note.pack(fill="x", padx=16)

        bar = tk.Frame(win, bg=bg)
        bar.pack(side="bottom", fill="x", padx=16, pady=14)

        def apply():
            roles = {}
            steer_name = self.vars["steering"].get()
            if steer_name != "(none)":
                roles["steering"] = {"name": steer_name,
                                     "axis": int(self.vars["steer_axis"].get() or 0),
                                     "class": self.vars["class"].get()}
            ped_name = self.vars["pedals"].get()
            if ped_name != "(none)":
                entry = {"name": ped_name}
                for which in ("throttle", "brake", "clutch"):
                    v = self.vars[which].get()
                    entry[which] = int(v) if v not in ("-", "") else -1
                roles["pedals"] = entry
            rim_name = self.vars["rim"].get()
            if rim_name != "(none)":
                roles["rim"] = {"name": rim_name}
            if save_overrides({"roles": roles}):
                note.configure(text="Saved. Re-reading your devices...")
                win.update_idletasks()
                ok, why = self.on_apply()
                note.configure(text="Devices reloaded." if ok else ("Could not reload: %s" % why))
                win.after(900, win.destroy)
            else:
                note.configure(text="Could not save your choices.")

        def reset():
            if save_overrides({}):
                note.configure(text="Cleared. Back to automatic detection...")
                win.update_idletasks()
                self.on_apply()
                win.after(900, win.destroy)

        tk.Button(bar, text="Apply and reload", command=apply, relief="flat", bg="#1f6feb",
                  fg="#ffffff", font=("Consolas", 9), padx=12, pady=6).pack(side="left")
        tk.Button(bar, text="Map buttons...", command=lambda: ButtonsDialog(
            self.tk, self.rig, self.on_apply).show(), relief="flat", bg="#1c2530", fg=fg,
            font=("Consolas", 9), padx=12, pady=6).pack(side="left", padx=8)
        tk.Button(bar, text="Use automatic detection", command=reset, relief="flat", bg="#1c2530",
                  fg=fg, font=("Consolas", 9), padx=12, pady=6).pack(side="left", padx=8)
        tk.Button(bar, text="Close", command=win.destroy, relief="flat", bg="#1c2530",
                  fg=fg, font=("Consolas", 9), padx=12, pady=6).pack(side="right")
        win.wait_window()


class StatusWindow(object):
    """A small always-honest readout: what was found, what is wrong, and a button that copies it all."""

    ROWS = (
        ("Companion", "listening"),
        ("Game", "connected"),
        ("Force output", "armed"),
        ("Steering", "wheel"),
        ("Pedals", "pedals"),
        ("Buttons", "rim"),
        ("Controller", "gamepads"),
    )

    def __init__(self, on_close, rig=None, on_reload=None):
        import tkinter as tk
        from tkinter import scrolledtext
        self.tk, self.on_close = tk, on_close
        self.rig, self.on_reload = rig, on_reload
        self.root = tk.Tk()
        self.root.title("NCM Wheel Support")
        self.root.geometry("560x470")
        self.root.minsize(460, 380)
        bg, fg, dim = "#0d1117", "#d8e0e8", "#8b98a5"
        self.root.configure(bg=bg)

        head = tk.Frame(self.root, bg=bg)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text="NCM WHEEL SUPPORT", bg=bg, fg=fg,
                 font=("Consolas", 13, "bold")).pack(side="left")
        tk.Label(head, text="v" + STATE["version"], bg=bg, fg=dim,
                 font=("Consolas", 9)).pack(side="left", padx=(8, 0))

        grid = tk.Frame(self.root, bg=bg)
        grid.pack(fill="x", padx=14)
        self.values = {}
        for i, (label, key) in enumerate(self.ROWS):
            tk.Label(grid, text=label.upper(), bg=bg, fg=dim, font=("Consolas", 9),
                     anchor="w", width=13).grid(row=i, column=0, sticky="w", pady=1)
            v = tk.Label(grid, text="-", bg=bg, fg=fg, font=("Consolas", 9), anchor="w",
                         justify="left", wraplength=400)
            v.grid(row=i, column=1, sticky="w", pady=1)
            self.values[key] = v

        # Problems get their own block and their own colour, because they are the only thing here that
        # requires the reader to do something.
        self.problem = tk.Label(self.root, text="", bg=bg, fg="#f0a04b", font=("Consolas", 9),
                                anchor="w", justify="left", wraplength=520)
        self.problem.pack(fill="x", padx=14, pady=(8, 0))

        self.text = scrolledtext.ScrolledText(self.root, height=10, bg="#11161d", fg=dim,
                                              font=("Consolas", 8), relief="flat", wrap="word")
        self.text.pack(fill="both", expand=True, padx=14, pady=10)
        self.text.configure(state="disabled")

        bar = tk.Frame(self.root, bg=bg)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(bar, text="Copy diagnostics", command=self.copy,
                  bg="#1c2530", fg=fg, relief="flat", font=("Consolas", 9),
                  padx=10, pady=4).pack(side="left")
        tk.Button(bar, text="Devices...", command=self._devices,
                  bg="#1c2530", fg=fg, relief="flat", font=("Consolas", 9),
                  padx=10, pady=4).pack(side="left", padx=6)
        self.copied = tk.Label(bar, text="", bg=bg, fg="#5dd39e", font=("Consolas", 9))
        self.copied.pack(side="left", padx=10)
        tk.Label(bar, text="Settings live in the game: F6 -> WHEEL/PEDALS", bg=bg, fg=dim,
                 font=("Consolas", 8)).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._seen = 0
        self._tick()

    # ------------------------------------------------------------------
    def diagnostics(self):
        """Everything someone would otherwise be asked to dig out of a log."""
        lines = ["NCM Wheel Support v%s -- diagnostics" % STATE["version"], ""]
        for label, key in self.ROWS:
            lines.append("%-13s %s" % (label + ":", self._value(key)))
        lines.append("%-13s %s" % ("Wheel class:", STATE.get("device_class") or "-"))
        lines.append("%-13s %s" % ("Strength:", STATE.get("rung") or "-"))
        lines.append("%-13s %s" % ("Frames:", STATE.get("frames")))
        if STATE["problems"]:
            lines += ["", "PROBLEMS:"] + ["  - " + p for p in STATE["problems"]]
        lines += ["", "LOG:"] + list(LOG)
        return "\n".join(lines)

    def copy(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.diagnostics())
            self.copied.configure(text="copied -- paste it to whoever is helping")
            self.root.after(4000, lambda: self.copied.configure(text=""))
        except Exception:                                       # noqa: BLE001
            self.copied.configure(text="could not reach the clipboard")

    def _devices(self):
        if self.rig is None:
            self.copied.configure(text="no input system to configure")
            return
        DevicesDialog(self.tk, self.rig, self.on_reload).show()

    def _value(self, key):
        if key == "listening":
            return ("listening on port %s" % STATE["port"]) if STATE["listening"] else "not started"
        if key == "connected":
            return "connected" if STATE["connected"] else "waiting for the game"
        if key == "armed":
            if not STATE["armed"]:
                return "off -- arm it in game (F6 -> WHEEL/PEDALS)"
            return "ON  -- %s" % (STATE.get("device") or "wheel")
        if key == "gamepads":
            return ", ".join(STATE["gamepads"]) if STATE["gamepads"] else "none"
        return STATE.get(key) or "not found"

    def _tick(self):
        for _, key in self.ROWS:
            text = self._value(key)
            if self.values[key].cget("text") != text:
                self.values[key].configure(text=text)
        self.problem.configure(
            text=("\n".join("! " + p for p in STATE["problems"])) if STATE["problems"] else "")
        if len(LOG) != self._seen:
            self._seen = len(LOG)
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("end", "\n".join(list(LOG)[-200:]))
            self.text.see("end")
            self.text.configure(state="disabled")
        self.root.after(400, self._tick)

    def _close(self):
        try:
            self.on_close()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main() -> int:
    # Before anything prints. A packaged build has no console, so without the tee the startup lines -- the
    # ones that say which devices were found -- would simply vanish.
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=38480)
    ap.add_argument("--class", dest="device_class", default=None, choices=sorted(CEILING),
                    help="override the detected class. Only ever use this to go LOWER than detection.")
    ap.add_argument("--rung", type=int, default=1, help="1-based rung on this class's ladder (default: lowest)")
    ap.add_argument("--no-input", action="store_true",
                    help="force feedback only; do not present a virtual pad or read the rig.")
    ap.add_argument("--steer-degrees", type=float, default=45.0,
                    help="wheel degrees per side mapped to full stick deflection (default 45).")
    ap.add_argument("--wheel-range", type=float, default=900.0,
                    help="the wheel's physical lock-to-lock range in degrees (default 900).")
    ap.add_argument("--log", default=None,
                    help="also append everything to this file. A packaged build has no console.")
    ap.add_argument("--headless", action="store_true",
                    help="no window. The old behaviour; CI and anyone who wants it invisible.")
    ap.add_argument("--selftest", action="store_true",
                    help="ramp the wheel through the ladder and exit. Proves hardware output without NCM.")
    ap.add_argument("--profiles", default=None,
                    help="rig profile JSON. Default: tools/wheel-profiles/default.json")
    ap.add_argument("--monitor", action="store_true",
                    help="print live steering and pedal values. Use it to confirm which axis is which.")
    ap.add_argument("--device", type=int, default=None,
                    help="force a specific SDL haptic index. Default: try every one until one opens.")
    ap.add_argument("--arm", action="store_true",
                    help="permit force output. OFF by default: everything works identically without it.")
    args = ap.parse_args()
    if args.log:
        LOG_FILE["path"] = args.log
        print("[ui ] logging to %s" % args.log, flush=True)

    name, detected, caps = discover()
    device_class = args.device_class or detected
    if args.device_class and args.device_class != detected:
        print("[ffb] class OVERRIDDEN: detected %r, using %r" % (detected, args.device_class), flush=True)
    args.device_class = device_class

    rungs = ladder(args.device_class)
    if not rungs:
        print("no ladder for class %r" % args.device_class)
        return 2
    idx = max(1, min(args.rung, len(rungs)))
    rung = rungs[idx - 1]

    STATE["device_class"] = args.device_class
    print("[ffb] device class : %s" % args.device_class, flush=True)
    print("[ffb] ceiling      : %.2f of device maximum" % CEILING[args.device_class], flush=True)
    print("[ffb] ladder       : %s" % ", ".join("%.2f" % r for r in rungs), flush=True)
    STATE["rung"] = "%.2f (rung %d of %d)" % (rung, idx, len(rungs))
    print("[ffb] starting rung: %d of %d (%.2f)  <-- start at 1 and stop when a rung adds nothing"
          % (idx, len(rungs), rung), flush=True)
    if args.device_class in ("unknown", "direct_drive"):
        print("[ffb] NOTE: this class is untested by the authors. The ceiling is deliberately low and is a"
              " starting point for testing, not a tuned value.", flush=True)

    # **Before touching input**, so a missing driver arrives as an offer of help rather than as a failure
    # the user has to interpret. Headless skips it: there is nobody to ask.
    if not args.no_input and not args.headless and not args.selftest and not vigem_present():
        print("[vig] ViGEmBus not detected", flush=True)
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            choice = VigemDialog(tk).show()
            root.destroy()
            print("[vig] user chose: %s" % choice, flush=True)
            if choice == "install":
                # The installer runs on its own schedule and needs a restart to take effect, so there is
                # nothing useful to wait for here.
                problem("Finish the ViGEmBus installer, then close and reopen NCM Wheel Support.")
        except Exception as exc:                                # noqa: BLE001 - no Tk, no dialog, carry on
            print("[vig] could not show the dialog (%s)" % exc, flush=True)

    # Input first, and independent of everything else: steering and pedals must work whether or not NCM is
    # running, whether or not force feedback armed, and whether or not the telemetry bridge ever connects.
    rig = None
    if not args.no_input:
        rig = Input(steer_degrees=args.steer_degrees, wheel_range=args.wheel_range,
                    profile_path=args.profiles)
        why_in = rig.open()
        if why_in:
            problem(why_in)
            print("[in ] Force feedback is unaffected; steering and pedals are not available.", flush=True)
            rig = None
        else:
            rig.start()
            STATE["wheel"] = rig.steering.name if rig.steering else None
            STATE["pedals"] = rig.pedals.name if rig.pedals else None
            STATE["rim"] = rig.rim.name if rig.rim else None
            STATE["gamepads"] = [n for n, _ in rig.controllers]
            if rig.steering is None:
                problem("No steering device found. Is the wheel plugged in and powered?")
            if rig.pedals is None:
                problem("No pedals found. Steering will work; throttle and brake will not.")

    mixer = Mixer(args.device_class, rung, armed=args.arm)
    mixer.rig = rig

    if args.selftest:
        out = Output(args.device_class)
        out.index = args.device
        why = out.open()
        if why:
            print("[ffb] SELFTEST cannot arm: %s" % why, flush=True)
            return 2
        print("[ffb] selftest via %s effect. Ceiling %.2f." % (out.mode, CEILING[args.device_class]),
              flush=True)
        try:
            for step in ladder(args.device_class):
                print("[ffb]   texture %.2f + centring torque, 2s" % step, flush=True)
                out.write(step, -step)
                time.sleep(2.0)
            out.write(0.0, 0.0)
        finally:
            # The first version left the wheel powered and pulled to one side. Destroy, do not merely close.
            out.close()
        print("[ffb] selftest done; wheel released", flush=True)
        if rig is not None:
            rig.stop()
        return 0
    mixer.device_name, mixer.caps = name, caps
    if args.arm:
        out = Output(args.device_class)
        out.index = args.device
        why = out.open()
        if why:
            problem("Force feedback could not start: %s" % why)
            if "Resetting device" in why or "HapticOpen" in why:
                # Observed 2026-09-24: SDL can NAME the G29 but cannot open its haptics while Cyberpunk is
                # already running. DirectInput force feedback is an exclusive acquisition and the first
                # process to take it keeps it, so the order of startup decides who gets the wheel.
                print("[ffb] SDL can see the device but cannot acquire it. Force feedback is an EXCLUSIVE",
                      flush=True)
                print("[ffb] acquisition -- whichever process takes it first keeps it. Start this companion",
                      flush=True)
                print("[ffb] BEFORE the game (build-ffb-companion.ps1 -InstallStartup does that for you).",
                      flush=True)
                print("[ffb] Also check that Logitech G HUB is running, which Logitech wheels need for SDL",
                      flush=True)
                print("[ffb] haptics to be exposed at all.", flush=True)
            print("[ffb] continuing with telemetry only.", flush=True)
            mixer.armed = False
        else:
            mixer.out = out
            for kind in ("spring", "damper"):
                out.set_condition(kind, mixer.tune[kind])
            # Output has just opened the device, so it knows the name and capabilities first-hand. Use those
            # rather than the earlier open/close probe -- one open, one source of truth.
            if out.name:
                mixer.device_name, mixer.caps = out.name, out.caps
            STATE["armed"] = True
            STATE["device"] = out.name
            print("[ffb] armed via %s effect" % out.mode, flush=True)
    print("[ffb] device       : %s" % (name or "none detected"), flush=True)
    print("[ffb] force output : %s" % ("ARMED" if args.arm else "disarmed (telemetry only)"), flush=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(4)
    print("[ffb] listening on http://127.0.0.1:%d/bridge.html  --  now run /ncm.ffb on in game"
          % args.port, flush=True)
    if args.monitor and rig is not None:
        def watch():
            while True:
                time.sleep(0.25)
                d = rig.last
                if d:
                    print("[in ] raw=%+.3f -> steer=%+.3f | throttle=%.2f | brake=%.2f"
                          % (d.get("raw", 0), d.get("steer", 0), d.get("throttle", 0), d.get("brake", 0)),
                          flush=True)
        threading.Thread(target=watch, daemon=True).start()

    STATE["port"], STATE["listening"] = args.port, True

    def shutdown():
        # Unconditional. Ctrl-C, a closed window, an unhandled exception -- every one of them ends with the
        # wheel quiet, because the alternative is a device left oscillating by a process that no longer runs.
        mixer.silence()
        if mixer.out is not None:
            mixer.out.close()
        if rig is not None:
            rig.stop()
        try:
            srv.close()
        except Exception:                                       # noqa: BLE001
            pass

    def accept_loop():
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                return                                          # socket closed on the way out
            threading.Thread(target=serve, args=(conn, addr, mixer, args.port), daemon=True).start()

    if args.headless:
        try:
            accept_loop()
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    # The window owns the main thread; the server runs behind it. Closing the window shuts everything down
    # through the same path as Ctrl-C, so there is one teardown rather than two.
    def reload_input():
        """Re-read the rig after the user changed which device is which.

        Stop before re-opening: the devices are held by the running instance, and a second claim on the same
        wheel is exactly the failure that looks like 'detection is broken'."""
        if rig is None:
            return False, "input is not running"
        try:
            rig.stop()
            time.sleep(0.4)
            rig.overrides = load_overrides()
            why = rig.open()
            if why:
                problem(why)
                return False, why
            rig.start()
            STATE["wheel"] = rig.steering.name if rig.steering else None
            STATE["pedals"] = rig.pedals.name if rig.pedals else None
            STATE["rim"] = rig.rim.name if rig.rim else None
            STATE["gamepads"] = [n for n, _ in rig.controllers]
            STATE["problems"] = []
            return True, ""
        except Exception as exc:                                # noqa: BLE001
            return False, str(exc)

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        window = StatusWindow(shutdown, rig=rig, on_reload=reload_input)
    except Exception as exc:                                    # noqa: BLE001 - no display, no Tk, no matter
        print("[ui ] no window (%s); running headless" % exc, flush=True)
        try:
            accept_loop()
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    def refresh_frames():
        STATE["frames"] = mixer.frames
        window.root.after(1000, refresh_frames)

    window.root.after(1000, refresh_frames)
    window.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
