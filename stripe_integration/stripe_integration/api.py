import frappe
import stripe

from frappe.utils import flt, get_url, now, fmt_money

from stripe_integration.stripe_integration.utils import (
    get_api_key,
    get_company_abbr_from_company,
)


def _require_doc_permission(doctype: str, name: str, ptype: str = "write"):
    if not frappe.has_permission(doctype, ptype=ptype, doc=name):
        frappe.throw("Not permitted", frappe.PermissionError)


def _get_recipient_email(doc):
    for fn in ["contact_email", "customer_email", "email_id"]:
        if getattr(doc, fn, None):
            return getattr(doc, fn)

    customer = getattr(doc, "customer", None) or doc.get("customer")
    if customer:
        email = frappe.db.get_value("Customer", customer, "email_id")
        if email:
            return email
    return None


def _safe_set_value(doctype, name, fieldname, value):
    meta = frappe.get_meta(doctype)
    if meta.get_field(fieldname):
        frappe.db.set_value(doctype, name, fieldname, value, update_modified=False)
        return True
    return False


def _safe_append_history(inv, line: str):
    # Append to Custom Field payment_links_history if present.
    if not frappe.get_meta(inv.doctype).get_field("payment_links_history"):
        return

    existing = inv.get("payment_links_history") or ""
    if existing:
        existing = existing.rstrip() + "\n"
    new_val = existing + line
    frappe.db.set_value(inv.doctype, inv.name, "payment_links_history", new_val, update_modified=False)


def _compute_request_amount_and_kind(inv):
    """Return (amount, kind) where kind in {full, deposit, remainder}.

    Uses custom fields on Sales Invoice:
    - payment_split_type (Full Payment|Split Payment)
    - initial_payment_percentage (Percent)
    - custom_stripe_payment_processed (Check)

    Semantics:
    - Full Payment: request full current outstanding
    - Split Payment:
      - if custom_stripe_payment_processed=0: request deposit based on grand_total * initial_payment_percentage
      - else: request remaining current outstanding
    """

    outstanding = flt(inv.get("outstanding_amount") or 0)
    if outstanding <= 0:
        frappe.throw("Nothing to collect: invoice has no outstanding amount")

    split_type = (inv.get("payment_split_type") or "Full Payment").strip()
    processed = int(inv.get("custom_stripe_payment_processed") or 0) == 1

    if split_type == "Split Payment":
        if processed:
            return outstanding, "remainder"

        pct = flt(inv.get("initial_payment_percentage") or 0)
        if pct <= 0 or pct > 100:
            frappe.throw("Initial Payment % must be between 0 and 100 for Split Payment")

        base_total = flt(inv.get("grand_total") or 0)
        if base_total <= 0:
            frappe.throw("Invoice grand total is invalid")

        deposit = flt(base_total) * pct / 100.0
        deposit = min(deposit, outstanding)
        deposit = flt(deposit)
        if deposit <= 0:
            frappe.throw("Computed deposit amount is 0")

        return deposit, "deposit"

    return outstanding, "full"


def _create_payment_link(amount: float, currency_lc: str, invoice_name: str, company_abbr: str, metadata: dict):
    """Fallback path if Checkout Session create/rendering is problematic."""

    base_domain = "https://coengine.ai" if company_abbr == "COE" else "https://join.coreorbit.io"
    success_url = f"{base_domain}/payment-success?invoice={invoice_name}"

    price = stripe.Price.create(
        currency=currency_lc,
        unit_amount=int(round(amount * 100)),
        product_data={"name": f"Invoice {invoice_name}"},
    )

    link = stripe.PaymentLink.create(
        line_items=[{"price": price.id, "quantity": 1}],
        after_completion={"type": "redirect", "redirect": {"url": success_url}},
        payment_intent_data={"metadata": metadata},
        metadata=metadata,
    )

    return link


def _brand_name_from_company_abbr(company_abbr: str) -> str:
    mapping = {
        "COE": "COEngine",
        "COSL": "CoreOrbit",
    }
    brand = mapping.get((company_abbr or "").strip().upper())
    if not brand:
        frappe.throw(f"No email brand mapping configured for company abbr: {company_abbr}")
    return brand


def _send_payment_email(
    to_email: str,
    invoice_name: str,
    amount: float,
    currency: str,
    checkout_url: str,
    request_kind: str,
    mode_used: str,
    company_abbr: str,
):
    brand_name = _brand_name_from_company_abbr(company_abbr)

    # Build dynamic args used by template/plain fallback
    payment_type = "partial" if request_kind in ("deposit", "remainder") else "full"
    is_initial_payment = 1 if request_kind == "deposit" else 0

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    grand_total = float(inv.get("grand_total") or 0)
    configured_initial_pct = float(inv.get("initial_payment_percentage") or 0)

    if request_kind == "deposit":
        payment_percentage = configured_initial_pct if 0 < configured_initial_pct <= 100 else (
            round((float(amount) / grand_total) * 100, 2) if grand_total > 0 else 100
        )
    elif request_kind == "remainder":
        payment_percentage = round((float(amount) / grand_total) * 100, 2) if grand_total > 0 else 100
    else:
        payment_percentage = 100
    customer_name = inv.get("customer_name") or inv.get("customer") or "Customer"

    args = {
        "invoice_name": invoice_name,
        "customer_name": customer_name,
        "payment_type": payment_type,
        "is_initial_payment": is_initial_payment,
        "payment_percentage": payment_percentage,
        "grand_total": float(inv.get("grand_total") or 0),
        "payment_amount": f"{float(amount):.2f}",
        "currency": currency,
        "stripe_url": checkout_url,
    }

    # Attach invoice PDF using site's default print format for Sales Invoice
    attachment = {
        "print_format_attachment": 1,
        "doctype": "Sales Invoice",
        "name": invoice_name,
        "print_letterhead": 1,
        "lang": "en",
    }
    if (company_abbr or "").upper() == "COSL":
        attachment["print_format"] = "CoreOrbit Beautiful Invoice"

    attachments = [attachment]

    template_name = "Stripe CoreOrbit Payment Request" if (company_abbr or "").upper() == "COSL" else "Stripe COEngine Payment Request"
    et = frappe.get_doc("Email Template", template_name)
    rendered_subject = frappe.render_template(et.subject or "Invoice {{ invoice_name }} Payment Link", args)
    rendered_message = frappe.render_template(et.response or "", args)

    frappe.sendmail(
        recipients=[to_email],
        subject=rendered_subject,
        message=rendered_message,
        sender=f"{brand_name} <erp@coengine.ai>",
        now=True,
        delayed=False,
        with_container=False,
        add_unsubscribe_link=0,
        reference_doctype="Sales Invoice",
        reference_name=invoice_name,
        attachments=attachments,
    )


@frappe.whitelist()
def void_payment_link_stripe(invoice_name: str):
    """Void an active Stripe checkout link for a submitted Sales Invoice.

    - Checkout Session: expire session
    - Payment Link fallback: deactivate payment link
    """

    doctype = "Sales Invoice"
    _require_doc_permission(doctype, invoice_name, "write")

    inv = frappe.get_doc(doctype, invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be Submitted before voiding payment link")

    session_or_link_id = inv.get("stripe_checkout_session_id")
    checkout_url = inv.get("stripe_checkout_url")

    if not session_or_link_id and not checkout_url:
        frappe.throw("No Stripe payment link/session found on this invoice")

    company = inv.get("company")
    company_abbr = get_company_abbr_from_company(company)
    if not company_abbr:
        frappe.throw("Could not determine company abbr from invoice")

    stripe.api_key = get_api_key(company_abbr)

    result = {"ok": True, "invoice": invoice_name, "id": session_or_link_id}

    try:
        if session_or_link_id and str(session_or_link_id).startswith("cs_"):
            stripe.checkout.Session.expire(session_or_link_id)
            result["voided_type"] = "checkout_session"
        elif session_or_link_id and str(session_or_link_id).startswith("plink_"):
            stripe.PaymentLink.modify(session_or_link_id, active=False)
            result["voided_type"] = "payment_link"
        else:
            # Unknown id type: best effort by URL parse for payment links
            if checkout_url and "/pay/" in checkout_url:
                # If we only have URL and unknown id, keep clear error to avoid false success
                frappe.throw("Stored Stripe id is not voidable automatically; please verify session/link id")
            frappe.throw("Unsupported Stripe session/link id format")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe void payment link/session failed")
        raise

    _safe_set_value(doctype, invoice_name, "stripe_checkout_url", None)
    _safe_set_value(doctype, invoice_name, "stripe_checkout_session_id", None)
    _safe_set_value(doctype, invoice_name, "stripe_last_payment_link_sent", None)
    _safe_append_history(inv, f"{now()} | voided | {result.get('voided_type')} | {session_or_link_id}")
    frappe.db.commit()

    return result


@frappe.whitelist()
def request_payment_stripe(invoice_name: str):
    """Default: Stripe Checkout Session (hosted). Fallback: Stripe Payment Link.

    Uses custom fields on Sales Invoice (Address & Contact -> Payment Configuration).
    """

    doctype = "Sales Invoice"
    _require_doc_permission(doctype, invoice_name, "write")

    inv = frappe.get_doc(doctype, invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be Submitted before requesting payment")

    amount, request_kind = _compute_request_amount_and_kind(inv)

    company = inv.get("company")
    company_abbr = get_company_abbr_from_company(company)
    if not company_abbr:
        frappe.throw("Could not determine company abbr from invoice")

    stripe.api_key = get_api_key(company_abbr)

    currency = inv.get("currency") or frappe.get_cached_value("Company", company, "default_currency")
    currency_lc = (currency or "CAD").lower()

    customer_email = _get_recipient_email(inv)
    if not customer_email:
        frappe.throw("No customer email found on invoice/customer")

    success_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_success?invoice=" + invoice_name
    cancel_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_cancelled?invoice=" + invoice_name

    metadata = {
        "doctype": doctype,
        "docname": invoice_name,
        "company": company,
        "company_abbr": company_abbr,
        "site": frappe.local.site,
        "source": "checkout_session",
        "request_kind": request_kind,
        "payment_split_type": inv.get("payment_split_type"),
        "initial_payment_percentage": inv.get("initial_payment_percentage"),
        "requested_amount": str(amount),
    }

    checkout_url = None
    session_id = None
    mode_used = "checkout_session"

    try:
        session_params = {
            "mode": "payment",
            "payment_method_types": ["card"],
            "line_items": [
                {
                    "price_data": {
                        "currency": currency_lc,
                        "product_data": {"name": f"Invoice {invoice_name}"},
                        "unit_amount": int(round(amount * 100)),
                    },
                    "quantity": 1,
                }
            ],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "customer_email": customer_email,
            "metadata": metadata,
        }

        session = stripe.checkout.Session.create(**session_params)
        checkout_url = session.get("url")
        session_id = session.get("id")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe Checkout Session create failed; using Payment Link fallback")

    if not checkout_url:
        mode_used = "payment_link"
        metadata["source"] = "payment_link_fallback"
        link = _create_payment_link(amount, currency_lc, invoice_name, company_abbr, metadata)
        checkout_url = link.url
        session_id = link.id

    _safe_set_value(doctype, invoice_name, "stripe_checkout_url", checkout_url)
    _safe_set_value(doctype, invoice_name, "stripe_checkout_session_id", session_id)
    _safe_set_value(doctype, invoice_name, "stripe_payment_link_amount", amount)
    _safe_set_value(doctype, invoice_name, "stripe_payment_link_currency", currency)
    _safe_set_value(doctype, invoice_name, "stripe_last_payment_link_sent", now())

    _safe_append_history(inv, f"{now()} | {request_kind} | {currency} {amount:.2f} | {mode_used} | {session_id}")

    frappe.db.commit()

    _send_payment_email(customer_email, invoice_name, amount, currency, checkout_url, request_kind, mode_used, company_abbr)

    return {
        "ok": True,
        "mode": mode_used,
        "request_kind": request_kind,
        "checkout_url": checkout_url,
        "session_id": session_id,
        "email": customer_email,
        "amount": amount,
        "currency": currency,
    }


@frappe.whitelist(allow_guest=True)
def payment_success(invoice=None, **kwargs):
    frappe.respond_as_web_page(
        "Payment Successful",
        "<p>Payment successful. You may close this tab.</p>",
    )


@frappe.whitelist(allow_guest=True)
def payment_cancelled(invoice=None, **kwargs):
    frappe.respond_as_web_page(
        "Payment Cancelled",
        "<p>Payment cancelled. You may close this tab.</p>",
    )
