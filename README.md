# NCM Wheel Support

Racing wheel, pedals and **force feedback** for Cyberpunk 2077 multiplayer on [OPEN//77](https://open2077.net).

Force feedback is driven by **real vehicle telemetry** — throttle, brake, steering, RPM, gear, suspension
travel and measured tyre slip, read from the game's own vehicle state. Not from an FFT of your game audio,
which is how this sort of thing usually has to be done in a game with no native wheel support.

> **Status: alpha.** Force levels were measured on a Logitech G29 (gear drive). The belt and direct-drive
> ceilings are scaled down from that baseline rather than measured on those wheelbases, so they start low on
> purpose. See [Helping test](#helping-test).

---

## What it does

- **Steering, pedals and buttons** — your rig is presented to the game as a standard Xbox 360 controller.
- **Force feedback** — spring, damper, road texture and engine feel, from live telemetry.
- **Feel settings in the game** — strength, centring, stiffness and the mix all live in the NCM panel, on
  sliders, while you are sitting in the car.
- **A status window** that says what it found, whether the game is connected, and what is wrong — with one
  button that copies the lot to your clipboard.
- **Dropdowns for your hardware**, if detection gets it wrong.

## Why there is a companion program at all

Nothing inside the game can reach an HID device. A game resource has no sockets and no device access, in
either direction — so something outside the game has to read your wheel and drive its motors.

That is all this program is: a device shim with a status window. The game enumerates your hardware through
it, decides when the rig is live, tunes the feel, and feeds it telemetry. The window exists to tell you what
is happening and to let you correct what device is what — everything about how the car *feels* is set in
the game, where you are already sitting.

**Telemetry never leaves your machine.** It is scoped to your own occupied vehicle, by construction.

## Install

1. Download `NCM Wheel Support.exe` from [Releases](../../releases), and check it against `SHA256SUMS.txt`.
2. Run it. A small status window opens: it lists the hardware it found, says whether the game has
   connected, and shows anything that went wrong. Minimise it and leave it running.
3. **If ViGEmBus is missing it will offer to install it for you.** That is the driver that lets your wheel
   and pedals drive the car — Cyberpunk has no native wheel support, so the rig has to arrive as a game
   controller. You can let the program fetch it, open the page and do it yourself, or carry on without.
   It shows the download URL first, and refuses to run the installer unless it is validly signed by
   Nefarius Software Solutions. Force feedback does not need it and works either way.
4. Join an NCM server, open the NCM panel (`F6`) and pick **WHEEL/PEDALS**.
5. Press **ARM FORCE OUTPUT**, then start at the **lowest strength** and work up.

**If something is wrong, the window says so** — no wheel found, ViGEmBus missing, game not connected — and
**Copy diagnostics** puts the whole picture on your clipboard to paste to whoever is helping. You should
never have to go looking for a log file.

To have it start with Windows, run `build.ps1 -InstallStartup`, or drop a shortcut to the exe in your
Startup folder yourself. It is an ordinary shortcut, not a service or a registry entry, so you can delete it.
`--headless` runs it with no window if you would rather not see it.

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
newton-metre on a gear-drive G29 and roughly ten on a 20 Nm direct drive base. The same number is a nudge on
one device and a wrist injury on another.

So:

| | |
|---|---|
| **Ceilings are per device class** | as a fraction of *that device's* maximum — gear `0.24`, belt `0.20`, direct drive `0.06`, unidentified `0.04` |
| **An unidentified wheel is assumed to be the most powerful thing it could be** | and gets the lowest cap of all |
| **Strength is a fixed ladder, not a slider** | discrete rungs, so you cannot land somewhere you did not choose |
| **Rate of change is capped too** | a spike is what hurts, not a sustained level |
| **Output is off until you arm it** | and stops on every exit path — idle, disconnect, crash |

The gear-drive numbers are **measured, not guessed**. The first set were reasoned from first principles
and were about 2.5× too high; walking the ladder on a real wheel corrected them. The other classes are scaled
from that measurement, which is why they start below where the measured class ended up — an extrapolation
should be more cautious than the thing it is extrapolated from.

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

### If it gets your hardware wrong

**Press `Devices...` in the window.** Detection is a proposal, not a diagnosis — it reads a rig by where its
axes rest, which works well until it meets a load cell that rests mid-travel, a handbrake that looks like a
pedal, or a rim that enumerates as something else.

Every role is a dropdown: which device steers and on which axis, which device has the pedals and which axis
is throttle, brake and clutch, which device has the buttons, and what type of wheelbase it is. Pick, press
**Apply and reload**, and it re-reads your rig without a restart. Your choice is saved to
`%LOCALAPPDATA%\NCM Wheel Support\devices.json` and used from then on. **Use automatic detection** puts it
back.

Anything you leave as `(none)` stays unassigned rather than being guessed into a role you deliberately left
empty.

### If the buttons are wrong

**Devices... → Map buttons...** Press **Learn** next to a control, then press the button you want for it on
your wheel. The dialog shows which buttons are held as you press them, so you can also just prod things to
see what is what.

Wheels this project has never seen get a default guess at the button layout — face buttons first, then
shoulders, then D-pad — because an unrecognised wheel that cannot press anything is worse than one with a
best guess. It is only a guess, and this is how you correct it. Bindings are saved with your other device
choices.

`Guide` is offered but marked: binding it to a paddle you brush mid-corner drops you out of the game, so it
is deliberately not part of the default guess.

### Adding your wheel to the shipped profiles

Run with `--monitor` to see live axis and button values, then add an entry to `wheel-profiles/default.json`
and open a PR or an issue with it. Pedal assignment in axis order (throttle, brake, clutch) is a convention
rather than a fact, so it is printed at startup — if yours come out swapped, a profile entry fixes it.

## Helping test

**Belt drive and direct drive owners especially.** Those ceilings are extrapolated from the G29
measurement, not taken on that hardware, so the open question is narrow and specific: **is the capped range
useful on your wheel, or is the whole ladder too faint to be worth anything?** Either answer is worth having.

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
| `--headless` | no window |
| `--log FILE` | append everything to a file (a packaged build has no console) |
| `--device N` | force a specific SDL haptic index |
| `--port N` | listen port (default 38480) |

## How it works

The game cannot open a socket, so the direction is inverted: **this program serves a page, and the game loads
it.** OPEN//77 creates a hidden WebUI surface whose entry is `http://127.0.0.1:38480/bridge.html`, which gives
that page a real HTTP origin — so a WebSocket back to the same origin is same-origin, needs no TLS, and still
receives the Lua bridge. Telemetry goes up that socket at 30 Hz; settings come back down it.

## Requirements

- Windows
- **[ViGEmBus](https://github.com/nefarius/ViGEmBus/releases)** — required for wheel and pedal input
- Logitech G HUB, for Logitech wheels
- Python 3.10+ only if running from source

## Builds

Releases are built by GitHub Actions from the tagged source, and every release carries `SHA256SUMS.txt`.
The binary is **not code signed**, so Windows SmartScreen will warn on first run. If you would rather not
trust a binary at all, [build it yourself](#running-from-source-instead) — it is one command.

## License

MIT. See [LICENSE](LICENSE).

## Changelog

### 0.2.0

- **A status window.** It was built to be invisible, which was the wrong trade for something strangers
  install: an invisible program can only report a problem to a log file. It now shows what it found, whether
  the game is connected, and anything that needs acting on — plus **Copy diagnostics**, so nobody is ever
  asked to go and find a log.
- **`Devices...` dropdowns.** Detection reads a rig by where its axes rest, which is a good default and a bad
  guarantee on hardware nobody here has seen. Every role can now be reassigned by hand and is remembered.
- **ViGEmBus is offered, not demanded.** If the driver is missing the program explains what it is and offers
  to fetch it, shows the URL first, and refuses to run the installer unless it is validly signed.
- **Fixed: the packaged build was broken in ways the source was not.** The device profiles were never
  bundled, so a release ran with none of them; `vgamepad`'s ViGEmClient DLL was never bundled, so a release
  could never create a virtual pad at all. Both are bundled now, and the build is smoke-tested in CI.
- **Fixed: a confidently wrong error message.** The above surfaced as *"ViGEmBus is not installed"* on
  machines where it was installed and running. It now checks before blaming, and says plainly when a fault
  is ours.
- **Fixed:** force levels recalibrated after measurement on a real wheel — ceilings came down by roughly 60%,
  and the ladder starts lower and steps finer. The G29 is gear drive, not belt, and is now labelled correctly.
- `--log FILE` and `--headless`.

### 0.1.0

First release. Wheel, pedals and telemetry-driven force feedback.
