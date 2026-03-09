frappe.ui.form.on("Subscription", {
  refresh(frm) {
    const stripeSubId = frm.doc.stripe_subscription_id;
    if (!stripeSubId) return;

    frm.add_custom_button(__("Stripe: Pause"), () => runStripeAction(frm, "pause"));
    frm.add_custom_button(__("Stripe: Resume"), () => runStripeAction(frm, "resume"));
    frm.add_custom_button(__("Stripe: Cancel"), () => {
      frappe.confirm(
        __("Cancel this subscription in Stripe? This cannot be undone."),
        () => runStripeAction(frm, "cancel")
      );
    });

    frm.add_custom_button(__("Stripe: View Sync Log"), () => showSyncLog(frm));
  }
});

function runStripeAction(frm, action) {
  frappe.call({
    method: "stripe_integration.stripe_integration.subscription_sync.sync_subscription_action",
    args: {
      subscription_name: frm.doc.name,
      action
    },
    freeze: true,
    freeze_message: __("Syncing subscription with Stripe..."),
    callback: (r) => {
      const out = r.message || {};
      if (out.handled) {
        frappe.show_alert({ message: __("Stripe sync completed: {0}", [action]), indicator: "green" });
      } else {
        frappe.msgprint({
          title: __("Stripe Sync Not Applied"),
          indicator: "orange",
          message: __("Reason: {0}", [out.reason || "unknown"])
        });
      }
      frm.reload_doc();
    }
  });
}

function showSyncLog(frm) {
  frappe.call({
    method: "stripe_integration.stripe_integration.subscription_sync.get_recent_subscription_sync_events",
    args: {
      subscription_name: frm.doc.name,
      limit: 20
    },
    callback: (r) => {
      const rows = r.message || [];
      if (!rows.length) {
        frappe.msgprint(__("No Stripe sync log entries found for this subscription."));
        return;
      }

      const htmlRows = rows.map((row) =>
        `<tr>
          <td>${frappe.utils.escape_html(row.modified || "")}</td>
          <td>${frappe.utils.escape_html(row.event_type || "")}</td>
          <td>${frappe.utils.escape_html(row.status || "")}</td>
          <td>${frappe.utils.escape_html(row.error || "")}</td>
        </tr>`
      ).join("");

      const html = `<div style="max-height:380px;overflow:auto;">
          <table class="table table-bordered" style="margin-top:8px;">
            <thead>
              <tr><th>When</th><th>Event</th><th>Status</th><th>Error</th></tr>
            </thead>
            <tbody>${htmlRows}</tbody>
          </table>
        </div>`;

      frappe.msgprint({
        title: __("Stripe Sync Log"),
        message: html,
        wide: true
      });
    }
  });
}
