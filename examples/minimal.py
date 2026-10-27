from pypay import CreatePaymentRequest, PaymentClient

client = PaymentClient.from_env()
result = client.create_payment(
    CreatePaymentRequest(
        out_trade_no="ORDER-DEMO-0001",
        amount_fen=1,
        description="pypay minimal example",
    )
)
print(f"channel: {result.channel.value}")
print(f"status: {result.status.value}")
print(f"qr_code_url: {result.qr_code_url}")
print(f"form_html: {result.form_html}")
