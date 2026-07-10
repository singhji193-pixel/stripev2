app_name = "stripe_integration"
app_title = "Stripe Integration"
app_publisher = "Falck"
app_description = "Stripe Checkout + webhooks for ERPNext"
app_email = "ops@example.com"
app_license = "MIT"

# Add "Request Payment (Stripe)" button on Sales Invoice
doctype_js = {
    "Sales Invoice": "public/js/sales_invoice_stripe.js",
    "Subscription": "public/js/subscription_stripe.js",
}


doc_events = {}

doc_events.update({
    "Subscription": {
        "validate": "stripe_integration.stripe_integration.company_guard.validate_subscription_company_scope",
        "on_update": "stripe_integration.stripe_integration.subscription_sync.on_subscription_update",
    },
    "Sales Invoice": {
        "validate": "stripe_integration.stripe_integration.company_guard.validate_sales_invoice_company_scope",
    },
})

scheduler_events = {
    "hourly": [
        "stripe_integration.stripe_integration.reconciliation.run_hourly_reconciliation",
    ],
}

fixtures = [
    {
        "dt": "Email Template",
        "filters": [["name", "in", [
            "Stripe CoreOrbit Add Payment Method",
            "Stripe CoreOrbit Subscription Started",
            "Stripe CoreOrbit Subscription Paused",
            "Stripe CoreOrbit Subscription Resumed",
            "Stripe CoreOrbit Subscription Cancelled",
            "Stripe CoreOrbit Payment Request",
            "Stripe COEngine Add Payment Method",
            "Stripe COEngine Subscription Started",
            "Stripe COEngine Subscription Paused",
            "Stripe COEngine Subscription Resumed",
            "Stripe COEngine Subscription Cancelled",
            "Stripe COEngine Payment Request",
            "Stripe Payment Receipt Branded",
        ]]],
    },
    {
        "dt": "Custom Field",
        "filters": [["dt", "in", ["Subscription", "Sales Invoice", "Payment Entry"]]],
    },
    {
        "dt": "Print Format",
        "filters": [["name", "in", ["CoreOrbit Beautiful Invoice", "COEngine Beautiful Invoice"]]],
    },
]
