"""Diagnose the Gemini setup — run:  python scripts/gemini_check.py

Loads GEMINI_API_KEY the same way the app does (via config/.env), then:
  1. sanity-checks the key format,
  2. calls ListModels (proves the key authenticates),
  3. tries a tiny generateContent on the configured model.

It prints the raw API messages so you can tell an INVALID KEY (403/"API key
not valid") apart from a WRONG MODEL NAME (404) — the two look similar in the
main run's logs.
"""

from __future__ import annotations

import sys

import requests

from common import config


def main() -> None:
    sc = config.scoring()["gemini"]
    key = config.env(sc["api_key_env"])
    if not key:
        sys.exit("GEMINI_API_KEY is not set (check config/.env).")

    # Gemini keys come in a few formats ('AIza...', 'AQ....', etc.). Rather than
    # guess from the prefix, the ListModels call below is the real auth test.
    print(f"Key prefix : {key[:4]}...  (length {len(key)})")

    # 1. ListModels — proves authentication.
    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key}, timeout=30)
    print(f"\nListModels -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:400])
        print("\n→ Authentication failed. Fix the key before anything else.")
        return

    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    print(f"Models available to this key ({len(names)}):")
    for n in sorted(names):
        print(f"   {n}")

    configured = sc["model"]
    print(f"\nConfigured model (scoring.yaml): {configured}")
    if configured not in names:
        print(f"  ⚠ '{configured}' is NOT in the list above.")
        flash = [n for n in names if "flash" in n]
        if flash:
            print(f"    Set scoring.yaml -> gemini.model to one of: "
                  f"{', '.join(sorted(flash))}")

    # 2. Tiny generateContent test.
    endpoint = sc["endpoint"].format(model=configured)
    r2 = requests.post(endpoint, params={"key": key},
                       json={"contents": [{"parts": [{"text": "Reply with OK"}]}]},
                       timeout=30)
    print(f"\ngenerateContent({configured}) -> HTTP {r2.status_code}")
    if r2.status_code == 200:
        print("  ✓ Scoring will work.")
    else:
        print(r2.text[:400])


if __name__ == "__main__":
    main()
