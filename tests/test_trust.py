import base64
import time
import zlib

import pytest

from mycel.identity import Identity
from mycel.trust import (TrustError, TrustState, issue_cert, issue_root, join_code, membership_code,
                               parse_join_code, parse_membership_code, verify_chain)


def setup():
    founder = Identity.generate()
    return founder, [issue_root(founder)]


def test_chain_of_invitations():
    founder, root = setup()
    a, b = Identity.generate(), Identity.generate()
    a_chain = issue_cert(founder, root, a.node_id, a.pubkey, can_invite=True)
    b_chain = issue_cert(a, a_chain, b.node_id, b.pubkey)
    verify_chain(b_chain, founder.node_id, b.node_id, b.pubkey)
    c = Identity.generate()
    with pytest.raises(TrustError, match="invite rights"):
        issue_cert(b, b_chain, c.node_id, c.pubkey)  # b was not given invite rights


def test_forged_and_misused_chains_rejected():
    founder, root = setup()
    a, mallory = Identity.generate(), Identity.generate()
    a_chain = issue_cert(founder, root, a.node_id, a.pubkey)
    with pytest.raises(TrustError, match="another node"):
        verify_chain(a_chain, founder.node_id, mallory.node_id, mallory.pubkey)  # stolen chain
    with pytest.raises(TrustError, match="different mesh"):
        verify_chain(a_chain, mallory.node_id, a.node_id, a.pubkey)
    fake_root = [issue_root(mallory)]
    forged = issue_cert(mallory, fake_root, a.node_id, a.pubkey)
    with pytest.raises(TrustError):
        verify_chain(forged, founder.node_id, a.node_id, a.pubkey)
    tampered = [root[0], {**a_chain[1], "body": {**a_chain[1]["body"], "can_invite": True}}]
    with pytest.raises(TrustError):
        verify_chain(tampered, founder.node_id, a.node_id, a.pubkey)
    with pytest.raises(TrustError, match="expired"):
        verify_chain(a_chain, founder.node_id, a.node_id, a.pubkey, now=time.time() + 400 * 86400)


def test_revocation_cuts_whole_subtree():
    founder, root = setup()
    trust = TrustState(founder.node_id, root)
    a, b = Identity.generate(), Identity.generate()
    a_chain = issue_cert(founder, root, a.node_id, a.pubkey, can_invite=True)
    b_chain = issue_cert(a, a_chain, b.node_id, b.pubkey)
    trust.revoke(founder, a.node_id)
    with pytest.raises(TrustError, match="revoked"):
        trust.check_member({"kind": "private", "chain": b_chain}, b.node_id, b.pubkey)
    other = TrustState(founder.node_id, a_chain)
    assert other.accept_revocation(trust.revocation)
    assert not other.accept_revocation(trust.revocation)  # same seq: no change
    forged = a.seal("revocation", {"mesh": founder.node_id, "revoked": []}, seq=99)
    assert not other.accept_revocation(forged)  # only the founder may revoke


def test_codes_roundtrip_and_bomb():
    founder, root = setup()
    me = Identity.generate()
    nid, pub = parse_join_code(join_code(me))
    chain = issue_cert(founder, root, nid, pub)
    mesh, got, boot = parse_membership_code(membership_code(chain, ["x@127.0.0.1:1"]), me)
    assert mesh == founder.node_id and got == chain and boot == ["x@127.0.0.1:1"]
    with pytest.raises(TrustError):
        parse_membership_code(membership_code(chain, []), Identity.generate())  # someone else's code
    bomb = "mym1_" + base64.urlsafe_b64encode(zlib.compress(b"[" * 10_000_000)).decode()
    with pytest.raises(TrustError):
        parse_membership_code(bomb, me)


def test_global_tickets():
    from mycel import trust
    from mycel.trust import mint_ticket, verify_ticket

    me, other = Identity.generate(), Identity.generate()
    t = mint_ticket(me)
    verify_ticket(t, me.node_id, me.pubkey)
    with pytest.raises(TrustError):
        verify_ticket(t, other.node_id, other.pubkey)          # bound to the key that did the work
    weak = mint_ticket(me, bits=0)
    old = trust.GLOBAL_BITS
    trust.GLOBAL_BITS = 16
    try:
        if trust._work(me.pubkey_raw, weak["nonce"]) < 16:
            with pytest.raises(TrustError, match="proof of work"):
                verify_ticket(weak, me.node_id, me.pubkey)
    finally:
        trust.GLOBAL_BITS = old
