# NCM Wheel Support

Racing wheel, pedals and **force feedback** for Cyberpunk 2077 multiplayer on [OPEN//77](https://open2077.net).

Force feedback is driven by **real vehicle telemetry** — throttle, brake, steering, RPM, gear, suspension
travel and measured tyre slip, read from the game's own vehicle state. Not from an FFT of your game audio,
which is how this sort of thing usually has to be done in a game with no native wheel support.

> **Status: alpha.** Tested on one wheelbase (Logitech G29, gear drive). Belt drive and direct drive are
> untested by the author and are capped conservatively until someone tests them. See
> [Helping test](#helping-test).

---

## What it does

- **Steering, pedals and buttons** — your rig is presented to the game as a standard Xbox 360 controller.
- **Force feedback** — spring, damper, road texture and engine feel, from live telemetry.
- **Settings in the game** — device list, strength, centring, stiffness and mix all live in the NCM panel.
  There is no separate configuration window.

## Why there is a companion program at all

Nothing inside the game can reach an HID device. A game resource has no sockets and no device access, in
either direction — so something outside the game has to read your wheel and drive its motors.

That is all this program is: a device shim. It holds no settings of its own and has no interface. The game
enumerates your hardware through it, decides when the rig is live, tunes the feel, and feeds it telemetry.

**Telemetry never leaves your machine.** It is scoped to your own occupied vehicle, by construction.

## Install

1. Download `NCM Wheel Support.exe` from [Releases](../../releases).
2. Run it. There is no window — that is expected.
3. Join an NCM server, open the NCM panel (`F6`) and pick **WHEEL/PEDALS**.
4. Press **ARM FORCE OUTPUT**, then start at the **lowest strength** and work up.

To have it start with Windows, run `build.ps1 -InstallStartup`, or drop a shortcut to the exe in your
Startup folder yourself. It is an ordinary shortcut, not a service or a registry entry, so you can delete it.

**Logitech wheels need G HUB installed and running** for SDL to see their force feedback at all.

### Running from source instead

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python ncm-wheel.py --arm
```

Build your own exe with `.\build.ps1`.

## Safety

Read this before turning force output up, especially on a direct drive base.

**SDL haptic magnitude is normalised, and normalised is not equal.** A `0.5` constant force is roughly a
newton-metre on a belt-drive G29 and roughly ten on a 20 Nm direct drive base. The same number is a nudge on
one device and a wrist injury on another.

So:

| | |
|---|---|
| **Ceilings are per device class** | as a fraction of *that device's* maximum — gear `0.24`, belt `0.20`, direct drive `0.06`, unidentified `0.04` |
| **An unidentified wheel is assumed to be the most powerful thing it could be** | and gets the lowest cap of all |
| **Strength is a fixed ladder, not a slider** | discrete rungs, so you cannot land somewhere you did not choose |
| **Rate of change is capped too** | a spike is what hurts, not a sustained level |
| **Output is off until you arm it** | and stops on every exit path — idle, disconnect, crash |

Those ceilings are **measured, not guessed**. The first set were reasoned from first principles and were
about 2.5× too high; a real wheel and a person corrected them. That is why untested classes are held low
rather than estimated.

**Method:** start at the lowest rung. Step up only while each step still adds something. **Stop as soon as
one does not.**

## Hardware

Roles are detected by **shape**, not by name, so unknown hardware generally works:

- an axis that rests near **centre** and travels both ways → steering
- axes that rest at an **end** of travel → pedals
- many buttons and no usable axes → button box or rim

A combined wheel-and-pedals unit is handled: one device can hold both roles.

`wheel-profiles/default.json` only supplies what shape cannot tell you — which button on an unlabelled rim is
`A`, or a known wheelbase's drive type. Standard gamepads need no entry at all, since SDL already maps them.

**Your gamepad keeps working.** Wheel and controller both feed the same virtual pad and the larger deflection
wins, so you can keep a controller in your hands for on foot and the wheel in front of you for driving.

### Adding your wheel

Run with `--monitor` to see live axis and button values, then add an entry to `wheel-profiles/default.json`
and open a PR or an issue with it. Pedal assignment in axis order (throttle, brake, clutch) is a convention
rather than a fact, so it is printed at startup — if yours come out swapped, a profile entry fixes it.

## Helping test

**Belt drive and direct drive owners especially.** The author has a G29 only, so those classes are capped low
on purpose and nobody has confirmed whether the numbers are useful or pointless.

What is useful to report:

1. The startup lines — device detection and the resolved axis layout
2. Which rung each effect starts to be noticeable at
3. Which rung it stops improving at
4. Anything that felt unpleasant, and at which rung

Issues and PRs welcome.

## Options

| flag | |
|---|---|
| `--arm` | permit force output. Off by default; the panel can also arm it |
| `--rung N` | start at rung N of the ladder for the detected class |
| `--class NAME` | override detection: `gear`, `belt`, `direct_drive`, `unknown`. **Only use it to go lower** |
| `--monitor` | print live steering and pedal values |
| `--selftest` | walk the ladder and exit, with no game running |
| `--steer-degrees N` | wheel degrees per side mapped to full stick deflection (default 45) |
| `--wheel-range N` | your wheel's lock-to-lock range in degrees (default 900) |
| `--no-input` | force feedback only; do not present a virtual pad |
| `--device N` | force a specific SDL haptic index |
| `--port N` | listen port (default 38480) |

## How it works

The game cannot open a socket, so the direction is inverted: **this program serves a page, and the game loads
it.** OPEN//77 creates a hidden WebUI surface whose entry is `http://127.0.0.1:38480/bridge.html`, which gives
that page a real HTTP origin — so a WebSocket back to the same origin is same-origin, needs no TLS, and still
receives the Lua bridge. Telemetry goes up that socket at 30 Hz; settings come back down it.

## Requirements

- Windows
- [ViGEmBus](https://github.com/nefarius/ViGEmBus) for the virtual controller
- Logitech G HUB, for Logitech wheels
- Python 3.10+ only if running from source
