import time

import pytest

from mycel.governor import Governor, Limits, Rejected
from mycel.identity import EnvelopeError, Identity, ReplayCache, verify_direct, verify_relayed
from mycel.location import decode_center, encode, public_cell, sanitize_remote_cell
from mycel.membership import Membership
from mycel.netguard import validate_mesh_bind


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- identity
def test_direct_envelope_roundtrip_and_replay():
    a, b = Identity.generate(), Identity.generate()
    env = a.seal("infer", {"x": 1}, to=b.node_id)
    cache = ReplayCache()
    assert verify_direct(env, expected_type="infer", me=b.node_id, replay=cache)["body"] == {"x": 1}
    with pytest.raises(EnvelopeError, match="replayed"):
        verify_direct(env, expected_type="infer", me=b.node_id, replay=cache)


def test_tampered_misaddressed_stale_and_spoofed_envelopes_rejected():
    a, b, c = Identity.generate(), Identity.generate(), Identity.generate()
    env = a.seal("infer", {"x": 1}, to=b.node_id)
    tampered = {**env, "body": {"x": 2}}
    with pytest.raises(EnvelopeError):
        verify_direct(tampered, expected_type="infer", me=b.node_id, replay=ReplayCache())
    with pytest.raises(EnvelopeError, match="another node"):
        verify_direct(env, expected_type="infer", me=c.node_id, replay=ReplayCache())
    with pytest.raises(EnvelopeError, match="stale"):
        verify_direct(env, expected_type="infer", me=b.node_id, replay=ReplayCache(), now=time.time() + 120)
    spoof = {**env, "from": c.node_id}  # claim to be someone else with a's key
    with pytest.raises(EnvelopeError):
        verify_direct(spoof, expected_type="infer", me=b.node_id, replay=ReplayCache())


def test_relayed_advert_expiry():
    a = Identity.generate()
    adv = a.seal("advert", {"addr": "127.0.0.1:1"}, seq=1)
    verify_relayed(adv, expected_type="advert", max_age_s=30)
    with pytest.raises(EnvelopeError, match="expired"):
        verify_relayed(adv, expected_type="advert", max_age_s=30, now=time.time() + 60)


def test_key_file_created_private(tmp_path):
    p = tmp_path / "k" / "node_key.pem"
    ident = Identity.load_or_create(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert Identity.load_or_create(p).node_id == ident.node_id
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        Identity.load_or_create(p)


# ---------------------------------------------------------------- governor
def test_share_limit_is_hard():
    clk = Clock()
    g = Governor(Limits(max_share=0.25, window_s=60, max_concurrent=4, max_tokens_per_job=10_000,
                        per_peer_per_min=1000), clock=clk)
    used = 0.0
    for i in range(200):  # hammer it for 10 simulated minutes
        try:
            grant = g.admit(f"j{i}", f"p{i}", 500, tokens_per_s=50)
            clk.t += grant.deadline - grant.started  # job runs to its deadline
            used += grant.finish()
        except Rejected:
            clk.t += 3
    elapsed = clk.t - 1000.0
    assert used <= 0.25 * elapsed + 0.25 * 60 + 1e-6  # never more than share (+ initial bucket)


def test_budget_sets_deadline_not_token_cap_and_refunds():
    clk = Clock()
    g = Governor(Limits(max_share=0.1, window_s=60, max_tokens_per_job=4096), clock=clk)  # 6 s bucket
    grant = g.admit("j", "p", 4096, tokens_per_s=100)
    assert grant.max_tokens == 4096                      # replies aren't truncated up front...
    assert grant.deadline - grant.started == pytest.approx(6.0)  # ...the budget becomes a hard deadline
    clk.t += 1.0
    assert grant.finish() == pytest.approx(1.0)
    assert g.available_seconds() == pytest.approx(5.1, abs=0.01)  # refunded the unused reservation + refill
    tight = Governor(Limits(max_share=0.01, window_s=60), clock=clk)  # 0.6 s: not even a short reply fits
    with pytest.raises(Rejected, match="budget"):
        tight.admit("k", "p", 100, tokens_per_s=100)


def test_pause_concurrency_peer_rate_and_daily_cap():
    clk = Clock()
    g = Governor(Limits(max_share=1.0, max_concurrent=1, per_peer_per_min=2, daily_seconds=5), clock=clk)
    a = g.admit("a", "p", 10, 10)
    with pytest.raises(Rejected, match="concurrency"):
        g.admit("b", "q", 10, 10)
    clk.t += 2; a.finish()
    g.admit("c", "p", 10, 10).finish()
    with pytest.raises(Rejected, match="per-peer"):
        g.admit("d", "p", 10, 10)
    clk.t += 1
    e = g.admit("e", "q", 1000, 100)  # daily cap: 5 s total, 2 used -> at most 3 s left
    assert e.deadline - e.started <= 3.0 + 1e-6
    g.update(paused=True)
    with pytest.raises(Rejected, match="paused"):
        g.admit("f", "z", 10, 10)


def test_zero_share_never_admits():
    g = Governor(Limits(max_share=0.0))
    with pytest.raises(Rejected):
        g.admit("a", "p", 10, 100)


# ---------------------------------------------------------------- location
def test_geohash_and_privacy():
    assert encode(57.64911, 10.40744, 4) == "u4pr"  # reference vector (truncated)
    lat, lon, dlat, dlon = decode_center("u4pr")
    assert abs(lat - 57.64911) <= dlat and abs(lon - 10.40744) <= dlon
    assert public_cell(False, 1.0, 2.0, "metro") is None
    assert len(public_cell(True, 1.0, 2.0, "region")) == 3
    assert sanitize_remote_cell("u4pruydqqvj") == "u4pr"
    assert sanitize_remote_cell("<script>") is None


# ---------------------------------------------------------------- membership
from mycel.trust import TrustState, issue_cert, issue_root


def _member_state():
    founder = Identity.generate()
    return founder, TrustState(founder.node_id, [issue_root(founder)])


def _advert(ident, seq, chain, **body):
    proof = {"kind": "private", "chain": chain}
    return ident.seal("advert", {"addr": "127.0.0.1:9", "models": [], "membership": proof, **body}, seq=seq)


def test_membership_requires_proof_and_tracks_seq():
    founder, trust = _member_state()
    a, outsider = Identity.generate(), Identity.generate()
    a_chain = issue_cert(founder, trust.chain, a.node_id, a.pubkey)
    m = Membership("me", ttl_s=30, trust=trust)
    assert m.ingest(_advert(a, 5, a_chain))
    assert not m.ingest(_advert(a, 4, a_chain))  # older seq ignored
    assert m.ingest(_advert(a, 6, a_chain))
    assert not m.ingest(_advert(outsider, 1, a_chain))  # someone else's certs
    assert not m.ingest(_advert(outsider, 1, []))       # no certs
    assert not m.ingest(_advert(a, 7, a_chain, location="u4pruydq"))  # finer than allowed
    assert m.rejected == 3


def test_global_membership_by_ticket_and_local_block():
    from mycel.trust import GLOBAL_MESH_ID, mint_ticket

    me, a, b = Identity.generate(), Identity.generate(), Identity.generate()
    trust = TrustState(GLOBAL_MESH_ID, ticket=mint_ticket(me))
    m = Membership(me.node_id, ttl_s=30, trust=trust)
    adv = lambda ident, seq, ticket: ident.seal("advert", {"addr": "127.0.0.1:9", "models": [],
                                                            "membership": {"kind": "global", "ticket": ticket}}, seq=seq)
    assert m.ingest(adv(a, 1, mint_ticket(a)))
    assert not m.ingest(adv(b, 1, mint_ticket(a)))                 # stolen ticket
    assert not m.ingest(adv(b, 1, {**mint_ticket(b, bits=0), "bits": 30}))  # claims work it didn't do
    trust.blocked.add(a.node_id)
    m.apply_revocations()
    assert a.node_id not in m.peers and not m.ingest(adv(a, 2, mint_ticket(a)))


# ---------------------------------------------------------------- exposure rules
def test_mesh_bind_rules():
    assert validate_mesh_bind("127.0.0.1", False) == "127.0.0.1"
    for wild in ("0.0.0.0", "::"):
        with pytest.raises(ValueError, match="wildcard"):
            validate_mesh_bind(wild, True)
    with pytest.raises(ValueError, match="not loopback"):
        validate_mesh_bind("100.64.0.7", False)
    assert validate_mesh_bind("100.64.0.7", True) == "100.64.0.7"
