#!/usr/bin/env python3
"""Mint the OAuth token for a channel WITHOUT auto-opening a browser.

    python reauth_nobrowser.py channel_1

Prints an authorization URL. Open it in a browser that is signed in as the
channel's Gmail, click through "Advanced -> Go to app (unsafe)" (the app is
unverified -- expected), and approve. The script catches the redirect on
localhost and writes tokens/<channel>_token.json.

Verify afterwards that the JSON contains a non-empty "refresh_token".
"""
from __future__ import annotations

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from src.config import get_channel

# youtube.upload = insert videos; youtube.force-ssl = also set custom thumbnails.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    channel = get_channel(sys.argv[1])
    client_secret = channel.abspath(channel.google_credentials_file)
    token_path = channel.abspath(channel.oauth_token_file)

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes=SCOPES)

    # open_browser=False -> just prints the URL and waits for the localhost redirect.
    creds = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=False,
        access_type="offline",
        prompt="consent",           # force a fresh refresh_token every time
        authorization_prompt_message=(
            "\n>>> Open this URL in a browser signed in as {url_placeholder}\n"
            ">>> (Advanced -> Go to app -> Continue):\n\n{url}\n"
        ).replace("{url_placeholder}", channel.owner_email),
    )

    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }
    with open(token_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    if not creds.refresh_token:
        print("\n!!! No refresh_token in the response. Delete the token file and "
              "re-run -- make sure you did not previously grant this app.")
        return 1
    print(f"\nOK -> {token_path}  (refresh_token present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
