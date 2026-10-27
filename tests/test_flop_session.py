"""flop_session.py against FLOP's published wire-format and money-path vectors (tests/vectors)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flop_session", ROOT / "flop_session.py")
assert SPEC and SPEC.loader
session = importlib.util.module_from_spec(SPEC)
sys.modules["flop_session"] = session
SPEC.loader.exec_module(session)

VECTORS = ROOT / "tests" / "vectors"
WIRE = json.loads((VECTORS / "wire-format-v1.json").read_text())
MONEY = json.loads((VECTORS / "money-path-v1.json").read_text())
CC = WIRE["compute_channel_v1"]
NEGATIVE = {case["id"]: bytes.fromhex(case["bytes_hex"]) for case in WIRE["negative_cases"]}
HAVE_SR25519 = importlib.util.find_spec("sr25519") is not None
needs_sr25519 = unittest.skipUnless(HAVE_SR25519, "py-sr25519-bindings not installed (requirements-session.txt)")

LEAF = CC["leaf_inputs"]
CHANNEL = bytes.fromhex(CC["channel_id"]["hash_hex"])
POLICY = bytes.fromhex(LEAF["decode_policy_hash_hex"])


def h(value: str) -> bytes:
    return bytes.fromhex(value)


def vector_turn(version, signature: bytes = b"\x00" * 64):
    version = session.LeafVersion(version)
    v3 = version == session.LeafVersion.V3
    timed = version >= session.LeafVersion.V1
    return session.Turn(
        version=version,
        turn_index=LEAF["turn_index"],
        h_in=h(LEAF["h_in_hex"]),
        h_out=h(LEAF["h_out_hex"]),
        g_n=int(LEAF["g_n"]),
        decode_policy_hash=POLICY if version >= session.LeafVersion.V2 else None,
        h_ids=h(LEAF["h_ids_hex"]) if v3 else session.ZERO_HASH,
        toploc_commitment_hash=h(LEAF["toploc_commitment_hash_hex"]) if v3 else session.ZERO_HASH,
        miner_recv_ms=int(LEAF["miner_recv_ms"]) if timed else 0,
        miner_done_ms=int(LEAF["miner_done_ms"]) if timed else 0,
        latency_ms=int(LEAF["latency_ms"]) if timed else 0,
        enclave_sig=signature,
    )


class VectorFileTests(unittest.TestCase):
    def test_vendored_vectors_are_the_pinned_upstream_files(self) -> None:
        expected = {
            "wire-format-v1.json": "53440d853f1e595f045ddc6365180e74d0ff366be534dbe823a4bf412e5011de",
            "money-path-v1.json": "05c4afc7735c26b32d35d37e7207930110747ebac12c6f6bb4554fbc9fe115a2",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((VECTORS / name).read_bytes()).hexdigest(), digest, name)


class CodecTests(unittest.TestCase):
    def test_compact_u32_accept_vectors_round_trip(self) -> None:
        for case in WIRE["codec"]["scale_compact_u32"]:
            with self.subTest(value=case["value"]):
                self.assertEqual(session.compact_u32(case["value"]).hex(), case["bytes_hex"])
                self.assertEqual(session.decode_compact_u32(h(case["bytes_hex"])), case["value"])

    def test_malformed_compact_vectors_reject(self) -> None:
        for case in WIRE["codec"]["malformed_compact"]:
            with self.subTest(reason=case["reason"]), self.assertRaises(session.WireError):
                session.decode_compact_u32(h(case["bytes_hex"]))

    def test_scale_bool_and_integer_overflow(self) -> None:
        self.assertTrue(session.Reader(b"\x01").boolean())
        with self.assertRaises(session.WireError):
            session.Reader(b"\x02").boolean()
        with self.assertRaises(session.WireError):
            session.uint_le(1 << 32, 32)
        with self.assertRaises(session.WireError):
            session.uint_le(-1, 64)


class IdentityTests(unittest.TestCase):
    def test_channel_id_vector(self) -> None:
        inputs = CC["channel_id"]["inputs"]
        args = (
            h(inputs["genesis_hash_hex"]),
            h(inputs["agent_account_id32_hex"]),
            h(inputs["miner_account_id32_hex"]),
            int(inputs["nonce"]),
        )
        self.assertEqual(session.channel_id(*args), CHANNEL)

    def test_channel_id_binds_network_and_session(self) -> None:
        inputs = CC["channel_id"]["inputs"]
        genesis, agent = h(inputs["genesis_hash_hex"]), h(inputs["agent_account_id32_hex"])
        miner, nonce = h(inputs["miner_account_id32_hex"]), int(inputs["nonce"])
        other_network = session.channel_id(b"\x01" + genesis[1:], agent, miner, nonce)
        other_agent = session.channel_id(genesis, b"\x12" * 32, miner, nonce)
        self.assertEqual(other_network, NEGATIVE["wrong_genesis_network"])
        self.assertEqual(other_agent, NEGATIVE["wrong_session"])
        self.assertNotEqual(other_network, CHANNEL)

    def test_decode_policy_default_vector(self) -> None:
        policy = session.DecodePolicy()
        vector = WIRE["decode_policy_v1"]
        self.assertEqual(policy.encode().hex(), vector["scale_bytes_hex"])
        self.assertEqual(policy.hash().hex(), vector["sha256_hex"])

    def test_h_ids_vectors(self) -> None:
        for case in WIRE["h_ids_v1"]["vectors"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(session.h_ids(case["prompt_ids"], case["generated_ids"]).hex(), case["hash_hex"])


class TranscriptTests(unittest.TestCase):
    def test_leaf_preimages_and_hashes_for_every_version(self) -> None:
        for case in CC["leaf_versions"]:
            with self.subTest(version=case["version"]):
                turn = vector_turn(case["scale_tag"])
                self.assertEqual(session.leaf_preimage(CHANNEL, turn).hex(), case["preimage_hex"])
                self.assertEqual(session.leaf_hash(CHANNEL, turn).hex(), case["hash_hex"])

    def test_merkle_root_and_path(self) -> None:
        leaves = {case["version"]: h(case["hash_hex"]) for case in CC["leaf_versions"]}
        ordered = [leaves[name] for name in CC["merkle"]["leaf_order"]]
        root = session.merkle_root(ordered)
        self.assertEqual(root.hex(), CC["merkle"]["root_hex"])
        expected_path = [(h(item["sibling_hex"]), item["sibling_is_left"]) for item in CC["merkle"]["path_for_index_2"]]
        self.assertEqual(session.merkle_path(ordered, 2), expected_path)
        self.assertEqual(session.root_from_path(ordered[2], expected_path), root)
        self.assertEqual(session.merkle_root([]), session.ZERO_HASH)
        self.assertEqual(session.merkle_root([ordered[0]]), ordered[0])

    def test_verified_turn_v3_round_trip(self) -> None:
        encoded = h(CC["verified_turn_v3_scale_hex"])
        turn, path = session.decode_verified_turn(encoded)
        self.assertEqual(session.leaf_hash(CHANNEL, turn).hex(), CC["v3_leaf_signature"]["leaf_hash_hex"])
        self.assertEqual(session.encode_verified_turn(turn, path), encoded)
        self.assertEqual(len(encoded), 269 + 1 + 33 * len(path))

    def test_fcc4_transcript_round_trip(self) -> None:
        blob = h(CC["fcc4_transcript_blob_hex"])
        channel, entries = session.decode_transcript(blob)
        self.assertEqual(channel, CHANNEL)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0][1])
        self.assertEqual(session.encode_transcript(channel, entries), blob)

    def test_fcc4_with_ack_and_ack_message(self) -> None:
        case = CC["fcc4_transcript_with_ack"]
        channel, entries = session.decode_transcript(h(case["blob_hex"]))
        turn, ack = entries[0]
        self.assertEqual((ack.agent_send_ms, ack.agent_recv_ms), (int(case["agent_send_ms"]), int(case["agent_recv_ms"])))
        message = session.ack_message(channel, turn.turn_index, session.leaf_hash(channel, turn), ack.agent_send_ms, ack.agent_recv_ms)
        self.assertEqual(message.hex(), case["ack_preimage_hex"])
        self.assertEqual(session.encode_transcript(channel, entries), h(case["blob_hex"]))

    def test_receipt_v1_preimage(self) -> None:
        case = CC["receipt"]
        inputs = case["inputs"]
        message = session.receipt_message_v1(
            h(inputs["channel_id_hex"]), h(inputs["final_root_hex"]), int(inputs["aggregate_gn"]), int(inputs["payable"])
        )
        self.assertEqual(message.hex(), case["preimage_hex"])


class NegativeCaseTests(unittest.TestCase):
    def test_unknown_leaf_enum(self) -> None:
        with self.assertRaises(session.WireError):
            session.decode_verified_turn(NEGATIVE["unknown_leaf_enum"])

    def test_truncated_trailing_and_unknown_fcc_version(self) -> None:
        for case in ("truncated_fcc4", "trailing_fcc4", "unknown_fcc_version"):
            with self.subTest(case=case), self.assertRaises(session.WireError):
                session.decode_transcript(NEGATIVE[case])

    def test_wrong_leaf_version_rejects_at_decode(self) -> None:
        with self.assertRaises(session.WireError):
            session.decode_verified_turn(NEGATIVE["wrong_leaf_version"])

    def test_wrong_path_orientation_misses_the_root(self) -> None:
        turn, path = session.decode_verified_turn(NEGATIVE["wrong_path_orientation"])
        leaf = session.leaf_hash(CHANNEL, turn)
        self.assertNotEqual(session.root_from_path(leaf, path).hex(), CC["merkle"]["root_hex"])

    def test_legacy_leaf_on_policy_bound_channel(self) -> None:
        turn, _ = session.decode_verified_turn(NEGATIVE["legacy_leaf_current_channel"])
        session.check_leaf_cutoff(turn, None)
        with self.assertRaisesRegex(session.SessionError, "unsupported leaf version"):
            session.check_leaf_cutoff(turn, POLICY)

    def test_duplicate_turn_index_decodes_then_rejects(self) -> None:
        bundle = session.decode_verified_turns(NEGATIVE["duplicate_turn_index"])
        self.assertEqual(len(bundle), 2)
        self.assertEqual(bundle[0][0].turn_index, bundle[1][0].turn_index)
        with self.assertRaisesRegex(session.SessionError, "duplicate turn index"):
            session.verify_settlement_bundle(CHANNEL, b"\x00" * 32, bundle, session.ZERO_HASH, 0, None)


@needs_sr25519
class SignatureVectorTests(unittest.TestCase):
    def test_miner_leaf_signature_vector(self) -> None:
        case = CC["v3_leaf_signature"]
        self.assertTrue(session.sr25519_verify(h(case["public_key_hex"]), h(case["leaf_hash_hex"]), h(case["signature_hex"])))

    def test_agent_ack_signature_vector_and_flipped_byte(self) -> None:
        case = CC["fcc4_transcript_with_ack"]
        public_key = h(case["agent_public_key_hex"])
        self.assertTrue(session.sr25519_verify(public_key, h(case["ack_preimage_hex"]), h(case["agent_signature_hex"])))
        channel, entries = session.decode_transcript(NEGATIVE["invalid_agent_ack_signature"])
        turn, ack = entries[0]
        message = session.ack_message(channel, turn.turn_index, session.leaf_hash(channel, turn), ack.agent_send_ms, ack.agent_recv_ms)
        self.assertFalse(session.sr25519_verify(public_key, message, ack.agent_sig))

    def test_receipt_signature_vector_and_rejections(self) -> None:
        case = CC["receipt"]
        public_key, message = h(case["public_key_hex"]), h(case["preimage_hex"])
        self.assertTrue(session.sr25519_verify(public_key, message, h(case["signature_hex"])))
        self.assertFalse(session.sr25519_verify(public_key, message, NEGATIVE["invalid_receipt_signature"]))
        legacy = NEGATIVE["legacy_receipt_current_channel"]
        legacy_message, legacy_signature = legacy[:-64], legacy[-64:]
        self.assertTrue(session.sr25519_verify(public_key, legacy_message, legacy_signature))
        self.assertFalse(session.sr25519_verify(public_key, message, legacy_signature))

    def test_settlement_bundle_from_the_vector_turn(self) -> None:
        turn, path = session.decode_verified_turn(h(CC["verified_turn_v3_scale_hex"]))
        miner_key = h(CC["v3_leaf_signature"]["public_key_hex"])
        root = h(CC["merkle"]["root_hex"])
        session.verify_settlement_bundle(CHANNEL, miner_key, [(turn, path)], root, turn.g_n, POLICY)
        with self.assertRaisesRegex(session.SessionError, "aggregate_gn"):
            session.verify_settlement_bundle(CHANNEL, miner_key, [(turn, path)], root, turn.g_n - 1, POLICY)
        bad_path = session.decode_verified_turn(NEGATIVE["wrong_path_orientation"])
        with self.assertRaisesRegex(session.SessionError, "Merkle path"):
            session.verify_settlement_bundle(CHANNEL, miner_key, [bad_path], root, turn.g_n, POLICY)


@needs_sr25519
class AgentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = session.SessionKey.generate()
        self.agent = session.SessionKey.generate()
        self.policy = session.DecodePolicy().hash()
        self.channel = session.channel_id(b"\x07" * 32, b"\x01" * 32, b"\x02" * 32, 1)
        self.state = session.AgentSession(self.channel, self.agent, self.miner.public_key, self.policy)

    def turn(self, index: int, g_n: int = 10, policy: bytes | None = None, version=session.LeafVersion.V3):
        unsigned = session.Turn(
            version, index, bytes([index]) * 32, bytes([index + 1]) * 32, g_n,
            policy or self.policy, session.h_ids([1, 2], [3, index]), b"\x09" * 32, 100, 150, 50,
        )
        signature = self.miner.sign(session.leaf_hash(self.channel, unsigned))
        return session.Turn(**{**unsigned.__dict__, "enclave_sig": signature})

    def test_accepts_turns_countersigns_and_signs_receipt(self) -> None:
        for index in range(3):
            ack = self.state.accept_turn(self.turn(index), 90, 160)
            message = session.ack_message(self.channel, index, self.state.leaves[index], 90, 160)
            self.assertTrue(session.sr25519_verify(self.agent.public_key, message, ack.agent_sig))
        self.assertEqual(self.state.aggregate_gn(), 30)
        receipt = self.state.sign_receipt(payable=1000)
        expected = session.receipt_message_v1(self.channel, self.state.final_root(), 30, 1000)
        self.assertTrue(session.sr25519_verify(self.agent.public_key, expected, receipt))
        channel, entries = session.decode_transcript(self.state.transcript())
        self.assertEqual((channel, len(entries)), (self.channel, 3))
        bundle = [(turn, session.merkle_path(self.state.leaves, i)) for i, turn in enumerate(self.state.turns)]
        session.verify_settlement_bundle(self.channel, self.miner.public_key, bundle, self.state.final_root(), 30, self.policy)

    def test_refuses_out_of_order_policy_drift_legacy_and_forged_turns(self) -> None:
        with self.assertRaisesRegex(session.SessionError, "expected turn 0"):
            self.state.accept_turn(self.turn(1), 0, 0)
        with self.assertRaisesRegex(session.SessionError, "pinned policy"):
            self.state.accept_turn(self.turn(0, policy=b"\x05" * 32), 0, 0)
        forged = session.Turn(**{**self.turn(0).__dict__, "g_n": 11})
        with self.assertRaisesRegex(session.SessionError, "signature"):
            self.state.accept_turn(forged, 0, 0)
        self.assertEqual(self.state.turns, [])


class MoneyPathTests(unittest.TestCase):
    def landed(self):
        return [v for v in MONEY["vectors"] if v.get("adapter") == "compute-channel" and v["status"] == "landed"]

    def test_all_landed_compute_channel_vectors(self) -> None:
        profile = session.PROFILES["money-path-v1"]
        self.assertEqual(len(self.landed()), 5)
        for vector in self.landed():
            with self.subTest(vector=vector["id"]):
                escrow = int(vector["input"]["storage"]["escrow"])
                self.assertEqual(vector["input"]["profile"]["phi_percent"], profile.phi_percent)
                self.assertEqual(vector["input"]["profile"]["audit_fee_split_ppm"], profile.audit_ppm)
                call = vector["extrinsics"][0]
                expected = vector["expected"]
                if call["call"] == "timeout":
                    outcome = session.timeout_refund(escrow)
                else:
                    gn = int(call["args"]["aggregate_gn"])
                    turns = int(call["args"]["receipt_payable"]) - gn
                    if expected["result"] == "TariffExceedsEscrow":
                        with self.assertRaises(session.TariffExceedsEscrow):
                            session.force_finalize(escrow, turns, gn, profile)
                        continue
                    outcome = session.force_finalize(escrow, turns, gn, profile)
                    self.assertEqual(outcome.payable, int(call["args"]["receipt_payable"]))
                storage = expected["storage"]
                self.assertEqual(outcome.miner, int(storage["total_miner_paid"]))
                self.assertEqual(outcome.agent_refund, int(storage["total_refunded"]))
                self.assertEqual(outcome.penalty, int(storage["total_burned"]))
                self.assertEqual(outcome.audit, int(storage["total_audit_earmarked"]))
                self.assertEqual(outcome.total, escrow)

    def test_devnet_observed_cooperative_settlement(self) -> None:
        outcome = session.cooperative_settlement(3168000000000000150, session.PROFILES["devnet-observed"])
        self.assertEqual(outcome.miner, 3136320000000000149)
        self.assertEqual(outcome.total, 3168000000000000150)

    def test_spec_profile_conserves_and_splits_85_15(self) -> None:
        outcome = session.cooperative_settlement(10**18, session.PROFILES["spec-v0.5"])
        self.assertEqual((outcome.miner, outcome.validators, outcome.audit), (85 * 10**16, 14 * 10**16, 10**16))
        self.assertEqual(outcome.total, 10**18)


if __name__ == "__main__":
    unittest.main()
