import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from datetime import time as dtime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import frappe
import stripe
from frappe.utils import get_system_timezone, get_url, getdate, nowdate

from stripe_integration.stripe_integration.accounting import MariaDBNamedLock
from stripe_integration.stripe_integration.event_log import mark_event_status, upsert_event
from stripe_integration.stripe_integration.utils import get_api_key, get_company_abbr_from_company

LIFECYCLE_TEMPLATE_MAP = {
    "COE": {
        "add_payment_method": "Stripe COEngine Add Payment Method",
        "started": "Stripe COEngine Subscription Started",
        "resumed": "Stripe COEngine Subscription Resumed",
        "paused": "Stripe COEngine Subscription Paused",
        "cancelled": "Stripe COEngine Subscription Cancelled",
    },
    "COSL": {
        "add_payment_method": "Stripe CoreOrbit Add Payment Method",
        "started": "Stripe CoreOrbit Subscription Started",
        "resumed": "Stripe CoreOrbit Subscription Started",
        "paused": "Stripe CoreOrbit Subscription Paused",
        "cancelled": "Stripe CoreOrbit Subscription Cancelled",
    },
}

ALLOWED_COMPANY_ABBR = {"COE", "COSL"}
VALID_ACTIONS = {"pause", "resume", "cancel", "plan_change"}

SETUP_URL_FIELD = "stripe_setup_checkout_url"
SETUP_SESSION_FIELD = "stripe_setup_session_id"
SETUP_CREATED_AT_FIELD = "stripe_setup_link_created_at"
SETUP_EXPIRES_AT_FIELD = "stripe_setup_link_expires_at"
SETUP_STATUS_FIELD = "stripe_setup_link_status"
SETUP_PM_FIELD = "stripe_default_payment_method_id"
SETUP_INTENT_FIELD = "stripe_last_setup_intent_id"
SETUP_TOKEN_NONCE_FIELD = "stripe_setup_token_nonce"

STABLE_SETUP_ROUTE = "/api/method/stripe_integration.stripe_integration.subscription_sync.open_subscription_setup_link"

def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        value = obj[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        if hasattr(obj, key):
            value = getattr(obj, key)
            if not callable(value):
                return default if value is None else value
    except Exception:
        pass
    try:
        as_dict = obj.to_dict_recursive() if hasattr(obj, "to_dict_recursive") else obj.to_dict()
        if isinstance(as_dict, dict):
            value = as_dict.get(key, default)
            return default if value is None else value
    except Exception:
        pass
    return default


def _stable_setup_secret():
    return (getattr(frappe.local.conf, "encryption_key", None) or frappe.local.site or "stripe-subscription-setup").encode()


def _make_subscription_setup_token(subscription_name: str, nonce: str | None = None) -> str:
    nonce = nonce or frappe.db.get_value("Subscription", subscription_name, SETUP_TOKEN_NONCE_FIELD)
    if not nonce:
        return ""
    payload = f"{subscription_name}:{nonce}".encode()
    sig = hmac.new(_stable_setup_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _subscription_setup_token_valid(subscription_name: str, token: str | None) -> bool:
    if not subscription_name or not token:
        return False
    setup = frappe.db.get_value(
        "Subscription",
        subscription_name,
        [SETUP_TOKEN_NONCE_FIELD, SETUP_STATUS_FIELD, "status"],
        as_dict=True,
    ) or {}
    if setup.get(SETUP_STATUS_FIELD) != "pending":
        return False
    if (setup.get("status") or "").strip().lower() in {"cancelled", "canceled"}:
        return False
    expected = _make_subscription_setup_token(
        subscription_name,
        setup.get(SETUP_TOKEN_NONCE_FIELD),
    )
    if not expected:
        return False
    return hmac.compare_digest(expected, str(token).strip())


def _build_stable_subscription_setup_url(sub_doc) -> str:
    token = _make_subscription_setup_token(
        sub_doc.name,
        sub_doc.get(SETUP_TOKEN_NONCE_FIELD),
    )
    query = urlencode({"subscription_name": sub_doc.name, "token": token})
    return f"{get_url()}{STABLE_SETUP_ROUTE}?{query}"


def _rotate_subscription_setup_token(subscription_name: str):
    nonce = secrets.token_urlsafe(24)
    _set_subscription_fields(subscription_name, {SETUP_TOKEN_NONCE_FIELD: nonce})
    return nonce


def _get_company_letterhead(company: str | None) -> str | None:
    if not company:
        return None
    return frappe.db.get_value("Company", company, "default_letter_head")


def _is_enabled() -> bool:
    try:
        return int(frappe.db.get_single_value("Stripe Settings", "enable_subscription_state_sync") or 0) == 1
    except Exception:
        return False


def _normalize_action(action: str | None) -> str | None:
    if not action:
        return None
    action = str(action).strip().lower()
    return action if action in VALID_ACTIONS else None


def _validate_company_for_stripe(company: str):
    company_abbr = get_company_abbr_from_company(company)
    if company_abbr not in ALLOWED_COMPANY_ABBR:
        frappe.throw(f"Company {company_abbr} not allowed for Stripe sync")
    get_api_key(company_abbr)
    return company_abbr


def _require_subscription_permission(subscription_name: str, permission_type: str = "read"):
    subscription = frappe.get_doc("Subscription", subscription_name)
    subscription.check_permission(permission_type)
    return subscription


def _require_subscription_action_role():
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection({"System Manager", "Accounts Manager"}):
        frappe.throw("Not permitted", frappe.PermissionError)


def _event_stub(subscription_doc, action: str):
    return {
        "id": f"local_outbound_{subscription_doc.name}_{action}",
        "type": f"subscription.{action}",
        "data": {"object": {"id": getattr(subscription_doc, "stripe_subscription_id", None)}},
    }


def _validate_transition(stripe_sub_id: str, action: str, company_abbr: str):
    if action not in {"pause", "resume"}:
        return True, None

    remote = stripe.Subscription.retrieve(
        stripe_sub_id,
        api_key=get_api_key(company_abbr),
    )
    paused = bool(getattr(remote, "pause_collection", None))

    if action == "pause" and paused:
        return False, "already_paused"
    if action == "resume" and not paused:
        return False, "not_paused"
    return True, None


def _sync_subscription_plan(subscription_doc, stripe_sub_id: str, company_abbr: str):
    remote = stripe.Subscription.retrieve(
        stripe_sub_id,
        api_key=get_api_key(company_abbr),
    )
    existing_items = _stripe_get(_stripe_get(remote, "items") or {}, "data") or []
    desired_items = _build_stripe_subscription_items(subscription_doc)
    updates = []

    for index, desired in enumerate(desired_items):
        update = dict(desired)
        if index < len(existing_items):
            update["id"] = _stripe_get(existing_items[index], "id")
        updates.append(update)

    for extra in existing_items[len(desired_items):]:
        updates.append({"id": _stripe_get(extra, "id"), "deleted": True})

    pricing = _build_subscription_pricing_params(subscription_doc, company_abbr)
    params = {
        "items": updates,
        "discounts": pricing.get("discounts", []),
        "default_tax_rates": pricing.get("default_tax_rates", []),
        "proration_behavior": "none",
        "payment_behavior": "error_if_incomplete",
    }
    signature = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:20]
    params["idempotency_key"] = f"erpnext-plan-change-{subscription_doc.name}-{signature}"
    updated = stripe.Subscription.modify(
        stripe_sub_id,
        api_key=get_api_key(company_abbr),
        **params,
    )

    updated_items = _stripe_get(_stripe_get(updated, "items") or {}, "data") or []
    _set_subscription_fields(
        subscription_doc.name,
        {
            "stripe_subscription_item_id": (
                _stripe_get(updated_items[0], "id") if updated_items else ""
            ),
            "stripe_status": _stripe_get(updated, "status") or "",
        },
    )
    return updated


def _sync_subscription(subscription_doc, action: str):
    action = _normalize_action(action)
    if not action:
        return {"handled": False, "reason": "unsupported_action", "action": action}

    stripe_sub_id = getattr(subscription_doc, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_doc.name}

    company_abbr = _validate_company_for_stripe(subscription_doc.company)
    api_key = get_api_key(company_abbr)
    ev = _event_stub(subscription_doc, action)
    upsert_event(ev, payload=json.dumps(ev).encode(), company_abbr=company_abbr, status="Processing")

    try:
        ok, reason = _validate_transition(stripe_sub_id, action, company_abbr)
        if not ok:
            mark_event_status(ev["id"], "Ignored", reason)
            return {
                "handled": False,
                "reason": reason,
                "subscription": subscription_doc.name,
                "stripe_subscription_id": stripe_sub_id,
                "action": action,
            }

        if action == "cancel":
            stripe.Subscription.delete(stripe_sub_id, api_key=api_key)
        elif action == "resume":
            stripe.Subscription.modify(stripe_sub_id, pause_collection="", api_key=api_key)
        elif action == "pause":
            stripe.Subscription.modify(
                stripe_sub_id,
                pause_collection={"behavior": "void"},
                api_key=api_key,
            )
        elif action == "plan_change":
            _sync_subscription_plan(subscription_doc, stripe_sub_id, company_abbr)

        mark_event_status(ev["id"], "Completed")
        return {
            "handled": True,
            "subscription": subscription_doc.name,
            "stripe_subscription_id": stripe_sub_id,
            "action": action,
            "company_abbr": company_abbr,
        }
    except Exception as e:
        mark_event_status(ev["id"], "Failed", str(e))
        raise


def _erp_status_options():
    try:
        opts = frappe.db.get_value("DocField", {"parent": "Subscription", "fieldname": "status"}, "options") or ""
        return {x.strip() for x in opts.split("\n") if x.strip()}
    except Exception:
        return set()


def _map_stripe_to_erp_status(stripe_status: str | None, paused: bool = False):
    s = (stripe_status or "").strip().lower()
    if s in {"canceled", "incomplete_expired"}:
        return "Cancelled"
    if s in {"past_due"}:
        return "Past Due Date"
    if s in {"unpaid", "incomplete"}:
        return "Unpaid"
    if s in {"active", "trialing"}:
        return "Active"
    return None


def _resolve_subscription_email(sub_doc):
    # 1. Direct fields on the Subscription doc.
    for fn in ("contact_email", "email", "subscriber_email", "customer_email"):
        v = sub_doc.get(fn)
        if v:
            return v

    # 2-4. Walk the linked Customer via the shared resolver (Customer.email_id,
    # customer_primary_contact, then linked Contact via Dynamic Link).
    party_type = (sub_doc.get("party_type") or "").strip()
    party = sub_doc.get("party")
    if party_type != "Customer" or not party:
        return None

    from stripe_integration.stripe_integration.utils import resolve_customer_email
    return resolve_customer_email(party)


def _pick_lifecycle_kind(prev_status: str | None, prev_paused: bool, new_status: str | None, new_paused: bool):
    ps = (prev_status or "").strip().lower()
    ns = (new_status or "").strip().lower()

    if ns in {"canceled", "incomplete_expired"} and ns != ps:
        return "cancelled"
    if new_paused and not prev_paused:
        return "paused"
    if ns in {"unpaid", "incomplete", "past_due"} and ns != ps:
        return "add_payment_method"
    if ns in {"active", "trialing"} and not new_paused and prev_paused:
        return "resumed"
    if ns in {"active", "trialing"} and not new_paused and ps not in {"active", "trialing"}:
        return "started"
    return None


def _resolve_sender(company_abbr: str):
    if company_abbr == "COSL":
        return {
            "sender": "CoreOrbit Billing <billing@coreorbit.io>",
            "email_account": "CoreOrbit Billing",
        }
    return {
        "sender": "COEngine <erp@coengine.ai>",
        "email_account": "COEngine",
    }


def _set_subscription_fields(sub_name: str, values: dict):
    meta = frappe.get_meta("Subscription")
    update = {k: v for k, v in (values or {}).items() if meta.get_field(k)}
    if update:
        frappe.db.set_value("Subscription", sub_name, update, update_modified=False)
        frappe.db.commit()


def _build_stripe_subscription_items(sub_doc) -> list[dict]:
    items = []
    for row in sub_doc.get("plans") or []:
        plan = row.get("plan") if hasattr(row, "get") else getattr(row, "plan", None)
        qty = row.get("qty") if hasattr(row, "get") else getattr(row, "qty", None)
        if not plan:
            continue
        price_id = frappe.db.get_value("Subscription Plan", plan, "product_price_id")
        if not price_id:
            frappe.throw(f"Subscription Plan {plan} is missing Stripe product_price_id")
        item = {"price": price_id}
        if qty:
            item["quantity"] = int(qty)
        items.append(item)

    if not items:
        frappe.throw(f"Subscription {sub_doc.name} has no billable plans configured")
    return items


def _subscription_currency(sub_doc) -> str:
    return (
        frappe.get_cached_value("Company", sub_doc.get("company"), "default_currency")
        or "CAD"
    ).lower()


def _stripe_list_all(resource, api_key: str, **params):
    params = {**params, "limit": 100}
    rows = []
    while True:
        page = resource.list(api_key=api_key, **params)
        data = _stripe_get(page, "data") or []
        rows.extend(data)
        if not _stripe_get(page, "has_more"):
            return rows
        params["starting_after"] = _stripe_get(data[-1], "id")


def _ensure_subscription_discount(sub_doc, company_abbr: str, currency: str) -> str | None:
    percentage = float(sub_doc.get("additional_discount_percentage") or 0)
    amount = float(sub_doc.get("additional_discount_amount") or 0)
    if percentage <= 0 and amount <= 0:
        return None

    if percentage > 0:
        kind = "percent"
        value = round(percentage, 6)
        create_args = {"percent_off": value}
    else:
        kind = "amount"
        value = round(amount * 100)
        if value <= 0:
            return None
        create_args = {"amount_off": value, "currency": currency}

    signature = hashlib.sha256(
        f"{company_abbr}:{currency}:{kind}:{value}".encode()
    ).hexdigest()[:24]
    api_key = get_api_key(company_abbr)
    for coupon in _stripe_list_all(stripe.Coupon, api_key):
        metadata = _stripe_get(coupon, "metadata") or {}
        if metadata.get("erpnext_signature") == signature:
            return _stripe_get(coupon, "id")

    coupon = stripe.Coupon.create(
        duration="forever",
        name=f"ERPNext {kind} discount {value}",
        metadata={
            "erpnext_signature": signature,
            "company_abbr": company_abbr,
            "source": "erpnext_subscription",
        },
        idempotency_key=f"erpnext-subscription-coupon-{signature}",
        api_key=api_key,
        **create_args,
    )
    return _stripe_get(coupon, "id")


def _ensure_subscription_tax_rates(sub_doc, company_abbr: str) -> list[str]:
    template_name = sub_doc.get("sales_tax_template")
    if not template_name:
        return []

    template = frappe.get_doc("Sales Taxes and Charges Template", template_name)
    api_key = get_api_key(company_abbr)
    existing_rates = _stripe_list_all(stripe.TaxRate, api_key, active=True)
    tax_rate_ids = []
    company_country = frappe.get_cached_value("Company", sub_doc.get("company"), "country")
    country_code = (
        frappe.db.get_value("Country", company_country, "code")
        if company_country
        else None
    )
    country_code = (country_code or "").strip().upper()

    for row in template.get("taxes") or []:
        rate = float(row.get("rate") or 0)
        if rate == 0:
            continue
        if (row.get("add_deduct_tax") or "Add") != "Add" or row.get("charge_type") != "On Net Total":
            frappe.throw(
                f"Stripe subscriptions only support additive 'On Net Total' taxes; "
                f"{template_name} row {row.get('idx')} is unsupported"
            )

        inclusive = bool(row.get("included_in_print_rate"))
        label = (row.get("description") or row.get("account_head") or template_name)[:50]
        signature = hashlib.sha256(
            f"{company_abbr}:{label}:{rate:.6f}:{int(inclusive)}".encode()
        ).hexdigest()[:24]

        matched = None
        for tax_rate in existing_rates:
            metadata = _stripe_get(tax_rate, "metadata") or {}
            if metadata.get("erpnext_signature") == signature:
                matched = _stripe_get(tax_rate, "id")
                break

        if not matched:
            create_args = {
                "display_name": label,
                "description": f"ERPNext {template_name}",
                "percentage": rate,
                "inclusive": inclusive,
                "metadata": {
                    "erpnext_signature": signature,
                    "company_abbr": company_abbr,
                    "source": "erpnext_subscription",
                },
                "idempotency_key": f"erpnext-subscription-tax-{signature}",
                "api_key": api_key,
            }
            if len(country_code) == 2:
                create_args["country"] = country_code

            tax_rate = stripe.TaxRate.create(
                **create_args,
            )
            existing_rates.append(tax_rate)
            matched = _stripe_get(tax_rate, "id")

        tax_rate_ids.append(matched)

    return tax_rate_ids


def _build_subscription_pricing_params(sub_doc, company_abbr: str) -> dict:
    currency = _subscription_currency(sub_doc)
    params = {}
    coupon_id = _ensure_subscription_discount(sub_doc, company_abbr, currency)
    if coupon_id:
        params["discounts"] = [{"coupon": coupon_id}]

    tax_rate_ids = _ensure_subscription_tax_rates(sub_doc, company_abbr)
    if tax_rate_ids:
        params["default_tax_rates"] = tax_rate_ids
    return params


def _build_stripe_subscription_create_params(sub_doc, stripe_customer_id: str, payment_method: str, company_abbr: str):
    items = _build_stripe_subscription_items(sub_doc)

    params = {
        "customer": stripe_customer_id,
        "items": items,
        "default_payment_method": payment_method,
        "collection_method": "charge_automatically",
        "metadata": {
            "doctype": "Subscription",
            "docname": sub_doc.name,
            "company": sub_doc.get("company") or "",
            "company_abbr": company_abbr,
            "site": frappe.local.site,
            "source": "subscription_setup_completion",
        },
        "payment_settings": {"save_default_payment_method": "on_subscription"},
    }
    params.update(_build_subscription_pricing_params(sub_doc, company_abbr))

    start_date = getdate(sub_doc.get("start_date")) if sub_doc.get("start_date") else None
    current_period_start = (
        getdate(sub_doc.get("current_invoice_start"))
        if sub_doc.get("current_invoice_start")
        else None
    )
    today = getdate(nowdate())
    first_stripe_billing_date = next(
        (
            candidate
            for candidate in (current_period_start, start_date)
            if candidate and candidate > today
        ),
        None,
    )
    if first_stripe_billing_date:
        local_midnight = datetime.combine(
            first_stripe_billing_date,
            dtime.min,
            tzinfo=ZoneInfo(get_system_timezone()),
        )
        params["trial_end"] = int(local_midnight.astimezone(timezone.utc).timestamp())

    return params


def ensure_stripe_subscription_for_subscription(subscription_name: str, payment_method: str | None = None, stripe_customer_id: str | None = None):
    with MariaDBNamedLock(f"stripe-subscription-create-{subscription_name}", timeout=30):
        sub_doc = frappe.get_doc("Subscription", subscription_name)
        if sub_doc.get("stripe_subscription_id"):
            return {
                "created": False,
                "reason": "already_linked",
                "stripe_subscription_id": sub_doc.get("stripe_subscription_id"),
            }

        company_abbr = _validate_company_for_stripe(sub_doc.company)
        api_key = get_api_key(company_abbr)
        payment_method = payment_method or sub_doc.get(SETUP_PM_FIELD)
        supplied_payment_method = bool(payment_method)
        customer_email = _resolve_subscription_email(sub_doc)
        stripe_customer_id = stripe_customer_id or sub_doc.get("stripe_customer_id") or None
        payment_method_doc = None
        attached_customer = None
        if payment_method:
            payment_method_doc = stripe.PaymentMethod.retrieve(
                payment_method,
                api_key=api_key,
            )
            attached_customer = _stripe_get(payment_method_doc, "customer")
            if stripe_customer_id and attached_customer and attached_customer != stripe_customer_id:
                frappe.throw("Stripe payment method belongs to a different customer")
            if not stripe_customer_id and attached_customer:
                stripe_customer_id = attached_customer

        if not stripe_customer_id and customer_email:
            candidates = _stripe_get(
                stripe.Customer.list(email=customer_email, limit=100, api_key=api_key),
                "data",
            ) or []
            for customer in candidates:
                metadata = _stripe_get(customer, "metadata") or {}
                if (
                    metadata.get("docname") == sub_doc.name
                    or (
                        metadata.get("company_abbr") == company_abbr
                        and metadata.get("erpnext_party_type") == (sub_doc.get("party_type") or "")
                        and metadata.get("erpnext_party") == (sub_doc.get("party") or "")
                    )
                ):
                    stripe_customer_id = _stripe_get(customer, "id")
                    break

        if stripe_customer_id and attached_customer and attached_customer != stripe_customer_id:
            frappe.throw("Stripe payment method belongs to a different customer")

        customer_doc = None
        if stripe_customer_id:
            customer_doc = stripe.Customer.retrieve(stripe_customer_id, api_key=api_key)
            customer_metadata = dict(_stripe_get(customer_doc, "metadata") or {})
            metadata_company = (customer_metadata.get("company_abbr") or "").strip().upper()
            metadata_party = customer_metadata.get("erpnext_party")
            if metadata_company and metadata_company != company_abbr:
                frappe.throw("Stripe customer belongs to a different company")
            if metadata_party and metadata_party != (sub_doc.get("party") or ""):
                frappe.throw("Stripe customer belongs to a different ERPNext party")

            if not payment_method:
                invoice_settings = _stripe_get(customer_doc, "invoice_settings") or {}
                default_payment_method = _stripe_get(
                    invoice_settings,
                    "default_payment_method",
                )
                payment_method = _stripe_get(default_payment_method, "id") or default_payment_method
                if payment_method:
                    payment_method_doc = stripe.PaymentMethod.retrieve(
                        payment_method,
                        api_key=api_key,
                    )
                    attached_customer = _stripe_get(payment_method_doc, "customer")
                    if attached_customer and attached_customer != stripe_customer_id:
                        frappe.throw("Stripe default payment method belongs to a different customer")

        if not payment_method:
            return {
                "created": False,
                "reason": "missing_payment_method",
                "stripe_customer_id": stripe_customer_id,
            }

        if not stripe_customer_id:
            party_signature = hashlib.sha256(
                f"{company_abbr}:{sub_doc.get('party_type') or ''}:{sub_doc.get('party') or sub_doc.name}".encode()
            ).hexdigest()[:24]
            customer_kwargs = {
                "name": sub_doc.get("party") or sub_doc.name,
                "metadata": {
                    "doctype": "Subscription",
                    "docname": sub_doc.name,
                    "company_abbr": company_abbr,
                    "erpnext_party_type": sub_doc.get("party_type") or "",
                    "erpnext_party": sub_doc.get("party") or "",
                },
                "idempotency_key": f"erpnext-customer-{company_abbr}-{party_signature}",
            }
            if customer_email:
                customer_kwargs["email"] = customer_email
            customer_kwargs["api_key"] = api_key
            customer_doc = stripe.Customer.create(**customer_kwargs)
            stripe_customer_id = _stripe_get(customer_doc, "id")

        if not attached_customer:
            stripe.PaymentMethod.attach(
                payment_method,
                customer=stripe_customer_id,
                api_key=api_key,
            )

        if not customer_doc:
            customer_doc = stripe.Customer.retrieve(stripe_customer_id, api_key=api_key)
        customer_metadata = dict(_stripe_get(customer_doc, "metadata") or {})
        metadata_company = (customer_metadata.get("company_abbr") or "").strip().upper()
        metadata_party = customer_metadata.get("erpnext_party")
        if metadata_company and metadata_company != company_abbr:
            frappe.throw("Stripe customer belongs to a different company")
        if metadata_party and metadata_party != (sub_doc.get("party") or ""):
            frappe.throw("Stripe customer belongs to a different ERPNext party")
        customer_metadata.update(
            {
                "company_abbr": company_abbr,
                "erpnext_party_type": sub_doc.get("party_type") or "",
                "erpnext_party": sub_doc.get("party") or "",
            }
        )
        stripe.Customer.modify(
            stripe_customer_id,
            invoice_settings={"default_payment_method": payment_method},
            metadata=customer_metadata,
            api_key=api_key,
        )

        params = _build_stripe_subscription_create_params(
            sub_doc,
            stripe_customer_id,
            payment_method,
            company_abbr,
        )
        params["idempotency_key"] = f"erpnext-subscription-{company_abbr}-{sub_doc.name}"
        params["api_key"] = api_key
        remote_sub = stripe.Subscription.create(**params)

        remote_items = _stripe_get(_stripe_get(remote_sub, "items") or {}, "data") or []
        _set_subscription_fields(
            sub_doc.name,
            {
                "stripe_customer_id": stripe_customer_id,
                "stripe_subscription_id": _stripe_get(remote_sub, "id") or "",
                "stripe_subscription_item_id": (
                    _stripe_get(remote_items[0], "id") if remote_items else ""
                ),
                "stripe_status": _stripe_get(remote_sub, "status") or "",
                "stripe_paused": 1 if bool(_stripe_get(remote_sub, "pause_collection")) else 0,
            },
        )

        return {
            "created": True,
            "subscription": sub_doc.name,
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": _stripe_get(remote_sub, "id"),
            "stripe_status": _stripe_get(remote_sub, "status"),
            "trial_end": params.get("trial_end"),
            "used_saved_payment_method": not supplied_payment_method,
        }


def _generate_subscription_setup_checkout_url(sub_doc, company_abbr: str, to_email: str | None = None):
    # Create a fresh setup-mode Checkout Session so customer adds a payment method
    # without immediate charge. This avoids stale/expired one-time payment links.
    stripe_sub_id = sub_doc.get("stripe_subscription_id")
    _validate_company_for_stripe(sub_doc.get("company"))
    api_key = get_api_key(company_abbr)

    stripe_customer_id = None
    if stripe_sub_id:
        try:
            remote_sub = stripe.Subscription.retrieve(stripe_sub_id, api_key=api_key)
            stripe_customer_id = getattr(remote_sub, "customer", None)
        except Exception:
            stripe_customer_id = None

    success_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_success?subscription=" + sub_doc.name
    cancel_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_cancelled?subscription=" + sub_doc.name

    currency = (frappe.get_cached_value("Company", sub_doc.get("company"), "default_currency") or "CAD").lower()

    params = {
        "mode": "setup",
        "currency": currency,
        "payment_method_types": ["card"],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "doctype": "Subscription",
            "docname": sub_doc.name,
            "company": sub_doc.get("company") or "",
            "company_abbr": company_abbr,
            "source": "subscription_add_payment_method",
            "stripe_subscription_id": stripe_sub_id,
            "site": frappe.local.site,
        },
        "api_key": api_key,
    }

    if stripe_customer_id:
        params["customer"] = stripe_customer_id
    elif to_email:
        params["customer_email"] = to_email

    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as e:
        # Retry without customer_email if Stripe rejects malformed/invalid address.
        if params.get("customer_email"):
            params.pop("customer_email", None)
            session = stripe.checkout.Session.create(**params)
        else:
            raise e

    checkout_url = session.get("url")

    if checkout_url:
        _set_subscription_fields(
            sub_doc.name,
            {
                SETUP_URL_FIELD: checkout_url,
                SETUP_SESSION_FIELD: session.get("id") or "",
                SETUP_CREATED_AT_FIELD: frappe.utils.now_datetime(),
                SETUP_EXPIRES_AT_FIELD: frappe.utils.get_datetime(session.get("expires_at")) if session.get("expires_at") else None,
                SETUP_STATUS_FIELD: "pending",
                # keep legacy field in sync for backward-compatible templates
                "stripe_checkout_url": checkout_url,
            },
        )

    return checkout_url or ""


def _build_subscription_invoice_attachment(sub_doc):
    """Build Sales Invoice PDF attachment ONLY from this subscription's invoices.

    Avoids customer-wide fallback so wrong invoice never gets attached.
    Returns: (pdf_attachment_or_none, invoice_name_or_none)
    """
    si_name = None
    try:
        # Primary: latest submitted invoice tied to this subscription
        # (attach even if paid; user expects invoice PDF on add-payment-method email)
        si_name = frappe.db.get_value(
            "Sales Invoice",
            {"subscription": sub_doc.name, "docstatus": 1},
            "name",
            order_by="posting_date desc, posting_time desc, modified desc",
        )

        # Fallback: latest invoice tied to this subscription (any docstatus)
        if not si_name:
            si_name = frappe.db.get_value(
                "Sales Invoice",
                {"subscription": sub_doc.name},
                "name",
                order_by="posting_date desc, posting_time desc, modified desc",
            )

        if not si_name:
            return None, None

        company_abbr = get_company_abbr_from_company(sub_doc.get("company"))
        pf = "CoreOrbit Beautiful Invoice" if company_abbr == "COSL" else "COEngine Beautiful Invoice"

        letterhead = _get_company_letterhead(sub_doc.get("company"))
        try:
            # Preferred branded format
            return frappe.attach_print(
                "Sales Invoice",
                si_name,
                file_name=f"{si_name}.pdf",
                print_format=pf,
                letterhead=letterhead,
            ), si_name
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Attach print failed with forced format {pf} for {si_name}")
            try:
                # Safe fallback: Standard + company letterhead (avoids no-attachment outcome)
                return frappe.attach_print(
                    "Sales Invoice",
                    si_name,
                    file_name=f"{si_name}.pdf",
                    print_format="Standard",
                    letterhead=letterhead,
                ), si_name
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Attach print failed with Standard fallback for {si_name}")
                return None, si_name
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Build subscription invoice attachment failed for {sub_doc.name}")
        return None, None


def _send_lifecycle_email(subscription_name: str, company_abbr: str, kind: str, stripe_sub_obj: dict):
    template_name = (LIFECYCLE_TEMPLATE_MAP.get(company_abbr or "", {}) or {}).get(kind)
    if not template_name:
        return {"sent": False, "reason": "template_not_mapped"}

    if not frappe.db.exists("Email Template", template_name):
        return {"sent": False, "reason": "template_missing", "template": template_name}

    sub_doc = frappe.get_doc("Subscription", subscription_name)
    to_email = _resolve_subscription_email(sub_doc)
    if not to_email:
        return {"sent": False, "reason": "recipient_email_missing", "template": template_name}

    customer_name = sub_doc.get("party") or ""
    if (sub_doc.get("party_type") or "") == "Customer" and sub_doc.get("party"):
        customer_name = frappe.db.get_value("Customer", sub_doc.get("party"), "customer_name") or customer_name

    plan_name = ""
    try:
        plans = sub_doc.get("plans") or []
        if plans and getattr(plans[0], "plan", None):
            plan_name = plans[0].plan
    except Exception:
        plan_name = ""

    checkout_url = sub_doc.get(SETUP_URL_FIELD) or sub_doc.get("stripe_checkout_url") or ""
    if kind == "add_payment_method":
        if sub_doc.get(SETUP_STATUS_FIELD) != "pending" or not checkout_url:
            _rotate_subscription_setup_token(sub_doc.name)
            checkout_url = _generate_subscription_setup_checkout_url(
                sub_doc,
                company_abbr,
                to_email=to_email,
            )
            sub_doc = frappe.get_doc("Subscription", subscription_name)
        if not checkout_url:
            return {
                "sent": False,
                "reason": "setup_checkout_url_missing",
                "template": template_name,
            }
        checkout_url = _build_stable_subscription_setup_url(sub_doc)

    args = {
        "subscription_name": sub_doc.name,
        "party": sub_doc.get("party") or "",
        "customer_name": customer_name,
        "plan_name": plan_name,
        "company": sub_doc.get("company") or "",
        "stripe_subscription_id": sub_doc.get("stripe_subscription_id") or (stripe_sub_obj or {}).get("id") or "",
        "stripe_status": (stripe_sub_obj or {}).get("status") or "",
        "stripe_checkout_url": checkout_url,
        "paused": 1 if bool((stripe_sub_obj or {}).get("pause_collection")) else 0,
    }

    et = frappe.get_doc("Email Template", template_name)
    subject = frappe.render_template(et.subject or "Subscription Update", args)
    message = frappe.render_template(et.response or "", args)

    sender_cfg = _resolve_sender(company_abbr)
    attachments = []
    attached_invoice = None
    if kind == "add_payment_method":
        inv_pdf, attached_invoice = _build_subscription_invoice_attachment(sub_doc)
        if inv_pdf:
            attachments.append(inv_pdf)

    frappe.sendmail(
        recipients=[to_email],
        subject=subject,
        message=message,
        sender=sender_cfg["sender"],
        attachments=attachments or None,
        now=True,
        delayed=False,
        add_unsubscribe_link=0,
        reference_doctype="Subscription",
        reference_name=sub_doc.name,
    )
    return {
        "sent": True,
        "template": template_name,
        "to": to_email,
        "kind": kind,
        "has_attachment": bool(attachments),
        "attached_invoice": attached_invoice,
    }


def _apply_subscription_state(sub_name: str, stripe_sub_obj: dict):
    stripe_status = (stripe_sub_obj or {}).get("status")
    paused = bool((stripe_sub_obj or {}).get("pause_collection"))
    cancel_at_period_end = int(bool((stripe_sub_obj or {}).get("cancel_at_period_end")))

    prev = frappe.db.get_value("Subscription", sub_name, ["stripe_status", "stripe_paused"], as_dict=True) or {}
    prev_status = prev.get("stripe_status")
    prev_paused = bool(prev.get("stripe_paused"))

    update = {}
    if frappe.get_meta("Subscription").get_field("stripe_status"):
        update["stripe_status"] = stripe_status or ""
    if frappe.get_meta("Subscription").get_field("stripe_paused"):
        update["stripe_paused"] = 1 if paused else 0
    if frappe.get_meta("Subscription").get_field("cancel_at_period_end"):
        update["cancel_at_period_end"] = cancel_at_period_end

    erp_status = _map_stripe_to_erp_status(stripe_status, paused=paused)
    allowed = _erp_status_options()
    if erp_status and (not allowed or erp_status in allowed):
        update["status"] = erp_status

    if update:
        frappe.db.set_value("Subscription", sub_name, update, update_modified=False)
        frappe.db.commit()

    return {
        "subscription": sub_name,
        "stripe_status": stripe_status,
        "paused": paused,
        "erp_status": update.get("status"),
        "prev_stripe_status": prev_status,
        "prev_paused": prev_paused,
    }


def sync_subscription_from_webhook_event(event: dict):
    stripe_sub = (event or {}).get("data", {}).get("object", {}) or {}
    stripe_sub_id = stripe_sub.get("id")
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id"}

    sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_sub_id}, "name")
    if not sub_name:
        metadata = stripe_sub.get("metadata") or {}
        candidate = metadata.get("docname") if metadata.get("doctype") == "Subscription" else None
        if candidate and frappe.db.exists("Subscription", candidate):
            sub_name = candidate
            items = _stripe_get(_stripe_get(stripe_sub, "items") or {}, "data") or []
            _set_subscription_fields(
                sub_name,
                {
                    "stripe_customer_id": stripe_sub.get("customer") or "",
                    "stripe_subscription_id": stripe_sub_id,
                    "stripe_subscription_item_id": _stripe_get(items[0], "id") if items else "",
                },
            )
    if not sub_name:
        return {"handled": False, "reason": "subscription_not_found", "stripe_subscription_id": stripe_sub_id}

    out = _apply_subscription_state(sub_name, stripe_sub)
    out["handled"] = True

    company = frappe.db.get_value("Subscription", sub_name, "company")
    company_abbr = get_company_abbr_from_company(company)
    kind = _pick_lifecycle_kind(
        out.get("prev_stripe_status"),
        bool(out.get("prev_paused")),
        out.get("stripe_status"),
        bool(out.get("paused")),
    )

    if kind and company_abbr in ALLOWED_COMPANY_ABBR:
        try:
            email_out = _send_lifecycle_email(sub_name, company_abbr, kind, stripe_sub)
            out["email"] = email_out
            out["lifecycle_kind"] = kind
        except Exception as e:
            out["email"] = {"sent": False, "reason": str(e)[:300]}

    return out


@frappe.whitelist()
def reconcile_subscription_status(subscription_name: str):
    sub = _require_subscription_permission(subscription_name, "read")
    stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_name}

    company_abbr = _validate_company_for_stripe(sub.company)
    remote = stripe.Subscription.retrieve(
        stripe_sub_id,
        api_key=get_api_key(company_abbr),
    )
    return _apply_subscription_state(sub.name, dict(remote))


@frappe.whitelist()
def sync_subscription_action(subscription_name: str, action: str):
    _require_subscription_action_role()
    sub = _require_subscription_permission(subscription_name, "write")
    if not _is_enabled():
        return {"handled": False, "reason": "subscription_sync_disabled"}
    return _sync_subscription(sub, action)


@frappe.whitelist()
def request_subscription_payment_method(subscription_name: str, send_email: int = 1):
    sub = _require_subscription_permission(subscription_name, "write")
    company_abbr = _validate_company_for_stripe(sub.company)

    if not sub.get("stripe_subscription_id"):
        saved_payment_result = ensure_stripe_subscription_for_subscription(
            subscription_name,
        )
        if saved_payment_result.get("created"):
            return {
                "ok": True,
                "subscription": subscription_name,
                "subscription_created": True,
                "reused_saved_payment_method": bool(
                    saved_payment_result.get("used_saved_payment_method")
                ),
                "stripe_subscription_id": saved_payment_result.get(
                    "stripe_subscription_id"
                ),
                "email_sent": False,
            }
        if saved_payment_result.get("reason") not in {
            "missing_payment_method",
            "already_linked",
        }:
            return {
                "ok": False,
                "subscription": subscription_name,
                "reason": saved_payment_result.get("reason") or "stripe_subscription_creation_failed",
            }

    to_email = _resolve_subscription_email(sub)
    _rotate_subscription_setup_token(subscription_name)
    checkout_url = _generate_subscription_setup_checkout_url(sub, company_abbr, to_email=to_email)

    if not checkout_url:
        return {
            "ok": False,
            "reason": "setup_checkout_url_missing",
            "subscription": subscription_name,
        }

    out = {
        "ok": True,
        "subscription": subscription_name,
        "checkout_url": checkout_url,
        "email_sent": False,
        "stripe_subscription_linked": bool(getattr(sub, "stripe_subscription_id", None)),
    }

    if int(send_email or 0):
        try:
            email_out = _send_lifecycle_email(subscription_name, company_abbr, "add_payment_method", {})
            out["email"] = email_out
            out["email_sent"] = bool((email_out or {}).get("sent"))
        except Exception as e:
            out["email"] = {"sent": False, "reason": str(e)[:300]}

    return out


def queue_subscription_action(subscription_name: str, action: str):
    action = _normalize_action(action)
    if not action:
        return None
    return frappe.enqueue(
        "stripe_integration.stripe_integration.subscription_sync.sync_subscription_action",
        queue="short",
        timeout=300,
        subscription_name=subscription_name,
        action=action,
        enqueue_after_commit=True,
    )


@frappe.whitelist()
def get_recent_subscription_sync_events(subscription_name: str, limit: int = 20):
    subscription = _require_subscription_permission(subscription_name, "read")
    stripe_sub_id = getattr(subscription, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return []

    return frappe.get_all(
        "Stripe Event Log",
        filters={"stripe_object_id": stripe_sub_id},
        fields=["name", "event_id", "event_type", "status", "error", "processed_at", "modified"],
        order_by="modified desc",
        limit_page_length=max(1, min(int(limit or 20), 100)),
    )



@frappe.whitelist()
def retry_failed_subscription_events(limit: int = 20):
    frappe.only_for("System Manager")
    rows = frappe.get_all(
        "Stripe Event Log",
        filters={"status": "Failed"},
        fields=["name", "event_id", "event_type", "stripe_object_id", "error", "modified"],
        order_by="modified desc",
        limit_page_length=max(1, min(int(limit or 20), 100)),
    )

    out = []
    for r in rows:
        et = (r.get("event_type") or "").strip().lower()
        if not et.startswith("customer.subscription."):
            continue

        sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": r.get("stripe_object_id")}, "name")
        if not sub_name:
            out.append({"event_id": r.get("event_id"), "retried": False, "reason": "subscription_not_found"})
            continue

        try:
            subscription = frappe.get_doc("Subscription", sub_name)
            company_abbr = _validate_company_for_stripe(subscription.company)
            remote = stripe.Subscription.retrieve(
                r.get("stripe_object_id"),
                api_key=get_api_key(company_abbr),
            )
            result = _apply_subscription_state(sub_name, dict(remote))
            mark_event_status(r.get("event_id"), "Completed")
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "result": result})
        except Exception as e:
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "error": str(e)[:300]})

    return out


@frappe.whitelist()
def get_subscription_sync_health(hours: int = 24):
    frappe.only_for("System Manager")
    hours = max(1, min(int(hours or 24), 168))
    failed = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Failed' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]
    completed = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Completed' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]
    ignored = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Ignored' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]

    return {
        "window_hours": hours,
        "completed": int(completed),
        "failed": int(failed),
        "ignored": int(ignored),
    }

def _enforce_subscription_billing_defaults(doc):
    # Keep ERP subscription invoicing fully automatic.
    # We use db_set so this still works on submitted subscriptions where normal field updates are blocked.
    try:
        if int(getattr(doc, "submit_invoice", 0) or 0) != 1:
            doc.db_set("submit_invoice", 1, update_modified=False)
    except Exception:
        pass

    try:
        if (getattr(doc, "generate_invoice_at", None) or "") != "Beginning of the current subscription period":
            doc.db_set("generate_invoice_at", "Beginning of the current subscription period", update_modified=False)
    except Exception:
        pass


def on_subscription_update(doc, method=None):
    _enforce_subscription_billing_defaults(doc)

    if not _is_enabled():
        return

    if not getattr(doc, "stripe_subscription_id", None):
        return

    status = (doc.status or "").lower().strip()
    if status in ("cancelled", "canceled"):
        queue_subscription_action(doc.name, "cancel")
        return

    action = _normalize_action(getattr(doc, "stripe_sync_action", None))
    if action in ("pause", "resume", "plan_change"):
        queue_subscription_action(doc.name, action)
        try:
            frappe.db.set_value("Subscription", doc.name, "stripe_sync_action", "", update_modified=False)
        except Exception:
            pass


@frappe.whitelist(allow_guest=True)
def open_subscription_setup_link(subscription_name: str, token: str | None = None):
    if not _subscription_setup_token_valid(subscription_name, token):
        frappe.throw("Invalid or missing subscription setup token", frappe.PermissionError)

    sub = frappe.get_doc("Subscription", subscription_name)
    company_abbr = _validate_company_for_stripe(sub.company)
    to_email = _resolve_subscription_email(sub)
    checkout_url = _generate_subscription_setup_checkout_url(sub, company_abbr, to_email=to_email)
    if not checkout_url:
        frappe.throw("Unable to generate a fresh Stripe setup link")

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = checkout_url
    return
