#!/usr/bin/env python3
"""
Google token refresh helper.

Run this script whenever the access token expires (~1 hour).
It tries to auto-refresh using a client_secret (if available),
or prints the exact OAuth Playground URL to get a fresh token in 30 seconds.

Usage:
    python3 scripts/refresh_google_token.py                  # auto or guided
    python3 scripts/refresh_google_token.py --set <token>    # paste new token
    python3 scripts/refresh_google_token.py --status         # check expiry
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

TOKEN_FILE = Path("data/.google_token.json")
DOC_URL = "https://docs.google.com/document/d/1ZcLBIM7rEY3z-e8AQxibxijHvf5FajNeEUyvFT3RLTk/edit"


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        return {}
    return json.loads(TOKEN_FILE.read_text())


def save_token(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


def check_token_status(token: str) -> dict:
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v1/tokeninfo",
        params={"access_token": token},
    )
    return resp.json()


def try_refresh(data: dict) -> bool:
    """Attempt to get a new access token using the refresh token + client secret."""
    refresh_token = data.get("refresh_token", "")
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")

    if not (refresh_token and client_id and client_secret):
        return False

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    result = resp.json()
    if "access_token" in result:
        data["access_token"] = result["access_token"]
        save_token(data)
        print(f"✅  Token auto-refreshed. Expires in {result.get('expires_in', '?')}s")
        return True
    print(f"⚠️  Auto-refresh failed: {result.get('error_description', result)}")
    return False


def guided_refresh(data: dict) -> None:
    """Print step-by-step instructions to get a new token from OAuth Playground."""
    print()
    print("━" * 60)
    print("  GET A FRESH GOOGLE ACCESS TOKEN (30 seconds)")
    print("━" * 60)
    print()
    print("1. Open this URL in your browser:")
    print()
    print("   https://developers.google.com/oauthplayground/")
    print()
    print("2. In Step 1 (left panel), paste these scopes:")
    print("     https://www.googleapis.com/auth/documents")
    print("     https://www.googleapis.com/auth/drive.file")
    print("   → Click 'Authorize APIs' → sign in → Allow")
    print()
    print("3. In Step 2, click 'Exchange authorization code for tokens'")
    print()
    print("4. Copy the Access Token shown")
    print()
    print("5. Run:")
    print("     python3 scripts/refresh_google_token.py --set <paste-token>")
    print()
    print("   OR paste it at the prompt below:")
    print()
    new_token = input("  Paste access token (or press Enter to skip): ").strip()
    if new_token:
        set_token(new_token, data)


def set_token(new_token: str, data: dict | None = None) -> None:
    """Update the access token in the file."""
    if data is None:
        data = load_token()
    data["access_token"] = new_token
    save_token(data)

    # Verify
    info = check_token_status(new_token)
    if "error" in info:
        print(f"⚠️  Token appears invalid: {info}")
    else:
        expires = int(info.get("expires_in", 0))
        print(f"✅  Token saved! Expires in {expires}s ({expires//60}m)")
        print(f"    Scope: {info.get('scope', '')}")
        print(f"    Doc:   {DOC_URL}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google token refresh helper")
    parser.add_argument("--set", metavar="TOKEN", help="Set a new access token")
    parser.add_argument("--status", action="store_true", help="Check current token status")
    args = parser.parse_args()

    data = load_token()

    if args.set:
        set_token(args.set, data)
        return

    current_token = data.get("access_token", "")

    if args.status:
        if not current_token:
            print("No token on file.")
            return
        info = check_token_status(current_token)
        if "error" in info:
            print("❌  Token EXPIRED or invalid")
        else:
            expires = int(info.get("expires_in", 0))
            print(f"✅  Token VALID — {expires}s remaining ({expires//60}m {expires%60}s)")
        return

    # Auto mode: check status → try refresh → guided flow
    if current_token:
        info = check_token_status(current_token)
        if "error" not in info:
            expires = int(info.get("expires_in", 0))
            if expires > 300:
                print(f"✅  Token still valid ({expires//60}m {expires%60}s remaining). Nothing to do.")
                return
            print(f"⚠️  Token expiring soon ({expires}s). Refreshing...")

    # Try auto-refresh
    if try_refresh(data):
        return

    # Guided refresh
    print("ℹ️  No client_secret available for auto-refresh.")
    guided_refresh(data)


if __name__ == "__main__":
    main()
