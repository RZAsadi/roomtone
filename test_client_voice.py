"""Source-level guards for the embedded WebRTC negotiation recovery path."""
from pathlib import Path
import re
import unittest


class ClientVoiceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", source, re.DOTALL)
        if not match:
            raise AssertionError("Embedded browser client script was not found")
        cls.client = match.group(1)

    def test_unstable_signaling_defers_renegotiation_until_stable(self):
        self.assertIn("peer.pc.signalingState!=='stable')return", self.client)
        self.assertIn("peer.pc.onsignalingstatechange=()=>{if(peer.pc.signalingState==='stable'&&peer.pendingNegotiation&&!peer.remoteOfferPending)requestNegotiation(peer)}", self.client)
        self.assertIn("if(!failed&&peer.pendingNegotiation&&peer.pc.signalingState==='stable'&&!peer.remoteOfferPending)requestNegotiation(peer)", self.client)

    def test_role_changes_and_ice_recovery_use_shared_negotiation_path(self):
        self.assertIn("trans.direction=state.role==='listener'?'recvonly':'sendrecv';requestNegotiation(p)", self.client)
        self.assertIn("requestNegotiation(peer,true)", self.client)
        self.assertIn("peer.pc.createOffer(restart?{iceRestart:true}:undefined)", self.client)
        self.assertIn("peer.restarts<3", self.client)

    def test_incoming_offers_wait_for_local_negotiation_task(self):
        self.assertIn("if(p.negotiationTask)await p.negotiationTask", self.client)
        self.assertIn("p.remoteOfferPending=true", self.client)


if __name__ == "__main__":
    unittest.main()
