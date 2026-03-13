import json
import frappe
import stripe

from frappe.utils import get_url
from stripe_integration.stripe_integration.utils import get_company_abbr_from_company, get_api_key
from stripe_integration.stripe_integration.event_log import upsert_event, mark_event_status

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


def _set_api_key_for_company(company: str):
    company_abbr = get_company_abbr_from_company(company)
    if company_abbr not in ALLOWED_COMPANY_ABBR:
        frappe.throw(f"Company {company_abbr} not allowed for Stripe sync")
    stripe.api_key = get_api_key(company_abbr)
    return company_abbr


def _event_stub(subscription_doc, action: str):
    return {
        "id": f"local_outbound_{subscription_doc.name}_{action}",
        "type": f"subscription.{action}",
        "data": {"object": {"id": getattr(subscription_doc, "stripe_subscription_id", None)}},
    }


def _validate_transition(stripe_sub_id: str, action: str):
    if action not in {"pause", "resume"}:
        return True, None

    remote = stripe.Subscription.retrieve(stripe_sub_id)
    paused = bool(getattr(remote, "pause_collection", None))

    if action == "pause" and paused:
        return False, "already_paused"
    if action == "resume" and not paused:
        return False, "not_paused"
    return True, None


def _sync_subscription(subscription_doc, action: str):
    action = _normalize_action(action)
    if not action:
        return {"handled": False, "reason": "unsupported_action", "action": action}

    stripe_sub_id = getattr(subscription_doc, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_doc.name}

    company_abbr = _set_api_key_for_company(subscription_doc.company)
    ev = _event_stub(subscription_doc, action)
    upsert_event(ev, payload=json.dumps(ev).encode(), company_abbr=company_abbr, status="Processing")

    try:
        ok, reason = _validate_transition(stripe_sub_id, action)
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
            stripe.Subscription.delete(stripe_sub_id)
        elif action == "resume":
            stripe.Subscription.modify(stripe_sub_id, pause_collection="")
        elif action == "pause":
            stripe.Subscription.modify(stripe_sub_id, pause_collection={"behavior": "void"})
        elif action == "plan_change":
            mark_event_status(ev["id"], "Ignored", "plan_change_not_implemented")
            return {"handled": False, "reason": "plan_change_not_implemented", "subscription": subscription_doc.name}

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
    for fn in ("contact_email", "email", "subscriber_email", "customer_email"):
        v = sub_doc.get(fn)
        if v:
            return v

    party_type = (sub_doc.get("party_type") or "").strip()
    party = sub_doc.get("party")
    if party_type == "Customer" and party:
        email = frappe.db.get_value("Customer", party, "email_id")
        if email:
            return email
    return None


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
            "sender": "CoreOrbit <next@coengine.ai>",
            "email_account": "CoreOrbit SMTP",
        }
    return {
        "sender": "COEngine <erp@coengine.ai>",
        "email_account": "COEngine SMTP",
    }


def _set_subscription_fields(sub_name: str, values: dict):
    meta = frappe.get_meta("Subscription")
    update = {k: v for k, v in (values or {}).items() if meta.get_field(k)}
    if update:
        frappe.db.set_value("Subscription", sub_name, update, update_modified=False)
        frappe.db.commit()


def _generate_subscription_setup_checkout_url(sub_doc, company_abbr: str, to_email: str | None = None):
    # Create a fresh setup-mode Checkout Session so customer adds a payment method
    # without immediate charge. This avoids stale/expired one-time payment links.
    stripe_sub_id = sub_doc.get("stripe_subscription_id")
    _set_api_key_for_company(sub_doc.get("company"))

    stripe_customer_id = None
    if stripe_sub_id:
        try:
            remote_sub = stripe.Subscription.retrieve(stripe_sub_id)
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
    try:
        si_name = frappe.db.get_value(
            "Sales Invoice",
            {"subscription": sub_doc.name, "docstatus": 1},
            "name",
            order_by="posting_date desc, posting_time desc, modified desc",
        )
        if not si_name:
            return None

        company_abbr = get_company_abbr_from_company(sub_doc.get("company"))
        pf = "CoreOrbit Beautiful Invoice" if company_abbr == "COSL" else "COEngine Beautiful Invoice"
        return frappe.attach_print("Sales Invoice", si_name, file_name=f"{si_name}.pdf", print_format=pf)
    except Exception:
        return None


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
        checkout_url = _generate_subscription_setup_checkout_url(sub_doc, company_abbr, to_email=to_email)
        if not checkout_url:
            return {"sent": False, "reason": "setup_checkout_url_missing", "template": template_name}

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
    if kind == "add_payment_method":
        inv_pdf = _build_subscription_invoice_attachment(sub_doc)
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
    return {"sent": True, "template": template_name, "to": to_email, "kind": kind}


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


@frappe.whitelist()
def sync_subscription_from_webhook_event(event: dict):
    stripe_sub = (event or {}).get("data", {}).get("object", {}) or {}
    stripe_sub_id = stripe_sub.get("id")
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id"}

    sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_sub_id}, "name")
    if not sub_name:
        return {"handled": False, "reason": "subscription_not_found", "stripe_subscription_id": stripe_sub_id}

    out = _apply_subscription_state(sub_name, stripe_sub)

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
    sub = frappe.get_doc("Subscription", subscription_name)
    stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_name}

    _set_api_key_for_company(sub.company)
    remote = stripe.Subscription.retrieve(stripe_sub_id)
    return _apply_subscription_state(sub.name, dict(remote))


@frappe.whitelist()
def sync_subscription_action(subscription_name: str, action: str):
    if not _is_enabled():
        return {"handled": False, "reason": "subscription_sync_disabled"}
    sub = frappe.get_doc("Subscription", subscription_name)
    return _sync_subscription(sub, action)


@frappe.whitelist()
def request_subscription_payment_method(subscription_name: str, send_email: int = 1):
    sub = frappe.get_doc("Subscription", subscription_name)
    company_abbr = _set_api_key_for_company(sub.company)
    to_email = _resolve_subscription_email(sub)
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
    subscription = frappe.get_doc("Subscription", subscription_name)
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
        if not et.startswith("subscription."):
            continue

        action = et.split(".", 1)[1] if "." in et else None
        action = _normalize_action(action)
        if not action or action == "plan_change":
            continue

        sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": r.get("stripe_object_id")}, "name")
        if not sub_name:
            out.append({"event_id": r.get("event_id"), "retried": False, "reason": "subscription_not_found"})
            continue

        try:
            result = sync_subscription_action(sub_name, action)
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "action": action, "result": result})
        except Exception as e:
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "action": action, "error": str(e)[:300]})

    return out


@frappe.whitelist()
def get_subscription_sync_health(hours: int = 24):
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
