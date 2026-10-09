"""Build and run the IoV trust network on one machine.

    python network.py setup    create chain, start TA + 3 RSU nodes, permissions, streams, CREDIT asset, 12 vehicles
    python network.py start    start all nodes again (e.g. after a reboot)
    python network.py status   height, miners, peers per node
    python network.py stop     stop all nodes
    python network.py reset    stop and delete everything (nodes/ and network.json)

Roles (Singh et al., 2020 -> MultiChain):
    TA   genesis node: admin, issues CREDIT, writes registry/trust/claims, enforces revocation
    RSU  node with connect,send,receive,mine + verdicts.write; validators of the RSU plane
    Vehicle  address held by its RSU node (OBU proxy) with send,receive + reports.write
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mcrpc import Node, RPCError

ROOT = Path(__file__).resolve().parent
NODES_DIR = ROOT / "nodes"
STATE_FILE = ROOT / "network.json"
BIN = Path(os.environ.get("MULTICHAIN_BIN", ROOT.parent / "multichain"))
CHAIN = "iovchain"
WINDOWS = os.name == "nt"

# name -> (p2p port, rpc port)
PORTS = {"ta": (7447, 7446), "rsu1": (7457, 7456), "rsu2": (7467, 7466), "rsu3": (7477, 7476)}
RSUS = ["rsu1", "rsu2", "rsu3"]
VEHICLES = {f"V{i:02d}": RSUS[(i - 1) // 4] for i in range(1, 13)}  # 4 vehicles per RSU
STREAMS = ["registry", "reports", "verdicts", "trust", "claims"]
TV0 = 1  # initial trust value; paper leaves it unspecified (TA assigns it)
BLOCK_TIME = 2
PARAMS = {
    "chain-description": "IoV adaptive trust (Singh et al. 2020) on MultiChain",
    "target-block-time": BLOCK_TIME,
    "setup-first-blocks": 10,
    "mining-diversity": 0.5,
    "mine-empty-rounds": -1,  # keep producing blocks when idle: soft blocks are measured in block height
    "default-network-port": PORTS["ta"][0],
    "default-rpc-port": PORTS["ta"][1],
}


def exe(name):
    path = BIN / (name + (".exe" if WINDOWS else ""))
    return str(path) if path.exists() else name  # fall back to PATH


def node_args(name, password):
    p2p, rpc = PORTS[name]
    target = CHAIN if name == "ta" else f"{CHAIN}@127.0.0.1:{PORTS['ta'][0]}"
    return [exe("multichaind"), target, f"-datadir={NODES_DIR / name}", f"-port={p2p}", f"-rpcport={rpc}",
            "-rpcuser=multichainrpc", f"-rpcpassword={password}", "-rpcallowip=127.0.0.1"]


def launch(name, password):
    """Start a node in the background and wait until its RPC answers."""
    log = open(NODES_DIR / f"{name}.log", "ab")
    args = node_args(name, password)
    if WINDOWS:  # multichaind stays in the foreground on Windows; detach it
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
    else:
        subprocess.Popen(args + ["-daemon"], stdout=log, stderr=log)
    node = Node(name, CHAIN, PORTS[name][1], "multichainrpc", password)
    for _ in range(120):
        try:
            node.getinfo()
            return node
        except (OSError, RPCError):
            time.sleep(0.5)
    raise RuntimeError(f"{name} did not start; see {NODES_DIR / name}.log")


def first_join(name, password):
    """First run of an RSU node: it creates its wallet, asks for connect permission and exits."""
    out = subprocess.run(node_args(name, password), capture_output=True, text=True, timeout=120)
    m = re.search(r"grant (\w+) connect", out.stdout + out.stderr)
    if not m:
        raise RuntimeError(f"{name}: could not read its address:\n{out.stdout}{out.stderr}")
    return m.group(1)


def load():
    """Return (state, {name: Node}) for an already set-up network."""
    if not STATE_FILE.exists():
        sys.exit("network not set up - run: python network.py setup")
    state = json.loads(STATE_FILE.read_text())
    nodes = {n: Node(n, CHAIN, PORTS[n][1], "multichainrpc", v["password"]) for n, v in state["nodes"].items()}
    return state, nodes


def setup():
    if STATE_FILE.exists():
        sys.exit("already set up - use `python network.py start`, or `reset` to start over")
    for name in PORTS:  # multichain-util / multichaind need the datadir to exist
        (NODES_DIR / name).mkdir(parents=True, exist_ok=True)
    print("[1] creating chain", CHAIN)
    subprocess.run([exe("multichain-util"), "create", CHAIN, f"-datadir={NODES_DIR / 'ta'}"], check=True,
                   capture_output=True)
    params = NODES_DIR / "ta" / CHAIN / "params.dat"
    text = params.read_text()
    for key, val in PARAMS.items():
        text = re.sub(rf"^{re.escape(key)}\s*=.*$", f"{key} = {val}", text, flags=re.M)
    params.write_text(text)

    state = {"chain": CHAIN, "block_time": BLOCK_TIME, "tv0": TV0, "nodes": {}, "vehicles": {}}
    passwords = {n: secrets.token_hex(16) for n in PORTS}

    print("[2] starting TA (genesis/admin) node")
    ta = launch("ta", passwords["ta"])
    ta_addr = ta.listpermissions("admin")[0]["address"]
    state["nodes"]["ta"] = {"password": passwords["ta"], "address": ta_addr}

    print("[3] joining RSU nodes and granting connect,send,receive,mine")
    for rsu in RSUS:
        addr = first_join(rsu, passwords[rsu])
        ta.wait_confirmed([ta.grant(addr, "connect,send,receive,mine")])
        launch(rsu, passwords[rsu])
        state["nodes"][rsu] = {"password": passwords[rsu], "address": addr}
        print(f"    {rsu} {addr}")
    STATE_FILE.write_text(json.dumps(state, indent=2))
    nodes = {n: Node(n, CHAIN, PORTS[n][1], "multichainrpc", passwords[n]) for n in state["nodes"]}

    print("[4] creating closed streams", STREAMS, "and CREDIT asset")
    txids = [ta.create("stream", s, False) for s in STREAMS]
    txids.append(ta.issue(ta_addr, {"name": "CREDIT", "open": True}, 1_000_000, 1))
    ta.wait_confirmed(txids)
    for node in nodes.values():
        node.wait_confirmed(txids)  # RSUs must have the block before they can subscribe
        for s in STREAMS:
            node.subscribe(s)
    rsu_addrs = ",".join(state["nodes"][r]["address"] for r in RSUS)
    ta.wait_confirmed([ta.grant(rsu_addrs, "verdicts.write")])

    print("[5] registering vehicles (TA = configureVEH)")
    txids = []
    for name, rsu in VEHICLES.items():
        addr = nodes[rsu].getnewaddress()
        state["vehicles"][name] = {"address": addr, "node": rsu}
        txids += [ta.grant(addr, "send,receive"), ta.grant(addr, "reports.write"),
                  ta.publish("registry", [addr, name], {"json": {"vehicle": name, "rsu": rsu}}),
                  ta.publish("trust", [addr, "registration"],
                             {"json": {"tv": TV0, "status": "active", "blocked_until": 0, "event": "registration"}})]
        print(f"    {name} {addr} via {rsu}")
    ta.wait_confirmed(txids)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("done. state saved to", STATE_FILE.name)


def start():
    state, _ = load()
    for name, v in state["nodes"].items():
        launch(name, v["password"])
        print("started", name)


def stop():
    state, nodes = load()
    for name, node in nodes.items():
        try:
            node.stop()
            print("stopped", name)
        except OSError:
            print(name, "not running")


def status():
    state, nodes = load()
    miners = {p["address"] for p in nodes["ta"].listpermissions("mine")}
    for name, node in nodes.items():
        try:
            info = node.getinfo()
            mine = "miner" if state["nodes"][name]["address"] in miners else "-"
            print(f"{name:5} height={info['blocks']:<6} peers={info['connections']} {mine:5} "
                  f"{state['nodes'][name]['address']}")
        except OSError:
            print(f"{name:5} not running")


def reset():
    if STATE_FILE.exists():
        stop()
        time.sleep(3)
        STATE_FILE.unlink()
    shutil.rmtree(NODES_DIR, ignore_errors=True)
    print("reset done")


if __name__ == "__main__":
    cmds = {"setup": setup, "start": start, "stop": stop, "status": status, "reset": reset}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
