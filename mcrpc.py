"""Minimal MultiChain JSON-RPC client (stdlib only)."""
import base64
import json
import time
import urllib.error
import urllib.request


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(f"[{code}] {message}")
        self.code = code


class Node:
    """One MultiChain node reachable over JSON-RPC. `node.publish(...)` calls the RPC method `publish`."""

    def __init__(self, name, chain, rpcport, rpcuser, rpcpassword, host="127.0.0.1"):
        self.name = name
        self.chain = chain
        self.url = f"http://{host}:{rpcport}"
        token = base64.b64encode(f"{rpcuser}:{rpcpassword}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def call(self, method, *params):
        body = json.dumps({"method": method, "params": list(params), "id": 1, "chain_name": self.chain}).encode()
        req = urllib.request.Request(self.url, body, self.headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:  # MultiChain returns RPC errors with HTTP 500 + JSON body
            reply = json.loads(e.read() or b"{}")
            if not reply:
                raise
        if reply.get("error"):
            raise RPCError(reply["error"]["code"], reply["error"]["message"])
        return reply["result"]

    def __getattr__(self, method):
        return lambda *params: self.call(method, *params)

    def wait_confirmed(self, txids, timeout=120, since_height=None):
        """Block until every txid in `txids` is in a block (as seen by this node).
        since_height: height before the txs were sent -> skip per-tx lookups and just scan newer blocks."""
        deadline = time.time() + timeout
        if since_height is None:
            height = self.getblockcount()  # snapshot first so no block is missed between the two steps
            pending = {t for t in txids if not self._confirmed(t)}
        else:
            height, pending = since_height, set(txids)
        while pending:
            if time.time() > deadline:
                raise TimeoutError(f"{len(pending)} tx not confirmed after {timeout}s")
            new = self.getblockcount()
            for h in range(height + 1, new + 1):
                pending -= set(self.getblock(str(h), 1)["tx"])
            height = new
            if pending:
                time.sleep(0.1)

    def _confirmed(self, txid):
        try:
            return self.getrawtransaction(txid, 1).get("confirmations", 0) > 0
        except RPCError:
            return False  # still in a mempool / not yet propagated to this node

    def wait_blocks(self, n=1):
        target = self.getblockcount() + n
        while self.getblockcount() < target:
            time.sleep(0.25)
