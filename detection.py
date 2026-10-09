"""Vehicular-plane misbehaviour detection (local plausibility checks).

Singh et al. (2020, Sec. 8) list as a limitation that they "have not considered the misbehavior detection and
local detection checks using plausibility factors, filters, consistency (position, speed, heading), beacon
frequency". This module adds exactly those checks; their output feeds reportSuspicion (Algorithm 1).

Each vehicle broadcasts a CAM/BSM beacon (id, time, position, speed) every second on a 1-D highway.
A receiver flags the sender when a beacon fails any check:
    speed      claimed speed outside [0, V_MAX]
    range      claimed position farther than radio range from the receiver (it could not have been heard)
    position   claimed position disagrees with previous position + speed * dt by more than POS_TOL
    frequency  beacons arrive faster than the 10 Hz CAM maximum
"""
import random
from dataclasses import dataclass

V_MAX = 55.6       # m/s (200 km/h)
RADIO_RANGE = 300  # m, DSRC
POS_TOL = 10       # m, GPS noise + rounding
MIN_INTERVAL = 0.1 # s, CAM max 10 Hz

# behaviour of each simulated vehicle
HONEST, FAKE_POSITION, FAKE_SPEED, FLOODING = "honest", "fake_position", "fake_speed", "flooding"


@dataclass
class Beacon:
    sender: str
    t: float
    x: float
    v: float


def check(receiver_x, prev, b):
    """Return the list of failed plausibility checks for beacon `b` (prev = sender's previous beacon)."""
    failed = []
    if not 0 <= b.v <= V_MAX:
        failed.append("speed")
    if abs(b.x - receiver_x) > RADIO_RANGE:
        failed.append("range")
    if prev:
        dt = b.t - prev.t
        if dt < MIN_INTERVAL:
            failed.append("frequency")
        elif abs(b.x - (prev.x + prev.v * dt)) > POS_TOL:
            failed.append("position")
    return failed


def simulate_event(behaviour, seconds=5, seed=0):
    """Simulate one road event. behaviour: {vehicle: HONEST|FAKE_POSITION|FAKE_SPEED|FLOODING}.
    Returns {reporter: {suspect: [failed checks]}} as seen by every honest vehicle."""
    rng = random.Random(seed)
    names = sorted(behaviour)
    x0 = {n: 20.0 * i for i, n in enumerate(names)}     # 20 m spacing, all within radio range
    speed = {n: rng.uniform(22, 30) for n in names}     # true speeds, m/s

    def true_x(n, t):
        return x0[n] + speed[n] * t

    beacons = []
    for n in names:
        times = [k * 0.05 for k in range(int(seconds / 0.05))] if behaviour[n] == FLOODING else range(seconds)
        for t in times:
            x, v = true_x(n, t) + rng.uniform(-2, 2), speed[n]
            if behaviour[n] == FAKE_POSITION and t >= 2:
                x += 400                                 # ghost vehicle jumps ahead ("road clear" attack)
            if behaviour[n] == FAKE_SPEED:
                v = 80.0                                 # claims 288 km/h to fake congestion/emergency
            beacons.append(Beacon(n, t, x, v))
    beacons.sort(key=lambda b: b.t)

    findings = {}
    for receiver in names:
        if behaviour[receiver] != HONEST:
            continue                                     # misbehaving vehicles don't report
        last = {}
        for b in beacons:
            if b.sender == receiver:
                continue
            failed = check(true_x(receiver, b.t), last.get(b.sender), b)
            if failed:
                findings.setdefault(receiver, {}).setdefault(b.sender, set()).update(failed)
            last[b.sender] = b
    return {r: {s: sorted(f) for s, f in sus.items()} for r, sus in findings.items()}


if __name__ == "__main__":  # self-check: every attacker is caught by every honest vehicle, no honest one is flagged
    beh = {f"V{i:02d}": HONEST for i in range(1, 9)}
    beh.update(V09=FAKE_POSITION, V10=FAKE_SPEED, V11=FLOODING)
    found = simulate_event(beh, seed=42)
    attackers = {"V09", "V10", "V11"}
    for reporter, suspects in found.items():
        assert set(suspects) == attackers, (reporter, suspects)
    assert len(found) == 8
    for reporter, suspects in sorted(found.items())[:2]:
        print(reporter, "->", suspects)
    print("detection self-check passed: 8/8 honest vehicles flagged exactly", sorted(attackers))
