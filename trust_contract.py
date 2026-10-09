"""Algorithms 1-3 of Singh et al. (2020) re-implemented on MultiChain.

The paper's Solidity contract becomes:
    access modifiers (OnlyTA, OnlyRSU, intelligentVehicle)  -> MultiChain permissions (admin, verdicts.write, reports.write)
    registers PTR/VISR/SSR/VR/CR                            -> streams reports / verdicts / trust / claims / registry
    ether credit score                                      -> CREDIT asset
    soft revocation (blocked 1 min)                         -> grant send with a future startblock (chain-enforced)
    hard revocation (TV < 0, RevocationList)                -> revoke send,receive,reports.write (chain-enforced)
The checks the contract did with `require(...)` are done here before the transaction is built; the
permission parts are additionally enforced by every node's consensus rules.

Interpretation choices where the paper is ambiguous (see README):
    count = number of distinct reporters in the session (the paper's s.count starts at 1, an off-by-one)
    a suspect is punished once per session, not once per report naming it
    a vehicle is blocked until  now + 1 minute  (paper writes v.time + 1 minutes)
"""
import math
from collections import Counter

from mcrpc import RPCError

MIN_PARTICIPANTS = 4    # Alg. 2: s.count >= 4
SOFT_BLOCK_SECONDS = 60 # Fig. 8: "You are blocked for 1 minute"
REWARD = 5              # Alg. 3: creditScore += 5
ALARMER_BONUS = 2       # Alg. 3: alarmer gets +2
MAX_BLOCK = 4294967295


class ContractError(Exception):
    """Equivalent of a failed Solidity require()."""


class TrustContract:
    def __init__(self, state, nodes):
        self.state, self.nodes, self.ta = state, nodes, nodes["ta"]
        self.ta_addr = state["nodes"]["ta"]["address"]
        self.addr = {n: v["address"] for n, v in state["vehicles"].items()}
        self.name = {a: n for n, a in self.addr.items()}
        self.block_len = math.ceil(SOFT_BLOCK_SECONDS / state["block_time"])

    def node_of(self, vehicle):
        return self.nodes[self.state["vehicles"][vehicle]["node"]]

    # ---------- reads ----------
    def trust(self, addr, node=None):
        """Current VR entry (TV, status, blocked_until) = latest item for this address in the trust stream."""
        items = (node or self.ta).liststreamkeyitems("trust", addr, False, 1)
        if not items:
            raise ContractError("Not an Intelligent Vehicle")
        return items[-1]["data"]["json"]

    def status(self, vehicle):
        t = self.trust(self.addr[vehicle])
        if t["status"] == "revoked":
            return "revoked"
        return "blocked" if t["blocked_until"] > self.ta.getblockcount() else "active"

    def credit(self, vehicle):
        bal = self.node_of(vehicle).getaddressbalances(self.addr[vehicle])
        return next((b["qty"] for b in bal if b["name"] == "CREDIT"), 0)

    def reports(self, node, session):
        """Confirmed reports of a session as (reporter, suspect) in chain order."""
        items = node.liststreamkeyitems("reports", session, False, 100000)
        return [(i["publishers"][0], i["data"]["json"]["suspect"]) for i in items if i.get("confirmations", 0) > 0]

    # ---------- Algorithm 1: reportSuspicion ----------
    def report_suspicion(self, reporter, session, suspect, evidence=None):
        node, r_addr, s_addr = self.node_of(reporter), self.addr[reporter], self.addr.get(suspect)
        me = self.trust(r_addr, node)                                   # modifier intelligentVehicle
        if me["status"] == "revoked":
            raise ContractError("Your vehicle is Revoked")              # modifier notRevoked
        if me["blocked_until"] > node.getblockcount():
            raise ContractError("You are blocked for 1 minute")        # modifier notBlocked
        if s_addr is None or not node.liststreamkeyitems("registry", s_addr, False, 1):
            raise ContractError("Suspect is not an Intelligent Vehicle")
        for item in node.liststreamkeyitems("reports", session, False, 100000):
            if item["publishers"][0] == r_addr and item["data"]["json"]["suspect"] == s_addr:
                raise ContractError("Duplicate submission")
        return node.publishfrom(r_addr, "reports", [session, f"suspect:{s_addr}"],
                                {"json": {"session": session, "suspect": s_addr, "evidence": evidence or []}})

    # ---------- Algorithm 2: analyseReports (RSU) ----------
    def tally(self, node, session):
        reps = list(dict.fromkeys(self.reports(node, session)))   # distinct (reporter, suspect), chain order
        reporters = {r for r, _ in reps}
        count = len(reporters)
        score = Counter(s for _, s in reps)
        guilty = sorted(s for s, sc in score.items() if count >= MIN_PARTICIPANTS and sc > count / 2)
        return {"session": session, "count": count, "scores": dict(score), "misbehaving": guilty,
                "alarmer": reps[0][0] if reps else None}

    def analyse_reports(self, rsu, session):
        node = self.nodes[rsu]
        if node.liststreamkeyitems("verdicts", session, False, 1):
            raise ContractError("Session already analysed")
        verdict = self.tally(node, session)
        txid = node.publishfrom(self.state["nodes"][rsu]["address"], "verdicts", session, {"json": verdict})
        return verdict, txid

    def enforce(self, session):
        """TA applies the RSU verdict: TV -= 1, soft block for 1 minute, hard revocation when TV < 0."""
        items = self.ta.liststreamkeyitems("verdicts", session, False, 1)
        if not items:
            raise ContractError("Session not analysed by an RSU yet")
        verdict = items[-1]["data"]["json"]
        if verdict != self.tally(self.ta, session):                    # TA re-checks the RSU's arithmetic
            raise ContractError("RSU verdict does not match the reports on chain")
        done = {k for i in self.ta.liststreamkeyitems("trust", f"penalty:{session}", False, 1000) for k in i["keys"]}
        actions, txids = [], []
        for s_addr in verdict["misbehaving"]:
            if s_addr in done:
                continue
            t = dict(self.trust(s_addr))
            t["tv"] -= 1
            height = self.ta.getblockcount()
            if t["tv"] < 0:                                             # hard revocation
                txids += [self.ta.revoke(s_addr, "send,receive"), self.ta.revoke(s_addr, "reports.write")]
                t.update(status="revoked", blocked_until=0)
            else:                                                       # soft revocation
                until = height + self.block_len
                txids.append(self.ta.grant(s_addr, "send", 0, until, MAX_BLOCK))
                t.update(status="blocked", blocked_until=until)
            t["event"] = f"penalty:{session}"
            txids.append(self.ta.publish("trust", [s_addr, f"penalty:{session}"], {"json": t}))
            actions.append((self.name[s_addr], t["tv"], t["status"]))
        return actions, txids

    # ---------- Algorithm 3: claimReward ----------
    def claim_reward(self, vehicle, session):
        addr = self.addr[vehicle]
        t = dict(self.trust(addr))
        if t["status"] == "revoked":
            raise ContractError("Your vehicle is Revoked")
        items = self.ta.liststreamkeyitems("verdicts", session, False, 1)
        if not items:
            raise ContractError("Session not analysed by an RSU yet")
        verdict = items[-1]["data"]["json"]
        if self.ta.liststreamkeyitems("claims", f"{session}:{addr}", False, 1):
            raise ContractError("Reward already claimed")
        mine = {s for r, s in self.reports(self.ta, session) if r == addr}
        if not mine & set(verdict["misbehaving"]):                    # P.submitted == S.verificationResult
            raise ContractError("No verified report in this session")
        amount = REWARD + (ALARMER_BONUS if verdict["alarmer"] == addr else 0)
        t["tv"] += 1
        t["event"] = f"reward:{session}"
        txids = [self.ta.sendassetfrom(self.ta_addr, addr, "CREDIT", amount),
                 self.ta.publish("trust", [addr, f"reward:{session}"], {"json": t}),
                 self.ta.publish("claims", f"{session}:{addr}", {"json": {"amount": amount, "tv": t["tv"]}})]
        return amount, txids

    # ---------- TA only: readmit a hard-revoked vehicle ----------
    def reinstate(self, vehicle):
        addr = self.addr[vehicle]
        t = dict(self.trust(addr))
        if t["status"] != "revoked":
            raise ContractError("Vehicle is not revoked")
        t.update(tv=0, status="active", blocked_until=0, event="reinstated by TA")
        return [self.ta.grant(addr, "send,receive"), self.ta.grant(addr, "reports.write"),
                self.ta.publish("trust", [addr, "reinstated"], {"json": t})]

    def raw_publish(self, vehicle, session, suspect):
        """Bypass all client-side checks - used to show the chain itself rejects a blocked/revoked vehicle."""
        try:
            return self.node_of(vehicle).publishfrom(self.addr[vehicle], "reports", [session, "bypass"],
                                                     {"json": {"session": session, "suspect": self.addr[suspect]}})
        except RPCError as e:
            return f"REJECTED by chain: {e}"
