"""Throughput/latency benchmark mirroring Singh et al. (2020) Figs. 9-10.

Batches of 1, 100, 500, 750, 1000 reportSuspicion transactions with 1, N/2 and N miners (N = 4 nodes),
each repeated REPEATS times. Execution time = first submission -> last transaction confirmed in a block;
throughput = transactions / execution time (same definitions as the paper, Sec. 7).

    python benchmark.py [repeats]      -> results/benchmark.csv  (then: python plot_results.py)
"""
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from network import load
from trust_contract import TrustContract

BATCHES = [1, 100, 500, 750, 1000]
MINER_SETS = {1: ["ta"], 2: ["ta", "rsu1"], 4: ["ta", "rsu1", "rsu2", "rsu3"]}
SESSION_SIZE = 50
WORKERS = 4  # parallel submitting clients (MultiChain's default rpcthreads is 4)
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
OUT = Path(__file__).resolve().parent / "results" / "benchmark.csv"

state, nodes = load()
c = TrustContract(state, nodes)
ta = nodes["ta"]
vehicles = [v for v in sorted(c.addr) if c.status(v) == "active"]
m = len(vehicles)


def set_miners(names):
    addr = {n: state["nodes"][n]["address"] for n in nodes}
    txids = [ta.grant(addr[n], "mine") if n in names else ta.revoke(addr[n], "mine") for n in nodes if n != "ta"]
    ta.wait_confirmed(txids)
    ta.wait_blocks(3)
    active = {p["address"] for p in ta.listpermissions("mine")}
    assert active == {addr[n] for n in names}, active


def report(tag, k):
    j = k % SESSION_SIZE  # unique (reporter, suspect) pairs inside each 50-report session
    reporter, suspect = vehicles[j % m], vehicles[(j % m + 1 + j // m) % m]
    return c.report_suspicion(reporter, f"bench-{tag}-{k // SESSION_SIZE}", suspect)


def batch(tag, n):
    """Submit n reports asynchronously (WORKERS parallel clients, like the paper's async web3 sender)."""
    start_height, t1 = ta.getblockcount(), time.perf_counter()
    with ThreadPoolExecutor(WORKERS) as pool:
        txids = list(pool.map(lambda k: report(tag, k), range(n)))
    submitted = time.perf_counter() - t1
    ta.wait_confirmed(txids, timeout=600, since_height=start_height)
    return submitted, time.perf_counter() - t1


OUT.parent.mkdir(exist_ok=True)
run_id = time.strftime("%H%M%S")
with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["miners", "batch", "run", "submit_s", "exec_s", "tps"])
    for miners, names in MINER_SETS.items():
        set_miners(names)
        for n in BATCHES:
            for r in range(REPEATS):
                sub, ex = batch(f"{run_id}-{miners}m-{n}-{r}", n)
                w.writerow([miners, n, r, round(sub, 3), round(ex, 3), round(n / ex, 3)])
                f.flush()
                print(f"miners={miners} batch={n:5} run={r} submit={sub:7.2f}s exec={ex:7.2f}s tps={n / ex:7.2f}")
set_miners(MINER_SETS[4])
print("saved", OUT)
