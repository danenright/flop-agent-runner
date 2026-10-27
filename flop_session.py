"""Agent-side FLOP compute-channel session primitives.

An independent implementation of the FLOP Network Yellow Paper v0.5 (draft) wire formats an agent
needs for a direct session: Appendix F.0 codec, F.1 channel and decode-policy identities, F.3
transcript leaves, Merkle accumulator, VerifiedTurn and FCC4 containers, per-turn agent ack and
agent receipt v1, plus the R12.1d settlement arithmetic. It is checked against FLOP's published
wire-format and money-path vectors (tests/vectors). It contains no network or chain code.

Signatures are sr25519 under the Substrate "substrate" context, through the optional
py-sr25519-bindings package (requirements-session.txt). Everything else is standard library.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import IntEnum

CHANNEL_ID_DOMAIN = b"FLOP/COMPUTE_CHANNEL/ID"
RECEIPT_DOMAIN = b"FLOP/COMPUTE_CHANNEL/RECEIPT"
DECODE_POLICY_DOMAIN = b"FLOP_DECODE_POLICY_HASH_V1"
WIRE_VERSION_1 = b"\x01"
FCC4_MAGIC = b"FCC4"
ZERO_HASH = b"\x00" * 32
MAX_SETTLEMENT_TURNS = 1024


class WireError(ValueError):
    """Bytes or values that the Appendix F.0 profile requires an implementation to reject."""


class SessionError(ValueError):
    """A turn, receipt or settlement that an agent must refuse."""


class TariffExceedsEscrow(SessionError):
    pass


def blake2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def uint_le(value: int, bits: int, name: str = "value") -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value >= 1 << bits:
        raise WireError(f"{name} does not fit u{bits}")
    return value.to_bytes(bits // 8, "little")


def h256(value: bytes, name: str = "hash") -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        raise WireError(f"{name} must be 32 bytes")
    return bytes(value)


def compact_u32(value: int) -> bytes:
    uint_le(value, 32, "compact value")
    if value < 1 << 6:
        return bytes([value << 2])
    if value < 1 << 14:
        return ((value << 2) | 0b01).to_bytes(2, "little")
    if value < 1 << 30:
        return ((value << 2) | 0b10).to_bytes(4, "little")
    return b"\x03" + value.to_bytes(4, "little")


class Reader:
    """Strict cursor: truncation, non-minimal compacts, bad booleans and trailing bytes all reject."""

    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.offset = 0

    def take(self, length: int) -> bytes:
        end = self.offset + length
        if end > len(self.data):
            raise WireError("truncated input")
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def uint(self, bits: int) -> int:
        return int.from_bytes(self.take(bits // 8), "little")

    def hash(self) -> bytes:
        return self.take(32)

    def boolean(self) -> bool:
        flag = self.take(1)[0]
        if flag not in (0, 1):
            raise WireError("invalid boolean")
        return flag == 1

    def compact_u32(self) -> int:
        first = self.take(1)[0]
        mode = first & 0b11
        if mode == 0:
            return first >> 2
        if mode == 1:
            value = int.from_bytes(bytes([first]) + self.take(1), "little") >> 2
            if value < 1 << 6:
                raise WireError("non-minimal compact integer")
            return value
        if mode == 2:
            value = int.from_bytes(bytes([first]) + self.take(3), "little") >> 2
            if value < 1 << 14:
                raise WireError("non-minimal compact integer")
            return value
        if first >> 2 != 0:
            raise WireError("compact integer wider than u32")
        value = int.from_bytes(self.take(4), "little")
        if value < 1 << 30:
            raise WireError("non-minimal compact integer")
        return value

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise WireError("trailing bytes")


def decode_compact_u32(data: bytes) -> int:
    reader = Reader(data)
    value = reader.compact_u32()
    reader.finish()
    return value


# F.1 identities


def channel_id(genesis_hash: bytes, agent: bytes, miner: bytes, nonce: int) -> bytes:
    preimage = (
        CHANNEL_ID_DOMAIN
        + WIRE_VERSION_1
        + h256(genesis_hash, "genesis_hash")
        + h256(agent, "agent account")
        + h256(miner, "miner account")
        + uint_le(nonce, 64, "nonce")
    )
    return blake2_256(preimage)


class DecodeClass(IntEnum):
    TEXT_GENERATION = 0
    IMAGE_DENOISE = 1
    ROLLOUT = 2
    CONTROL_LOOP = 3
    OTHER = 4


@dataclass(frozen=True)
class SamplingParams:
    temperature_milli: int = 0
    top_p_ppm: int = 1_000_000
    top_k: int = 0
    repetition_penalty_ppm: int = 1_000_000
    beam_width: int = 1
    seed: int = 0

    def encode(self) -> bytes:
        return (
            uint_le(self.temperature_milli, 32, "temperature_milli")
            + uint_le(self.top_p_ppm, 32, "top_p_ppm")
            + uint_le(self.top_k, 32, "top_k")
            + uint_le(self.repetition_penalty_ppm, 32, "repetition_penalty_ppm")
            + uint_le(self.beam_width, 16, "beam_width")
            + uint_le(self.seed, 64, "seed")
        )


@dataclass(frozen=True)
class DecodePolicy:
    """`DecodePolicy` v1; the defaults match `hp_poui::DecodePolicy::default()` in FLOP's vectors."""

    decode_class: DecodeClass = DecodeClass.TEXT_GENERATION
    other_class: int | None = None
    tokenizer_hash: bytes = ZERO_HASH
    sampling: SamplingParams = field(default_factory=SamplingParams)
    stop_conditions_hash: bytes = ZERO_HASH
    output_transform_id: bytes | None = None
    class_policy_hash: bytes = ZERO_HASH

    def encode(self) -> bytes:
        tag = bytes([DecodeClass(self.decode_class)])
        if self.decode_class == DecodeClass.OTHER:
            if self.other_class is None:
                raise WireError("Other decode class needs a u16 value")
            tag += uint_le(self.other_class, 16, "other_class")
        elif self.other_class is not None:
            raise WireError("other_class applies only to the Other decode class")
        transform = b"\x00" if self.output_transform_id is None else b"\x01" + h256(self.output_transform_id)
        return (
            uint_le(1, 16, "version")
            + tag
            + h256(self.tokenizer_hash, "tokenizer_hash")
            + self.sampling.encode()
            + h256(self.stop_conditions_hash, "stop_conditions_hash")
            + transform
            + h256(self.class_policy_hash, "class_policy_hash")
        )

    def hash(self) -> bytes:
        return hashlib.sha256(DECODE_POLICY_DOMAIN + self.encode()).digest()


def h_ids(prompt_ids: list[int], generated_ids: list[int]) -> bytes:
    def encode(ids: list[int], name: str) -> bytes:
        return uint_le(len(ids), 32, f"{name} count") + b"".join(uint_le(item, 32, name) for item in ids)

    return blake2_256(encode(prompt_ids, "prompt id") + encode(generated_ids, "generated id"))


# F.3 transcript


class LeafVersion(IntEnum):
    V0 = 0
    V1 = 1
    V2 = 2
    V3 = 3


@dataclass(frozen=True)
class Turn:
    """One miner-signed transcript turn; fields absent from a version must stay at their zero value."""

    version: LeafVersion
    turn_index: int
    h_in: bytes
    h_out: bytes
    g_n: int
    decode_policy_hash: bytes | None = None
    h_ids: bytes = ZERO_HASH
    toploc_commitment_hash: bytes = ZERO_HASH
    miner_recv_ms: int = 0
    miner_done_ms: int = 0
    latency_ms: int = 0
    enclave_sig: bytes = b"\x00" * 64

    def check_shape(self) -> None:
        version = LeafVersion(self.version)
        has_policy = self.decode_policy_hash is not None
        has_v3_fields = self.h_ids != ZERO_HASH or self.toploc_commitment_hash != ZERO_HASH
        if version in (LeafVersion.V0, LeafVersion.V1) and (has_policy or has_v3_fields):
            raise WireError(f"{version.name} carries no decode policy and no V3 fields")
        if version == LeafVersion.V0 and (self.miner_recv_ms or self.miner_done_ms or self.latency_ms):
            raise WireError("V0 carries no timing fields")
        if version == LeafVersion.V2 and (not has_policy or has_v3_fields):
            raise WireError("V2 needs a decode policy and zero V3 fields")
        if version == LeafVersion.V3 and (not has_policy or self.h_ids == ZERO_HASH):
            raise WireError("V3 needs a decode policy and a non-zero h_ids")
        if len(self.enclave_sig) != 64:
            raise WireError("enclave signature must be 64 bytes")


def leaf_preimage(channel: bytes, turn: Turn) -> bytes:
    turn.check_shape()
    version = LeafVersion(turn.version)
    preimage = (
        h256(channel, "channel_id")
        + uint_le(turn.turn_index, 32, "turn_index")
        + h256(turn.h_in, "h_in")
        + h256(turn.h_out, "h_out")
        + uint_le(turn.g_n, 128, "g_n")
    )
    if version >= LeafVersion.V2:
        preimage += h256(turn.decode_policy_hash, "decode_policy_hash")
    if version == LeafVersion.V3:
        preimage += h256(turn.h_ids, "h_ids") + h256(turn.toploc_commitment_hash, "toploc_commitment_hash")
    if version >= LeafVersion.V1:
        preimage += (
            uint_le(turn.miner_recv_ms, 64, "miner_recv_ms")
            + uint_le(turn.miner_done_ms, 64, "miner_done_ms")
            + uint_le(turn.latency_ms, 64, "latency_ms")
        )
    return preimage


def leaf_hash(channel: bytes, turn: Turn) -> bytes:
    return blake2_256(leaf_preimage(channel, turn))


def merkle_node(left: bytes, right: bytes) -> bytes:
    return blake2_256(h256(left) + h256(right))


def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return ZERO_HASH
    level = [h256(leaf, "leaf") for leaf in leaves]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [merkle_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_path(leaves: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Siblings from leaf to root as (sibling_hash, sibling_is_left)."""
    if not 0 <= index < len(leaves):
        raise WireError("leaf index out of range")
    level, position, path = list(leaves), index, []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = position ^ 1
        path.append((level[sibling], sibling < position))
        level = [merkle_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        position //= 2
    return path


def root_from_path(leaf: bytes, path: list[tuple[bytes, bool]]) -> bytes:
    node = h256(leaf, "leaf")
    for sibling, sibling_is_left in path:
        node = merkle_node(sibling, node) if sibling_is_left else merkle_node(node, sibling)
    return node


def encode_verified_turn(turn: Turn, path: list[tuple[bytes, bool]]) -> bytes:
    """SCALE `VerifiedTurn`; absent policy, h_ids and TOPLOC fields encode as zero hashes."""
    turn.check_shape()
    body = (
        bytes([LeafVersion(turn.version)])
        + uint_le(turn.turn_index, 32, "turn_index")
        + h256(turn.h_in)
        + h256(turn.h_out)
        + uint_le(turn.g_n, 128, "g_n")
        + (turn.decode_policy_hash or ZERO_HASH)
        + turn.h_ids
        + turn.toploc_commitment_hash
        + uint_le(turn.miner_recv_ms, 64)
        + uint_le(turn.miner_done_ms, 64)
        + uint_le(turn.latency_ms, 64)
        + turn.enclave_sig
        + compact_u32(len(path))
    )
    for sibling, sibling_is_left in path:
        body += h256(sibling, "sibling") + (b"\x01" if sibling_is_left else b"\x00")
    return body


def decode_verified_turn(data: bytes) -> tuple[Turn, list[tuple[bytes, bool]]]:
    reader = Reader(data)
    try:
        version = LeafVersion(reader.take(1)[0])
    except ValueError as error:
        raise WireError("unknown leaf version") from error
    turn_index, h_in, h_out, g_n = reader.uint(32), reader.hash(), reader.hash(), reader.uint(128)
    policy, ids, toploc = reader.hash(), reader.hash(), reader.hash()
    recv, done, latency, signature = reader.uint(64), reader.uint(64), reader.uint(64), reader.take(64)
    path = [(reader.hash(), reader.boolean()) for _ in range(reader.compact_u32())]
    reader.finish()
    turn = Turn(
        version, turn_index, h_in, h_out, g_n,
        None if version < LeafVersion.V2 else policy, ids, toploc, recv, done, latency, signature,
    )
    if version < LeafVersion.V2 and policy != ZERO_HASH:
        raise WireError(f"{version.name} carries no decode policy")
    turn.check_shape()
    return turn, path


@dataclass(frozen=True)
class TurnAck:
    agent_send_ms: int
    agent_recv_ms: int
    agent_sig: bytes


def ack_message(channel: bytes, turn_index: int, leaf: bytes, agent_send_ms: int, agent_recv_ms: int) -> bytes:
    return (
        h256(channel, "channel_id")
        + uint_le(turn_index, 32, "turn_index")
        + h256(leaf, "leaf_hash")
        + uint_le(agent_send_ms, 64, "agent_send_ms")
        + uint_le(agent_recv_ms, 64, "agent_recv_ms")
    )


def receipt_message_v1(channel: bytes, final_root: bytes, aggregate_gn: int, payable: int) -> bytes:
    return (
        RECEIPT_DOMAIN
        + WIRE_VERSION_1
        + h256(channel, "channel_id")
        + h256(final_root, "final_root")
        + uint_le(aggregate_gn, 128, "aggregate_gn")
        + uint_le(payable, 128, "payable")
    )


def encode_transcript(channel: bytes, entries: list[tuple[Turn, TurnAck | None]]) -> bytes:
    blob = FCC4_MAGIC + h256(channel, "channel_id") + uint_le(len(entries), 32, "turn_count")
    for turn, ack in entries:
        turn.check_shape()
        blob += (
            bytes([LeafVersion(turn.version)])
            + uint_le(turn.turn_index, 32)
            + turn.h_in
            + turn.h_out
            + uint_le(turn.g_n, 128)
        )
        blob += b"\x00" if turn.decode_policy_hash is None else b"\x01" + h256(turn.decode_policy_hash)
        blob += (
            turn.h_ids
            + turn.toploc_commitment_hash
            + uint_le(turn.miner_recv_ms, 64)
            + uint_le(turn.miner_done_ms, 64)
            + uint_le(turn.latency_ms, 64)
            + turn.enclave_sig
        )
        if ack is None:
            blob += b"\x00"
        else:
            if len(ack.agent_sig) != 64:
                raise WireError("agent signature must be 64 bytes")
            blob += b"\x01" + uint_le(ack.agent_send_ms, 64) + uint_le(ack.agent_recv_ms, 64) + ack.agent_sig
    return blob


def decode_verified_turns(data: bytes) -> list[tuple[Turn, list[tuple[bytes, bool]]]]:
    """SCALE `Vec<VerifiedTurn>` as carried by `settle`."""
    reader = Reader(data)
    count = reader.compact_u32()
    if count > MAX_SETTLEMENT_TURNS:
        raise WireError("bundle declares more turns than a settlement allows")
    bundle = []
    for _ in range(count):
        path_start = reader.offset + 269
        if path_start > len(reader.data):
            raise WireError("truncated input")
        path_reader = Reader(reader.data[path_start:])
        path_length = path_reader.compact_u32()
        end = path_start + path_reader.offset + 33 * path_length
        bundle.append(decode_verified_turn(reader.data[reader.offset:end]))
        reader.offset = end
    reader.finish()
    return bundle


def decode_transcript(data: bytes) -> tuple[bytes, list[tuple[Turn, TurnAck | None]]]:
    reader = Reader(data)
    if reader.take(4) != FCC4_MAGIC:
        raise WireError("not an FCC4 transcript")
    channel, count = reader.hash(), reader.uint(32)
    if count > MAX_SETTLEMENT_TURNS:
        raise WireError("transcript declares more turns than a bundle allows")
    entries = []
    for _ in range(count):
        try:
            version = LeafVersion(reader.take(1)[0])
        except ValueError as error:
            raise WireError("unknown leaf version") from error
        turn_index, h_in, h_out, g_n = reader.uint(32), reader.hash(), reader.hash(), reader.uint(128)
        policy = reader.hash() if reader.boolean() else None
        ids, toploc = reader.hash(), reader.hash()
        recv, done, latency, signature = reader.uint(64), reader.uint(64), reader.uint(64), reader.take(64)
        ack = TurnAck(reader.uint(64), reader.uint(64), reader.take(64)) if reader.boolean() else None
        turn = Turn(version, turn_index, h_in, h_out, g_n, policy, ids, toploc, recv, done, latency, signature)
        turn.check_shape()
        entries.append((turn, ack))
    reader.finish()
    return channel, entries


# sr25519 signatures


def _sr25519():
    try:
        import sr25519
    except ImportError as error:
        raise SessionError(
            "sr25519 signatures need py-sr25519-bindings: pip install -r requirements-session.txt"
        ) from error
    return sr25519


def sr25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        return bool(_sr25519().verify(bytes(signature), bytes(message), bytes(public_key)))
    except (ValueError, TypeError):
        return False


class SessionKey:
    """An sr25519 keypair for one channel's `agent_key`; distinct from the account that funds it."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise SessionError("session key seed must be 32 bytes")
        self.public_key, self._secret = _sr25519().pair_from_seed(bytes(seed))

    @classmethod
    def generate(cls) -> "SessionKey":
        return cls(os.urandom(32))

    def sign(self, message: bytes) -> bytes:
        return _sr25519().sign((self.public_key, self._secret), bytes(message))


# Agent session


def check_leaf_cutoff(turn: Turn, pinned_policy: bytes | None) -> None:
    """Accepted-version cutoff: a channel with a pinned decode policy takes only V2/V3 with that policy."""
    if pinned_policy is None:
        return
    if turn.version not in (LeafVersion.V2, LeafVersion.V3):
        raise SessionError("unsupported leaf version: a policy-bound channel accepts only V2 and V3 leaves")
    if turn.decode_policy_hash != pinned_policy:
        raise SessionError("turn decode policy differs from the channel's pinned policy")


@dataclass
class AgentSession:
    """The agent's view of one open channel: accept miner turns, countersign them, sign the receipt."""

    channel: bytes
    agent_key: SessionKey
    miner_session_key: bytes
    decode_policy_hash: bytes | None
    policy_marker: bool = True
    leaves: list[bytes] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    acks: list[TurnAck] = field(default_factory=list)

    def accept_turn(self, turn: Turn, agent_send_ms: int, agent_recv_ms: int) -> TurnAck:
        """Verify one turn before using its output, then countersign it (R12.1b)."""
        if len(self.turns) >= MAX_SETTLEMENT_TURNS:
            raise SessionError("channel reached the settlement turn limit")
        if turn.turn_index != len(self.turns):
            raise SessionError(f"expected turn {len(self.turns)}, got {turn.turn_index}")
        check_leaf_cutoff(turn, self.decode_policy_hash if self.policy_marker else None)
        if turn.miner_done_ms < turn.miner_recv_ms:
            raise SessionError("miner reported finishing before receiving the request")
        leaf = leaf_hash(self.channel, turn)
        if not sr25519_verify(self.miner_session_key, leaf, turn.enclave_sig):
            raise SessionError("miner leaf signature does not verify under the channel session key")
        ack = TurnAck(
            agent_send_ms,
            agent_recv_ms,
            self.agent_key.sign(ack_message(self.channel, turn.turn_index, leaf, agent_send_ms, agent_recv_ms)),
        )
        self.turns.append(turn)
        self.leaves.append(leaf)
        self.acks.append(ack)
        return ack

    def final_root(self) -> bytes:
        return merkle_root(self.leaves)

    def aggregate_gn(self) -> int:
        total = sum(turn.g_n for turn in self.turns)
        uint_le(total, 128, "aggregate_gn")
        return total

    def sign_receipt(self, payable: int) -> bytes:
        return self.agent_key.sign(receipt_message_v1(self.channel, self.final_root(), self.aggregate_gn(), payable))

    def transcript(self) -> bytes:
        return encode_transcript(self.channel, list(zip(self.turns, self.acks)))


def verify_settlement_bundle(
    channel: bytes,
    miner_session_key: bytes,
    bundle: list[tuple[Turn, list[tuple[bytes, bool]]]],
    final_root: bytes,
    aggregate_gn: int,
    pinned_policy: bytes | None,
) -> None:
    """Check a miner's settle arguments: version cutoff, signatures, Merkle paths, distinct turns, aggregate."""
    if len(bundle) > MAX_SETTLEMENT_TURNS:
        raise SessionError("bundle exceeds the settlement turn limit")
    seen = set()
    for turn, _ in bundle:
        if turn.turn_index in seen:
            raise SessionError(f"duplicate turn index {turn.turn_index}")
        seen.add(turn.turn_index)
    total = 0
    for turn, path in bundle:
        check_leaf_cutoff(turn, pinned_policy)
        leaf = leaf_hash(channel, turn)
        if not sr25519_verify(miner_session_key, leaf, turn.enclave_sig):
            raise SessionError(f"turn {turn.turn_index} signature does not verify")
        if root_from_path(leaf, path) != final_root:
            raise SessionError(f"turn {turn.turn_index} Merkle path does not reach the final root")
        total += turn.g_n
    if total != aggregate_gn:
        raise SessionError(f"aggregate_gn {aggregate_gn} does not equal the checked sum {total}")


# R12.1d settlement arithmetic


@dataclass(frozen=True)
class SettlementProfile:
    """How a payable amount divides. The miner receives the remainder after the validator leg and audit
    earmark, each floored. Amounts are base units (10^-18 FLOP)."""

    name: str
    validator_ppm: int
    audit_ppm: int
    phi_percent: int
    base_per_turn: int = 1
    rate_g: int = 1


PROFILES = {
    # FLOP's money-path v1 compute-channel fixtures: gross accounting, zero audit carve, 20% penalty.
    "money-path-v1": SettlementProfile("money-path-v1", validator_ppm=0, audit_ppm=0, phi_percent=20),
    # Observed on devnet 2026-10-27 (channel 0xdcad..e239): the miner was paid the escrow less floor(1%).
    "devnet-observed": SettlementProfile("devnet-observed", validator_ppm=0, audit_ppm=10_000, phi_percent=20),
    # Yellow Paper v0.5 R12.1a: 85% miner, 15% validators of which 1% funds the audit earmark.
    # The published text does not fix rounding; treat this profile's amounts as estimates.
    "spec-v0.5": SettlementProfile("spec-v0.5", validator_ppm=140_000, audit_ppm=10_000, phi_percent=20),
}


@dataclass(frozen=True)
class SettlementOutcome:
    payable: int
    miner: int
    validators: int
    audit: int
    agent_refund: int
    penalty: int

    @property
    def total(self) -> int:
        return self.miner + self.validators + self.audit + self.agent_refund + self.penalty


def split_payable(payable: int, profile: SettlementProfile) -> tuple[int, int, int]:
    if profile.validator_ppm + profile.audit_ppm > 1_000_000:
        raise SessionError("profile shares exceed the payable amount")
    validators = payable * profile.validator_ppm // 1_000_000
    audit = payable * profile.audit_ppm // 1_000_000
    return payable - validators - audit, validators, audit


def cooperative_settlement(escrow: int, profile: SettlementProfile) -> SettlementOutcome:
    """Cooperative `settle`: the full reserved escrow is payable; under-use is not refunded (R12.1a)."""
    uint_le(escrow, 128, "escrow")
    miner, validators, audit = split_payable(escrow, profile)
    return SettlementOutcome(escrow, miner, validators, audit, 0, 0)


def force_tariff(turns: int, aggregate_gn: int, profile: SettlementProfile) -> int:
    return profile.base_per_turn * turns + profile.rate_g * aggregate_gn


def force_finalize(escrow: int, turns: int, aggregate_gn: int, profile: SettlementProfile) -> SettlementOutcome:
    """`force_settle` then `finalize`: tariff P to the payable split, (1-phi)(E-P) refunded, phi(E-P) penalty."""
    uint_le(escrow, 128, "escrow")
    payable = force_tariff(turns, aggregate_gn, profile)
    if payable > escrow:
        raise TariffExceedsEscrow(f"tariff {payable} exceeds escrow {escrow}")
    unused = escrow - payable
    penalty = unused * profile.phi_percent // 100
    miner, validators, audit = split_payable(payable, profile)
    return SettlementOutcome(payable, miner, validators, audit, unused - penalty, penalty)


def timeout_refund(escrow: int) -> SettlementOutcome:
    """Non-delivery `timeout` or `expire_force_open`: the whole escrow returns to the agent (phi = 0)."""
    uint_le(escrow, 128, "escrow")
    return SettlementOutcome(0, 0, 0, 0, escrow, 0)
