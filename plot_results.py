"""Plot results/benchmark.csv against the paper's Ethereum numbers (Singh et al. 2020, Figs. 9-10)."""
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RES = Path(__file__).resolve().parent / "results"
BATCHES = [1, 100, 500, 750, 1000]
# values read from the paper's Fig. 9 (execution time, s) and Fig. 10 (throughput, tx/s); key = miners (N = 4)
PAPER_EXEC = {1: [1.5, 17.24, 44.28, 64.42, 80.05], 2: [1.63, 19.29, 47.608, 66.341, 86.44],
              4: [1.57, 15.88, 59.65, 64.42, 87.21]}
PAPER_TPS = {1: [0.66, 5.8, 11.29, 10.75, 12.49], 2: [0.6134, 5.18, 10.5, 11.3, 11.568],
             4: [0.636, 6.29, 8.33, 11.64, 11.466]}
COLORS = {1: "#2a6fdb", 2: "#e07b00", 4: "#2e9e5b"}

rows = defaultdict(list)
with open(RES / "benchmark.csv") as f:
    for r in csv.DictReader(f):
        rows[int(r["miners"]), int(r["batch"])].append((float(r["exec_s"]), float(r["tps"]), float(r["exec_s"]) - float(r["submit_s"])))
ours = {m: ([mean(x[0] for x in rows[m, b]) for b in BATCHES], [mean(x[1] for x in rows[m, b]) for b in BATCHES])
        for m in (1, 2, 4)}

for idx, (title, ylabel, paper, fname, log) in enumerate([
        ("Execution time vs. batch size", "execution time (s)", PAPER_EXEC, "fig_execution_time.png", False),
        ("Throughput vs. batch size", "throughput (tx/s)", PAPER_TPS, "fig_throughput.png", True)]):
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    for m in (1, 2, 4):
        label = {1: "1 miner", 2: "N/2 = 2 miners", 4: "N = 4 miners"}[m]
        ax.plot(BATCHES, ours[m][idx], "o-", color=COLORS[m], label=f"MultiChain, {label}")
        ax.plot(BATCHES, paper[m], "s--", color=COLORS[m], alpha=0.45, label=f"Ethereum PoW (paper), {label}")
    ax.set(title=title, xlabel="number of reportSuspicion transactions", ylabel=ylabel, xticks=BATCHES)
    if log:
        ax.set_yscale("log")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(RES / fname)
    print("saved", RES / fname)

with open(RES / "summary.md", "w") as f:
    f.write("| miners | batch | MultiChain exec (s) | paper exec (s) | MultiChain tps | paper tps "
            "| confirmation lag (s) |\n|---|---|---|---|---|---|---|\n")
    for m in (1, 2, 4):
        for i, b in enumerate(BATCHES):
            lag = mean(x[2] for x in rows[m, b])  # last submission -> last confirmation
            f.write(f"| {m} | {b} | {ours[m][0][i]:.2f} | {PAPER_EXEC[m][i]} | {ours[m][1][i]:.2f} "
                    f"| {PAPER_TPS[m][i]} | {lag:.2f} |\n")
print("saved", RES / "summary.md")
