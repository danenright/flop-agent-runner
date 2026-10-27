# FLOP agent runner

A small, stdlib-plus-`cryptography` tool that makes a Technocore `did:key` into a FLOP network agent:

- **draws** the devnet faucet;
- **enrolls** with the FLOP gateway by signing a strictly checked challenge with the DID key;
- **spends** on real inference tasks, with per-request and per-cycle caps;
- **confirms** each spend's settlement on chain;
- **checks**, for any DID or address with no key needed, how much settled inference spend the chain credits to that account.

> **Read [FINDINGS.md](FINDINGS.md) first.** On devnet, spend through the FLOP gateway settles on chain but is paid by, and attributed to, the gateway's **broker** account. The requesting agent's account shows **0** inference spend. FLOP scores the agent airdrop on settled compute-channel spend, so if you rely on the gateway you can expect no agent credit. Run `credit` on your own DID to see where you stand.

Community tool; not affiliated with FLOP Labs. Devnet earns nothing. Nothing here guarantees any allocation.

## Requirements

- Python 3.11 or newer, and `pip install -r requirements.txt` (pinned `cryptography`)
- A Technocore identity file, `~/.config/technocore/agent.env` holding `SIGN_SEED=<64 hex>`. Any 32-byte Ed25519 seed works; see [technocore-contributor-onboarding](https://github.com/danenright/technocore-contributor-onboarding) to create one.

The DID's Ed25519 public key **is** the FLOP account (SS58 prefix 42), the same binding FLOP's own [`tclk` FLOP rail](https://github.com/flop-labs/tclk/tree/main/flop) uses. There is no second wallet.

## Am I credited? (no key needed)

```bash
python3 flop_agent.py credit did:key:z6Mk...
python3 flop_agent.py credit 5G4yAhvQFhDnt5gtyLKEg2yLK7KWGghMByWw9zZvzBJvQcC4
```

The output includes:

- the account's balance and **settled inference spend**, from FLOP's explorer;
- the account's own recent compute channels;
- a one-line verdict.

## Run one devnet cycle

```bash
python3 flop_agent.py address   # DID and FLOP account; offline
python3 flop_agent.py status    # pins identity and chain; shows ledger state
python3 flop_agent.py fund      # quick faucet grant (25 FLOP, once per 24 h per address)
python3 flop_agent.py enroll    # gateway key; signs only a statement naming this gateway, account and chain
python3 flop_agent.py spend --prompt "List three risks of a Friday database migration."
python3 flop_agent.py settle    # on-chain settlement: payer, miner paid, block, extrinsic
```

For unattended use, put tasks in a queue folder and run one cycle a day:

```bash
mkdir -p ~/flop-queue && cp examples/queue/*.json ~/flop-queue/
python3 flop_agent.py run --queue ~/flop-queue --max-tasks 5 --budget-flop 20
```

Each cycle does the following:

1. **Funds** from the faucet unless `--fund none`.
2. **Spends** on queued tasks:
   - each request is priced first with `/models/estimate` and refused if its worst case exceeds `--max-cost-flop`;
   - the cycle stops before `--budget-flop` would be exceeded;
   - finished files move to `done/`, and tasks it can't run move to `skipped/`.
3. **Settles:** checks every recorded channel on the explorer.

Task files are `{"title": "...", "prompt": "..."}`. Model output is data: it is written to a private results file and never executed. Never queue secrets, because prompts go to a third-party miner.

### Costs on devnet

- **Pricing:** the gateway prices a request's worst case as `max_tokens` × 0.006 FLOP, so 3.07 FLOP for the default 512 tokens. The default per-request cap is 4 FLOP.
- **What's paid:** the miner was paid almost the full reserved escrow, not the metered `G_n` cost (see FINDINGS.md).

## Backends

| Backend | Path | Who pays the channel |
|---|---|---|
| `gateway` (default) | Faucet, wallet enrollment, then FLOP's OpenAI-compatible gateway | **The gateway's broker** (not credited to you on devnet) |
| `cli` | FLOP's direct-session CLI (`flop session run`) with your keystore. The CLI is in operator packs and not yet public. | **Your account** (the spend FLOP's agent scoring reads) |

The `cli` adapter calls the documented command and fails closed if it does not recognise the JSON result. `--exclude-miner` refuses miners under your control, because spend to your own miner is not credited. A direct session pays its escrow, so the `cli` default cap is 0.01 FLOP.

```bash
export FLOP_CLI='docker run --rm -v $HOME/.flop:/root/.flop <operator-supplied-image@sha256:digest>'
export FLOP_KEY_PASSWORD=...
python3 flop_agent.py --address <keystore-ss58> run --queue ~/flop-queue --backend cli \
  --miner <independent-miner-ss58> --model qwen/qwen2.5-0.5b-instruct --exclude-miner <your-miner-ss58>
```

## Optional: Technocore Parcel tasks

A queued file can also be a private [Technocore Parcel](https://github.com/danenright/technocore-parcel) capability file. The runner then:

1. claims the parcel with your DID;
2. sends its instructions as the prompt;
3. posts the reply back as a DID-signed result, capped at 2,800 characters.

Parcel support is loaded only when such a file appears:

```bash
python3 flop_agent.py --technocore-parcel ~/src/technocore-parcel run --queue ~/flop-queue
# or: export TECHNOCORE_PARCEL=~/src/technocore-parcel
```

## Testnet

`--network testnet` refuses to run until you pass FLOP's published `--faucet-url`, `--gateway-url` and `--expected-genesis`, plus `--explorer-url` for settlement and credit checks. It never guesses a chain.

## State and evidence

State lives in `~/.local/state/flop-agent-runner/<network>/`, or in the legacy `~/.local/state/technocore-parcel/flop-agent/` if that exists. You can override it with `FLOP_AGENT_STATE_DIR` or `--state-dir`. Everything is mode `600`:

| File | Contents |
|---|---|
| `identity.json` | Pins one DID, one account and one genesis. A different identity is refused. A new genesis needs `--accept-new-genesis`. |
| `credential.json` | The gateway bearer key. It never enters the ledger or evidence. |
| `ledger.jsonl` | Fund, enroll, spend and settlement records. Prompts and replies are stored as SHA-256 only. |
| `results/` | Full prompt and reply per task |

To export public evidence:

```bash
python3 flop_agent.py evidence --output evidence/my-devnet.json
```

The export contains:

- DID, account and genesis;
- faucet transactions;
- per-spend request and channel IDs, miner, and metered cost;
- on-chain settlement: payer, whether you paid, miner paid, block and extrinsic;
- totals for settled spend and spend paid by this account, plus the explorer's spend figure for the account.

It contains no bearer key, prompt or reply text.

## Safety rules the runner enforces

- One DID and one account per state directory. Wallets under common control are scored as one participant anyway.
- It never signs an enrollment statement for another gateway, account, chain or action.
- Every request has a cost cap, and every cycle a budget.
- Your own miners can be excluded before a direct session and after a brokered one.
- Model output is never executed. Parcel results are capped.

## Develop

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

## Durable DID attribution

[`ATTESTATION.json`](ATTESTATION.json) is a signature by `did:key:z6MkrNkU2iHvF1YAM7JQxgzU8a8YgB6QGCCKBFzQbRmpZ1GM` binding this repository to commit `0a1c880d5135d2a53f64a2b2e24b6a2a6658e9e9`. CI checks it with the onboarding repository's checksum-pinned verifier:

```bash
git clone https://github.com/danenright/technocore-contributor-onboarding
python3 technocore-contributor-onboarding/verify_attestation.py ATTESTATION.json
```

## History

This runner started inside [`technocore-parcel`](https://github.com/danenright/technocore-parcel) and moved here with its commit history intact.

## License

Apache-2.0. Community integration, not an official FLOP Labs protocol or reward mechanism.
