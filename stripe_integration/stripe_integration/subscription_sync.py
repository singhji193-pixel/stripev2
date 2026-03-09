import json

import frappe
import stripe

from stripe_integration.stripe_integration.utils import get_company_abbr_from_company, get_api_key
from stripe_integration.stripe_integration.event_log import upsert_event, mark_event_status

ALLOWED_COMPANY_ABBR = {"COE", "COSL"}
VALID_ACTIONS = {"pause", "resume", "cancel", "plan_change"}


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
    # Safety checks to avoid unnecessary API mutations
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
            # reserved for next batch when ERP plan->Stripe price mapping is finalized
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


@frappe.whitelist()
def sync_subscription_action(subscription_name: str, action: str):
    if not _is_enabled():
        return {"handled": False, "reason": "subscription_sync_disabled"}
    sub = frappe.get_doc("Subscription", subscription_name)
    return _sync_subscription(sub, action)


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


def on_subscription_update(doc, method=None):
    if not _is_enabled():
        return

    # Guardrail: no Stripe ID means nothing to sync
    if not getattr(doc, "stripe_subscription_id", None):
        return

    status = (doc.status or "").lower().strip()
    if status in ("cancelled", "canceled"):
        queue_subscription_action(doc.name, "cancel")
        return

    # optional explicit action hook via custom field (if present)
    action = _normalize_action(getattr(doc, "stripe_sync_action", None))
    if action in ("pause", "resume", "plan_change"):
        queue_subscription_action(doc.name, action)
        # clear one-shot action field to prevent accidental re-queue loops
        try:
            frappe.db.set_value("Subscription", doc.name, "stripe_sync_action", "", update_modified=False)
        except Exception:
            pass
