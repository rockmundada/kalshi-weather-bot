#!/usr/bin/env python3
"""
Test Kalshi API authentication (balance endpoint).
Run this to verify your API key and private key are correct.

Common cause of INCORRECT_API_KEY_SIGNATURE:
  The API Key ID and the private key file must be from the SAME key pair.
  Create a new key at Kalshi → Account & security → API Keys → Create Key,
  then set KALSHI_API_KEY to the new Key ID and save the downloaded .key
  file as the path in KALSHI_PRIVATE_KEY_PATH (e.g. ~/kalshi_private_key.pem).
"""
import sys
from config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH, KALSHI_BASE_URL
from trading.kalshi_api import KalshiAPI


def main():
    print("Kalshi auth check")
    print(f"  Base URL: {KALSHI_BASE_URL}")
    print(f"  API Key:  {KALSHI_API_KEY[:8]}... (len={len(KALSHI_API_KEY)})")
    print(f"  Key file: {KALSHI_PRIVATE_KEY_PATH}")
    api = KalshiAPI()
    if api.private_key is None:
        print("FAIL: Could not load private key. Check path and file format (PEM).")
        sys.exit(1)
    print("  Private key: loaded")
    data = api._request("GET", "/portfolio/balance")
    if data is None:
        print("FAIL: 401 Unauthorized / INCORRECT_API_KEY_SIGNATURE")
        print("  → Use the API Key ID and private key from the SAME Kalshi 'Create Key'.")
        print("  → Re-create a key at Kalshi and update config + key file.")
        sys.exit(1)
    balance = data.get("balance", 0) / 100.0
    print(f"OK: Balance = ${balance:.2f}")
    sys.exit(0)


if __name__ == "__main__":
    main()
