app_name = "stripe_integration"
app_title = "Stripe Integration"
app_publisher = "Falck"
app_description = "Stripe Checkout + webhooks for ERPNext"
app_email = "ops@example.com"
app_license = "MIT"

# Add "Request Payment (Stripe)" button on Sales Invoice
doctype_js = {
    "Sales Invoice": "public/js/sales_invoice_stripe.js",
}


doc_events = {}

doc_events.update({"Subscription": {"on_update": "stripe_integration.stripe_integration.subscription_sync.on_subscription_update"}})
