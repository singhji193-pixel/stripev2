import frappe
import stripe

from frappe.utils import flt, get_url, now, fmt_money
from frappe.utils.password import check_password

from stripe_integration.stripe_integration.utils import (
    get_api_key,
    get_company_abbr_from_company,
)
from stripe_integration.stripe_integration.refunds import apply_refund_to_erp


STRIPE_ACTION_ROLES = ("System Manager", "Accounts Manager", "Accounts User")


def _require_doc_permission(doctype: str, name: str, ptype: str = "write"):
    if not frappe.has_permission(doctype, ptype=ptype, doc=name):
        frappe.throw("Not permitted", frappe.PermissionError)


def _require_stripe_action_guard(gate_password: str = None):
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not any(role in user_roles for role in STRIPE_ACTION_ROLES):
        frappe.throw("Not permitted", frappe.PermissionError)

    # Optional password gate from Stripe Settings. If no password is configured, only role+doc perms apply.
    # Use __Auth existence check to avoid noisy "Password not found..." server messages.
    has_gate = bool(
        frappe.db.get_value(
            "__Auth",
            {
                "doctype": "Stripe Settings",
                "name": "Stripe Settings",
                "fieldname": "stripe_action_password",
            },
            "name",
        )
    )

    if not has_gate:
        return

    if not gate_password:
        frappe.throw("Approval password is required", frappe.PermissionError)

    try:
        check_password("Stripe Settings", "Stripe Settings", gate_password, "stripe_action_password")
    except frappe.AuthenticationError:
        frappe.throw("Invalid approval password", frappe.PermissionError)


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


def _resolve_sender(company_abbr: str) -> str:
    if (company_abbr or "").upper() == "COSL":
        return "CoreOrbit <next@coengine.ai>"
    return "COEngine <erp@coengine.ai>"


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
    _brand_name_from_company_abbr(company_abbr)

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

    # Hard-force attachment PDF to exact print format (avoid renderer fallback)
    abbr = (company_abbr or "").upper()
    pf = "CoreOrbit Beautiful Invoice" if abbr == "COSL" else "COEngine Beautiful Invoice"
    pdf = frappe.attach_print("Sales Invoice", invoice_name, file_name=f"{invoice_name}.pdf", print_format=pf)
    attachments = [pdf] if pdf else None

    template_name = "Stripe CoreOrbit Payment Request" if (company_abbr or "").upper() == "COSL" else "Stripe COEngine Payment Request"
    et = frappe.get_doc("Email Template", template_name)
    rendered_subject = frappe.render_template(et.subject or "Invoice {{ invoice_name }} Payment Link", args)
    rendered_message = frappe.render_template(et.response or "", args)

    frappe.sendmail(
        recipients=[to_email],
        subject=rendered_subject,
        message=rendered_message,
        sender=_resolve_sender(company_abbr),
        now=True,
        delayed=False,
        with_container=False,
        add_unsubscribe_link=0,
        reference_doctype="Sales Invoice",
        reference_name=invoice_name,
        attachments=attachments,
    )


def _build_refund_pdf_attachment(invoice_name: str, company_abbr: str):
    abbr = (company_abbr or "").upper()
    preferred = "CoreOrbit Refund Confirmation" if abbr == "COSL" else "COEngine Refund Confirmation"
    fallback = "CoreOrbit Beautiful Invoice" if abbr == "COSL" else "COEngine Beautiful Invoice"

    pf = preferred if frappe.db.exists("Print Format", preferred) else fallback
    try:
        return frappe.attach_print("Sales Invoice", invoice_name, file_name=f"{invoice_name}-refund.pdf", print_format=pf)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Refund PDF attachment generation failed")
        return None


def _send_refund_email(invoice_name: str, company_abbr: str, refund_payload: dict):
    to_email = _get_recipient_email(frappe.get_doc("Sales Invoice", invoice_name))
    if not to_email:
        return {"sent": False, "reason": "recipient_email_missing"}

    template_name = "Stripe CoreOrbit Refund Processed" if (company_abbr or "").upper() == "COSL" else "Stripe COEngine Refund Processed"
    if not frappe.db.exists("Email Template", template_name):
        return {"sent": False, "reason": "template_missing", "template": template_name}

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    args = {
        "invoice_name": invoice_name,
        "customer_name": inv.get("customer_name") or inv.get("customer") or "Customer",
        "refund_amount": float(refund_payload.get("amount") or 0),
        "currency": (refund_payload.get("currency") or inv.get("currency") or "CAD").upper(),
        "refund_id": refund_payload.get("refund_id") or "",
        "refund_status": refund_payload.get("refund_status") or "",
        "payment_intent": refund_payload.get("payment_intent") or "",
    }

    et = frappe.get_doc("Email Template", template_name)
    subject = frappe.render_template(et.subject or "Refund processed", args)
    message = frappe.render_template(et.response or "", args)
    refund_pdf = _build_refund_pdf_attachment(invoice_name, company_abbr)

    frappe.sendmail(
        recipients=[to_email],
        subject=subject,
        message=message,
        sender=_resolve_sender(company_abbr),
        now=True,
        delayed=False,
        with_container=False,
        add_unsubscribe_link=0,
        reference_doctype="Sales Invoice",
        reference_name=invoice_name,
        attachments=[refund_pdf] if refund_pdf else None,
    )
    return {"sent": True, "template": template_name, "to": to_email, "has_attachment": bool(refund_pdf)}


@frappe.whitelist()
def void_payment_link_stripe(invoice_name: str, gate_password: str = None):
    """Void an active Stripe checkout link for a submitted Sales Invoice.

    - Checkout Session: expire session
    - Payment Link fallback: deactivate payment link
    """

    doctype = "Sales Invoice"
    _require_doc_permission(doctype, invoice_name, "write")
    _require_stripe_action_guard(gate_password)

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
def refund_payment_stripe(invoice_name: str, gate_password: str = None, reason: str = "requested_by_customer"):
    """Create a full Stripe refund for the invoice's latest Stripe payment and mirror it in ERP.

    Notes:
    - This endpoint currently supports full refunds only (small/safe scope for PR2).
    - ERP linkage is done by cancelling the matching submitted Payment Entry.
    """

    doctype = "Sales Invoice"
    _require_doc_permission(doctype, invoice_name, "write")
    _require_stripe_action_guard(gate_password)

    inv = frappe.get_doc(doctype, invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be Submitted before refunding")

    pi_id = inv.get("stripe_payment_intent_id")
    if not pi_id:
        # Fallback: derive PI from latest submitted Payment Entry linked to this invoice.
        refs = frappe.get_all(
            "Payment Entry Reference",
            filters={
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_name,
                "parenttype": "Payment Entry",
                "docstatus": ["!=", 2],
            },
            fields=["parent", "modified"],
            order_by="modified desc",
            limit_page_length=5,
        )
        for ref in refs:
            pe_ref = frappe.db.get_value("Payment Entry", ref.get("parent"), ["reference_no", "docstatus"], as_dict=True)
            if pe_ref and int(pe_ref.get("docstatus") or 0) == 1 and pe_ref.get("reference_no"):
                pi_id = pe_ref.get("reference_no")
                break

    if not pi_id:
        frappe.throw("No Stripe payment intent found for this invoice")

    company = inv.get("company")
    company_abbr = get_company_abbr_from_company(company)
    if not company_abbr:
        frappe.throw("Could not determine company abbr from invoice")

    stripe.api_key = get_api_key(company_abbr)

    payment = stripe.PaymentIntent.retrieve(pi_id)
    amount_received = float((payment.get("amount_received") or 0) / 100.0)
    currency = (payment.get("currency") or inv.get("currency") or "CAD").upper()

    if amount_received <= 0:
        frappe.throw("Stripe payment has no received amount to refund")

    refund = stripe.Refund.create(
        payment_intent=pi_id,
        reason=reason or "requested_by_customer",
        metadata={
            "doctype": "Sales Invoice",
            "docname": invoice_name,
            "company": company,
            "company_abbr": company_abbr,
            "site": frappe.local.site,
            "source": "erp_manual_refund",
        },
    )

    result_payload = {
        "ok": True,
        "invoice": invoice_name,
        "payment_intent": pi_id,
        "refund_id": refund.get("id"),
        "refund_status": refund.get("status"),
        "amount": float((refund.get("amount") or 0) / 100.0),
        "currency": (refund.get("currency") or currency).upper(),
    }

    linked = apply_refund_to_erp(
        stripe_payment_intent_id=pi_id,
        stripe_refund_id=result_payload["refund_id"],
        refund_amount=result_payload["amount"],
        currency=result_payload["currency"],
        source="api.refund_payment_stripe",
    )
    result_payload["erp_linkage"] = linked

    try:
        result_payload["email"] = _send_refund_email(invoice_name, company_abbr, result_payload)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe refund email failed")
        result_payload["email"] = {"sent": False, "reason": "send_failed"}

    return result_payload


@frappe.whitelist()
def request_payment_stripe(invoice_name: str, gate_password: str = None):
    """Default: Stripe Checkout Session (hosted). Fallback: Stripe Payment Link.

    Uses custom fields on Sales Invoice (Address & Contact -> Payment Configuration).
    """

    doctype = "Sales Invoice"
    _require_doc_permission(doctype, invoice_name, "write")
    _require_stripe_action_guard(gate_password)

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
