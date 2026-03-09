import hashlib
import frappe
from frappe.utils import now


def _extract_object_id(event: dict) -> str | None:
    try:
        obj = (event or {}).get("data", {}).get("object", {})
        return obj.get("id") or obj.get("payment_intent") or obj.get("subscription")
    except Exception:
        return None


def upsert_event(event: dict, payload: bytes, company_abbr: str | None = None, request_id: str | None = None, status: str = "Queued"):
    event_id = (event or {}).get("id")
    if not event_id:
        return None

    company = None
    if company_abbr:
        company = frappe.db.get_value("Company", {"abbr": company_abbr}, "name")

    payload_hash = hashlib.sha256(payload or b"").hexdigest()
    values = {
        "event_id": event_id,
        "event_type": (event or {}).get("type"),
        "company": company,
        "company_abbr": company_abbr,
        "stripe_object_id": _extract_object_id(event),
        "request_id": request_id,
        "status": status,
        "payload_hash": payload_hash,
    }

    existing = frappe.db.get_value("Stripe Event Log", {"event_id": event_id}, "name")
    if existing:
        frappe.db.set_value("Stripe Event Log", existing, values, update_modified=False)
        return existing

    d = frappe.new_doc("Stripe Event Log")
    d.update(values)
    d.insert(ignore_permissions=True)
    return d.name


def mark_event_status(event_id: str, status: str, error: str | None = None):
    if not event_id:
        return
    name = frappe.db.get_value("Stripe Event Log", {"event_id": event_id}, "name")
    if not name:
        return
    update = {
        "status": status,
        "processed_at": now() if status in ("Completed", "Failed", "Ignored") else None,
    }
    if error:
        update["error"] = error[:2000]
    frappe.db.set_value("Stripe Event Log", name, update, update_modified=False)
