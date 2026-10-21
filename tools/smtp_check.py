"""Check AlgoSage SMTP settings without printing credentials.

Usage:
    .venv/bin/python tools/smtp_check.py --to you@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from algosage.config import _load_dotenv  # noqa: E402
from algosage.emailer import (  # noqa: E402
    EmailMessage,
    email_sender_from_env,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send an AlgoSage SMTP delivery check")
    parser.add_argument("--to", required=True, help="recipient email address")
    args = parser.parse_args()

    _load_dotenv()
    sender = email_sender_from_env()
    if type(sender).__name__ != "SMTPEmailSender":
        print("SAGE_EMAIL_SENDER must be 'smtp' for this check", file=sys.stderr)
        return 2

    sender.send(
        EmailMessage(
            to_address=args.to,
            subject="AlgoSage SMTP check",
            text_body=(
                "This is a delivery check from AlgoSage. "
                "No verification code or account data is included."
            ),
        )
    )
    print("SMTP delivery check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
