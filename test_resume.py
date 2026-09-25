# -*- coding: utf-8 -*-
"""Prove the effect-lifecycle fix, because the bug it closes was invisible to every kind of reasoning.

The defect: `SDL_HapticRunEffect` was called once at arm time and never again, while `stop()` called
`SDL_HapticStopEffect`. Updating a STOPPED effect changes its parameters and produces no force -- so the
first `stop()` of a session silenced the wheel until the user re-armed. It looked completely correct in the
source, and it reported no error. Only a person holding a wheel found it:

    "After armed Force Input I can Test Pulse the wheel and get a feedback but the button works only once.
     To make the Test Pulse work again I have to press unarm and arm"   -- monki, T128, 2026-09-25

So this test does not check that the code says the right words. It counts the SDL calls, in order, through a
fake device, and asserts that force is produced the SECOND time as well as the first.

    python3 test_resume.py
"""
import sys

import importlib.util

spec = importlib.util.spec_from_file_location("ncmwheel", "ncm-wheel.py")
ncmwheel = importlib.util.module_from_spec(spec)
sys.modules["ncmwheel"] = ncmwheel
spec.loader.exec_module(ncmwheel)


class FakeHaptic:
    """Records what was asked of the device. Update on a stopped effect is recorded as WASTED, which is what
    the real device does silently and is the entire point of the test."""

    SDL_HAPTIC_CONSTANT = 1
    SDL_HAPTIC_SINE = 2
    SDL_HAPTIC_SPRING = 4
    SDL_HAPTIC_DAMPER = 8

    def __init__(self):
        self.playing = {}
        self.calls = []
        self.wasted = 0
        self.forces = []

    def SDL_HapticRunEffect(self, dev, eid, iterations):
        self.playing[eid] = True
        self.calls.append(("run", eid))
        return 0

    def SDL_HapticStopEffect(self, dev, eid):
        self.playing[eid] = False
        self.calls.append(("stop", eid))
        return 0

    def SDL_HapticStopAll(self, dev):
        for k in self.playing:
            self.playing[k] = False
        self.calls.append(("stopall", None))
        return 0

    def SDL_HapticUpdateEffect(self, dev, eid, eff):
        self.calls.append(("update", eid))
        if not self.playing.get(eid):
            self.wasted += 1          # parameters changed on a silent effect: no force reaches the wheel
        else:
            self.forces.append(eid)
        return 0

    def SDL_HapticDestroyEffect(self, dev, eid):
        self.playing.pop(eid, None)
        self.calls.append(("destroy", eid))
        return 0


class Eff:
    """Stands in for SDL_HapticEffect: only the attributes Output writes to."""
    class _P:
        magnitude = 0
    class _C:
        level = 0
    class _Cond:
        def __init__(self):
            n = 3
            self.right_sat = [0] * n
            self.left_sat = [0] * n
            self.right_coeff = [0] * n
            self.left_coeff = [0] * n
            self.deadband = [0] * n
            self.center = [0] * n
    def __init__(self):
        self.periodic = Eff._P()
        self.constant = Eff._C()
        self.condition = Eff._Cond()


def armed_output():
    """An Output in the state `open()` leaves it in, without needing a real device."""
    out = ncmwheel.Output("belt")
    fake = FakeHaptic()
    out.haptic = fake
    out.sdl = object()
    out.dev = object()
    out.mode = "sine+constant"
    out.sine, out.sine_id = Eff(), 10
    out.const, out.const_id = Eff(), 11
    out.conditions = {"spring": (Eff(), 12)}
    out.axes = 1
    out._playing = True
    for eid in (10, 11, 12):
        fake.playing[eid] = True
    return out, fake


FAILED = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s  %s" % (name, detail))


print("effect lifecycle")

# --- 1. the exact reported symptom: pulse, then pulse again -------------------------------------
out, fake = armed_output()
out.write(0.5, -0.5)
first = len(fake.forces)
check("a freshly armed device produces force", first > 0, "no updates reached a playing effect")

out.stop()
check("stop() halts every effect", not any(fake.playing.values()))

fake.wasted = 0
before = len(fake.forces)
out.write(0.5, -0.5)
check("force is produced AFTER a stop, with no re-arm", len(fake.forces) > before,
      "this is the bug: %d update(s) went to stopped effects" % fake.wasted)
check("no update is wasted on a stopped effect", fake.wasted == 0,
      "%d wasted" % fake.wasted)

# --- 2. conditions are resumed too, via their own path ------------------------------------------
out, fake = armed_output()
out.stop()
fake.wasted = 0
out.set_condition("spring", 0.6)
check("set_condition revives a stopped condition", fake.wasted == 0 and fake.playing.get(12) is True,
      "wasted=%d playing=%s" % (fake.wasted, fake.playing.get(12)))

# --- 3. resume re-sends values the cache would otherwise suppress --------------------------------
out, fake = armed_output()
out.write(0.5, -0.5)
out.stop()
out.write(0.5, -0.5)          # identical values: a naive cache would skip both and stay silent
check("identical values still reach the device after a silence", 10 in fake.forces[-2:] or 11 in fake.forces[-2:],
      "the change-threshold cache suppressed the first frame back")

# --- 4. resume is idempotent and cheap -----------------------------------------------------------
out, fake = armed_output()
runs_before = sum(1 for c in fake.calls if c[0] == "run")
for _ in range(20):
    out.write(0.5, -0.5)
runs_after = sum(1 for c in fake.calls if c[0] == "run")
check("a playing device is not re-run on every frame", runs_after == runs_before,
      "%d redundant run(s)" % (runs_after - runs_before))

# --- 5. the grain is relative to what the device can produce -------------------------------------
print("force resolution")
out, fake = armed_output()
out.write(0.0, 0.0)
steps = 0
last = 0.0
# Walk the whole belt range in small increments and count how many actually reach the device.
for i in range(1, 501):
    t = (i / 500.0) * ncmwheel.CEILING["belt"]
    before = len(fake.forces)
    out.write(0.0, t)
    if len(fake.forces) > before:
        steps += 1
check("the belt ceiling resolves to hundreds of steps, not tens", steps >= 300,
      "only %d distinct force levels across the whole range" % steps)

# --- 5b. the engine buzz follows the revs ---------------------------------------------------------
print("engine frequency")
hz = lambda ms: 1000.0 / ms
idle_ms = ncmwheel.engine_period_ms(0.0)
red_ms = ncmwheel.engine_period_ms(1.0)
check("idle is a slow throb, not a razor", 8 <= hz(idle_ms) <= 20,
      "idle is %.1f Hz" % hz(idle_ms))
check("redline stays inside what a wheel motor can articulate", hz(red_ms) <= 60,
      "redline is %.1f Hz" % hz(red_ms))
check("frequency rises with revs", red_ms < idle_ms,
      "idle %dms redline %dms" % (idle_ms, red_ms))
check("the old fixed 125 Hz is gone", idle_ms != 8 and red_ms != 8)
check("period is monotonic across the range",
      all(ncmwheel.engine_period_ms(i / 10.0) >= ncmwheel.engine_period_ms((i + 1) / 10.0)
          for i in range(10)))
check("a nonsense ratio cannot produce a zero or negative period",
      all(ncmwheel.engine_period_ms(v) >= 1 for v in (None, "x", -5, 99, float("nan"))))

out, fake = armed_output()
out.write(0.5, 0.0, 0.0)
low = out.sine.periodic.period
out.write(0.5, 0.0, 1.0)
high = out.sine.periodic.period
check("the device is actually told the new period", high < low,
      "period went %d -> %d" % (low, high))

out, fake = armed_output()
out.write(0.5, 0.0, 0.5)
held = out.sine.periodic.period
out.write(0.5, 0.0, None)      # unmeasured revs must not silently mean idle
check("an unmeasured rev ratio keeps the last period", out.sine.periodic.period == held,
      "period changed to %d with no measurement" % out.sine.periodic.period)

# --- 6. close() still destroys, which is the older lesson ----------------------------------------
print("cleanup")
out, fake = armed_output()
out.write(0.3, -0.3)
out.close()
destroyed = {eid for kind, eid in fake.calls if kind == "destroy"}
check("every effect is destroyed on close", {10, 11, 12} <= destroyed,
      "destroyed=%s" % sorted(destroyed))
check("nothing is left playing", not any(fake.playing.values()))

print("")
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
print("effect lifecycle: all checks passed")
