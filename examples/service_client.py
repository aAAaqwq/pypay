"""Minimal business-side client example.

Required environment variables:
  PYPAY_BASE_URL      e.g. https://pay.example.com
  PYPAY_KEY_ID        project API key id
  PYPAY_API_SECRET    project API secret

This example creates a payment, prints the unified cashier URL, and queries it.
Run it with the project's test or sandbox credentials only.
"""

from __future__ import annotations

import os

from pypay_service.client import PayServiceClient


def main() -> None:
    base_url = os.environ["PYPAY_BASE_URL"]
    key_id = os.environ["PYPAY_KEY_ID"]
    secret = os.environ["PYPAY_API_SECRET"]
    with PayServiceClient(base_url, key_id, secret) as client:
        payment = client.create_payment(
            business_order_no="ORDER-20261029-001",
            subject="Super-AIGC 额度包",
            amount_fen=9900,
            expires_in_seconds=1800,
            return_url="https://www.example.com/pay/success",
            metadata={"user_ref": "user-123", "plan": "pro"},
        )
        print("payment_id:", payment["payment_id"])
        print("cashier_url:", payment["cashier_url"])
        print("created:", payment["created"])
        print("query:", client.get_payment(payment["payment_id"])["status"])


if __name__ == "__main__":
    main()
