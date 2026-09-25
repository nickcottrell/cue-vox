"""Mint the sidecar's per-boot identity: a pairing TOKEN and a signed ATTESTATION.

Run once at launch (by cbx-service.sh, in the cue-vox venv which has cryptography). Writes
two files into $MAESTRO_ROOT/.claude/runtime/:

  cbx.token        a random per-boot secret (0600). The sidecar requires it on /synth;
                   cue-vox reads the same file and sends it. A rogue process squatting :8123
                   without the token is rejected -- runtime proof of "the valid one".

  cbx-attest.json  {fingerprint, signature, signed_by, pubkey_fp, ...}. The fingerprint is a
                   sha256 over the sidecar CODE + every reference clip + the model id. It is
                   SIGNED with the human Ed25519 key -- only the key-holder could mint it, and
                   anyone with human.pub can verify it. This is integrity + provable provenance
                   (the honest scope: the sidecar holds no secret; this proves it is the
                   authorized, untampered build).

Dumb-by-design: computed once, written flat. The sidecar just serves the file; it does no
crypto. cue-vox verifies the signature (and can recompute the fingerprint) at runtime.
"""
import hashlib
import json
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("MAESTRO_ROOT", os.path.abspath(os.path.join(HERE, "..", "..")))
RUNTIME = os.path.join(ROOT, ".claude", "runtime")
MODELS = os.path.join(HERE, "models")
sys.path.insert(0, os.path.join(ROOT, "cue-mem", "lib"))
os.environ.setdefault("CUE_MEM_KEYS_DIR", os.path.join(ROOT, ".claude", "keys"))
import signing  # noqa: E402


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ref_hashes():
    """sha256 of every reference clip the sidecar can clone (default set + per-voice dirs)."""
    out = {}
    if os.path.isdir(MODELS):
        for name in sorted(os.listdir(MODELS)):
            p = os.path.join(MODELS, name)
            if name.endswith(".wav"):
                out[name] = _sha(p)
            elif os.path.isdir(p):                # per-voice ref dir? no -- refs are flat wavs
                pass
    return out


def build_fingerprint():
    """The stable BUILD identity: sha256 over code + every ref + model id. No timestamp, so
    cue-vox can recompute it from the same files and confirm the running sidecar matches."""
    payload = {
        "code_sha": _sha(os.path.join(HERE, "chatterbox_server.py")),
        "refs": _ref_hashes(),
        "model": "chatterbox-tts",
    }
    payload["ref_count"] = len(payload["refs"])
    canon = json.dumps({k: payload[k] for k in ("code_sha", "refs", "model")},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest(), payload


def build_attestation(built_at):
    fingerprint, payload = build_fingerprint()
    signature = signing.sign_bytes(fingerprint)
    return {
        "fingerprint": fingerprint,
        "signature": signature,                    # None if no key on this host
        "signed_by": "human:ed25519" if signature else "",
        "pubkey_fp": signing.get_public_key_fingerprint(),
        "built_at": built_at,                      # metadata only -- NOT in the fingerprint
        "payload": payload,
    }


def main():
    os.makedirs(RUNTIME, exist_ok=True)
    built_at = os.environ.get("CBX_BUILT_AT") or str(int(time.time()))

    token = secrets.token_hex(32)
    tok_path = os.path.join(RUNTIME, "cbx.token")
    with open(tok_path, "w") as f:
        f.write(token)
    os.chmod(tok_path, 0o600)

    att = build_attestation(built_at)
    att_path = os.path.join(RUNTIME, "cbx-attest.json")
    with open(att_path, "w") as f:
        json.dump(att, f, indent=2)
    os.chmod(att_path, 0o600)

    # Emit the token on stdout so the launcher can export it to the sidecar's env.
    print(token)
    print("[cbx-attest] token + attestation minted (fp=%s signed=%s refs=%d)"
          % (att["fingerprint"][:12], bool(att["signature"]), att["payload"]["ref_count"]),
          file=sys.stderr)


if __name__ == "__main__":
    main()
