"""End-to-end scenario on a freshly set-up network (python network.py reset && python network.py setup).

Event E1  V11 broadcasts fake positions, V12 fake speeds, V10 falsely accuses honest V03.
          -> V11, V12 convicted (TV 1 -> 0, soft-blocked 1 minute), V03 not; honest reporters rewarded.
Event E1c 3 colluders accuse V05 -> below the 4-participant minimum, nobody is punished.
Event E2  V11 keeps attacking while blocked -> TV -1 -> hard revocation. V12 is blocked and cannot report.
Then      V12's block expires and it can transact again; V11 stays rejected until the TA reinstates it.
Output is printed and saved to results/scenario_log.txt.
"""
import time
from pathlib import Path

from detection import FAKE_POSITION, FAKE_SPEED, HONEST, simulate_event
from network import load
from trust_contract import ContractError, TrustContract

OUT = Path(__file__).resolve().parent / "results"
OUT.mkdir(exist_ok=True)
LOG = open(OUT / "scenario_log.txt", "w", encoding="utf-8")


def log(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    LOG.write(line + "\n")
    LOG.flush()


state, nodes = load()
c = TrustContract(state, nodes)
ta, rsu1 = nodes["ta"], nodes["rsu1"]
V = sorted(c.addr)
run = time.strftime("%H%M%S")


def names(addrs):
    return [c.name.get(a, a) for a in addrs]


def table():
    log(f"  {'vehicle':8}{'rsu':6}{'TV':>4}  {'status':9}{'CREDIT':>7}")
    for v in V:
        t = c.trust(c.addr[v])
        log(f"  {v:8}{state['vehicles'][v]['node']:6}{t['tv']:>4}  {c.status(v):9}{c.credit(v):>7}")


def submit(session, reports):
    txids = []
    for reporter, suspect, evidence in reports:
        try:
            txids.append(c.report_suspicion(reporter, session, suspect, evidence))
        except ContractError as e:
            log(f"  {reporter} -> {suspect}: refused ({e})")
    for n in (rsu1, ta):
        n.wait_confirmed(txids)
    log(f"  {len(txids)} reportSuspicion transactions confirmed")


def analyse_and_enforce(session):
    verdict, txid = c.analyse_reports("rsu1", session)
    rsu1.wait_confirmed([txid])
    ta.wait_confirmed([txid])
    scores = {c.name[a]: s for a, s in verdict["scores"].items()}
    log(f"  RSU1 analyseReports: count={verdict['count']} scores={scores} "
        f"misbehaving={names(verdict['misbehaving'])} alarmer={c.name.get(verdict['alarmer'])}  tx {txid[:16]}..")
    actions, txids = c.enforce(session)
    ta.wait_confirmed(txids)
    for v, tv, st in actions:
        log(f"  TA enforce: {v} TV={tv} -> {st}")
    return verdict


def claims(session, who):
    txids = []
    for v in who:
        try:
            amount, t = c.claim_reward(v, session)
            txids += t
            log(f"  {v} claimReward: +{amount} CREDIT, TV+1")
        except ContractError as e:
            log(f"  {v} claimReward refused: {e}")
    ta.wait_confirmed(txids)


if any(c.trust(c.addr[v])["event"] != "registration" for v in V):
    raise SystemExit("run on a fresh network: python network.py reset && python network.py setup")

log(f"== IoV adaptive trust on MultiChain - scenario run {run} ==")
log(f"chain={state['chain']} block_time={state['block_time']}s soft_block={c.block_len} blocks TV0={state['tv0']}")
table()

# ---------------- Event E1 ----------------
E1 = f"E1-{run}"
log(f"\n== Event {E1}: local plausibility detection ==")
beh = {v: HONEST for v in V}
beh.update(V11=FAKE_POSITION, V12=FAKE_SPEED)
found = simulate_event(beh, seed=1)
for r in ("V01", "V05"):
    log(f"  {r} detected {found[r]}")
reports = [(r, s, ev) for r in sorted(found) if r != "V10" for s, ev in sorted(found[r].items())]
reports.append(("V10", "V03", ["fabricated"]))            # false accusation
submit(E1, reports)
analyse_and_enforce(E1)
log("  on-chain send permission now:")
for v in ("V11", "V12"):
    p = ta.listpermissions("send", c.addr[v])[0]
    log(f"    {v} send valid from block {p['startblock']} (current {ta.getblockcount()})")
log("  V12 tries to report while blocked:")
try:
    c.report_suspicion("V12", E1, "V01")
except ContractError as e:
    log(f"    contract check: {e}")
log(f"    bypassing the client checks: {c.raw_publish('V12', E1 + '-bypass', 'V01')}")
log("\n  claims for E1:")
claims(E1, [f"V{i:02d}" for i in range(1, 11)])

# ---------------- E1c: collusion below minimum participation ----------------
E1c = f"E1c-{run}"
log(f"\n== Event {E1c}: 3 colluders accuse honest V05 ==")
submit(E1c, [(r, "V05", ["fabricated"]) for r in ("V08", "V09", "V10")])
analyse_and_enforce(E1c)
log("  -> no punishment: fewer than 4 participants (paper's anti-51% rule)")

# ---------------- Event E2 ----------------
E2 = f"E2-{run}"
log(f"\n== Event {E2}: V11 keeps attacking, V12 now honest but still blocked ==")
beh = {v: HONEST for v in V}
beh["V11"] = FAKE_POSITION
found = simulate_event(beh, seed=2)
submit(E2, [(r, s, ev) for r in sorted(found) for s, ev in sorted(found[r].items())])
analyse_and_enforce(E2)
claims(E2, [f"V{i:02d}" for i in range(1, 11)])

# ---------------- block expiry, revocation, reinstatement ----------------
until = c.trust(c.addr["V12"])["blocked_until"]
log(f"\n== waiting for V12's soft block to expire at block {until} ==")
while ta.getblockcount() < until:
    time.sleep(1)
log(f"  height {ta.getblockcount()}: V12 status={c.status('V12')}, "
    f"publish -> {c.raw_publish('V12', E2 + '-after-block', 'V01')[:20]}..")
log(f"  V11 status={c.status('V11')}, publish -> {c.raw_publish('V11', E2 + '-after-block', 'V01')}")
log("\n== TA reinstates V11 (only the TA can) ==")
ta.wait_confirmed(c.reinstate("V11"))
log(f"  V11 status={c.status('V11')} TV={c.trust(c.addr['V11'])['tv']}")

log("\n== final trust state ==")
table()
log(f"\nheight={ta.getblockcount()}  reports={len(ta.liststreamitems('reports', False, 100000))}  "
    f"verdicts={len(ta.liststreamitems('verdicts', False, 1000))}  "
    f"trust updates={len(ta.liststreamitems('trust', False, 100000))}")
