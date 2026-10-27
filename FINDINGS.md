# Finding: FLOP gateway spend is credited to the broker, not your agent

**Observed:** 27 October 2026 UTC, FLOP devnet (genesis `0x99c172c0…2d631`). Devnet earns nothing; this records how spend is attributed, not an allocation.

## Why it matters

FLOP's published agent airdrop rule is pro rata to **compute purchased in settled sessions**. The ratified yellowpaper decision D-0514 says agent scoring "MUST derive from settled compute-channel spend". So what matters is whose account opens and pays each compute channel.

The gateway at `api.flop.network` is the easiest way for an agent to buy FLOP inference today, and community guides tell agents to "spend testnet FLOP on inference". On devnet, every channel the gateway opens is paid by the gateway's broker account, not by the agent that sent the request.

## Evidence

One request was made with [`flop_agent.py`](flop_agent.py), after a faucet grant and a signed wallet enrollment. Everything below is public and can be rechecked without a key:

| | Value |
|---|---|
| Agent DID | `did:key:z6MkrNkU2iHvF1YAM7JQxgzU8a8YgB6QGCCKBFzQbRmpZ1GM` |
| Agent account (the same Ed25519 key) | `5G4yAhvQFhDnt5gtyLKEg2yLK7KWGghMByWw9zZvzBJvQcC4` |
| Faucet grant | 25 FLOP, transaction `0x186e41113fcb9f443b619d9065a05dc272a4e236c5ecdc158da0e1380c4b1292` |
| Gateway request | `gen_bbcd0b79-68f7-4536-94a0-73969b8d087e`: 377 completion tokens, receipt `aggregateGn` 390 at 0.003 FLOP/G_n (1.17 FLOP metered) |
| Compute channel | [`0xdcad92337e1813ddcf0be9ef9dba60b8a3ed957516d8f30152a77b1ec6abe239`](https://explorer.devnet.flop.network/api/compute-channels/0xdcad92337e1813ddcf0be9ef9dba60b8a3ed957516d8f30152a77b1ec6abe239) |
| Channel `agent` (payer) | **`5GsXb3TmNGixuEsbtixqHnqWr9UQJ2M8bkqBQWteNekgF98S`**, the gateway broker |
| Settlement | `settled` / `PAID`, cooperative. Block 2,200,785, extrinsic `0xb31d00aa…801645`. Escrow 3.168 FLOP, miner paid **3.13632 FLOP**. |
| Agent account's inference spend | [**0 FLOP, 0 channels**](https://explorer.devnet.flop.network/api/accounts/5G4yAhvQFhDnt5gtyLKEg2yLK7KWGghMByWw9zZvzBJvQcC4/inference-spend) |
| Broker account's inference spend | 17,426 FLOP over 149 settled channels; the channel above is among them |

The capability-free export is [`evidence/devnet-2026-10-27.json`](evidence/devnet-2026-10-27.json).

Two side observations:

- **Cooperative settlement paid the miner almost the whole reserved escrow** (3.136 of 3.168 FLOP), not the metered 1.17 FLOP. That matches the yellowpaper: "Escrow at `open_channel` **is** the payment for reserved capacity; under-use **MUST NOT** be refunded".
- The gateway enrollment response granted a **20,000 FLOP** allowance, not the 25 FLOP its OpenAPI description states.

## Check any account yourself

```bash
python3 flop_agent.py credit did:key:z6Mk...        # or an SS58 address; no key needed
```

The command maps a `did:key` to its account (the DID's Ed25519 key is the account) and reads the explorer's settled inference spend and that account's channels.

## What would be credited

A **direct session**: the agent's own account signs `open_channel` or `force_open`, countersigns each turn and the settlement receipt, and the channel settles. That needs one of:

- FLOP's direct-session SDK or CLI, documented but not yet public;
- miners publishing session offers and public endpoints (none on devnet today);
- the gateway's own-funds payment proof (x402), which its spec says "is not enabled".

## Limits

- **This is devnet.** Testnet may route, attribute or score differently, and FLOP has not published how brokered spend is treated.
- **The explorer is FLOP's indexer, not a light client.** Attribution here comes from its `compute-channels` records (the `agent` field) and its per-account `inference-spend` totals. It was not read directly from chain storage.
- This is one request. The broker figures show the same pattern across that account's 149 settled channels.

If you know of a published FLOP statement that brokered spend is credited to the requesting agent, please open an issue. This note will be corrected.
