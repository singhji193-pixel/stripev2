import frappe
import stripe

from stripe_integration.stripe_integration.utils import get_company_abbr_from_company, get_api_key
from stripe_integration.stripe_integration.event_log import upsert_event, mark_event_status

ALLOWED_COMPANY_ABBR = {"COE", "COSL"}


def _is_enabled() -> bool:
    try:
        return int(frappe.db.get_single_value("Stripe Settings", "enable_subscription_state_sync") or 0) == 1
    except Exception:
        return False


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
        "data": {"object": {"id": getattr(subscription_doc, "stripe_subscription_id", None)}}
    }


def _sync_subscription(subscription_doc, action: str):
    stripe_sub_id = getattr(subscription_doc, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_doc.name}

    company_abbr = _set_api_key_for_company(subscription_doc.company)
    ev = _event_stub(subscription_doc, action)
    upsert_event(ev, payload=str(ev).encode(), company_abbr=company_abbr, status="Processing")

    try:
        if action == "cancel":
            stripe.Subscription.delete(stripe_sub_id)
        elif action == "resume":
            stripe.Subscription.modify(stripe_sub_id, pause_collection="")
        elif action == "pause":
            stripe.Subscription.modify(stripe_sub_id, pause_collection={"behavior": "void"})
        elif action == "plan_change":
            # marker path for batch3.1; concrete price change wiring in next batch when plan mapping is finalized
            pass
        else:
            mark_event_status(ev["id"], "Ignored", f"unsupported_action:{action}")
            return {"handled": False, "reason": "unsupported_action", "action": action}

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


@frappe.whitelist()
def sync_subscription_action(subscription_name: str, action: str):
    sub = frappe.get_doc("Subscription", subscription_name)
    return _sync_subscription(sub, action)


def queue_subscription_action(subscription_name: str, action: str):
    return frappe.enqueue(
        "stripe_integration.stripe_integration.subscription_sync.sync_subscription_action",
        queue="short",
        timeout=300,
        subscription_name=subscription_name,
        action=action,
        enqueue_after_commit=True,
    )


def on_subscription_update(doc, method=None):
    if not _is_enabled():
        return

    status = (doc.status or "").lower().strip()
    if status in ("cancelled", "canceled"):
        queue_subscription_action(doc.name, "cancel")
        return

    # optional explicit action hook via custom field (if present)
    action = getattr(doc, "stripe_sync_action", None)
    if action in ("pause", "resume", "plan_change"):
        queue_subscription_action(doc.name, action)
