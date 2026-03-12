frappe.ui.form.on("Subscription", {
  refresh(frm) {
    removeLegacyButtons(frm);
    stripLegacyActionMenuDom();
    setTimeout(() => { removeLegacyButtons(frm); stripLegacyActionMenuDom(); }, 200);
    setTimeout(() => { removeLegacyButtons(frm); stripLegacyActionMenuDom(); }, 900);

    frm.add_custom_button(__("Stripe: Request Payment Method"), () => requestPaymentMethod(frm));
    frm.add_custom_button(__("Stripe: View Sync Log"), () => showSyncLog(frm));
  }
});


function removeLegacyButtons(frm) {
  [
    __("Fetch Subscription Updates"),
    __("Force-Fetch Subscription Updates"),
    __("Cancel Subscription"),
    __("Stripe: Pause"),
    __("Stripe: Resume"),
    __("Stripe: Cancel"),
    "Fetch Subscription Updates",
    "Force-Fetch Subscription Updates",
    "Cancel Subscription",
    "Stripe: Pause",
    "Stripe: Resume",
    "Stripe: Cancel"
  ].forEach((label) => {
    try { frm.remove_custom_button(label); } catch (e) {}
    try { frm.page.remove_inner_button(label); } catch (e) {}
    try { frm.page.remove_menu_item(label); } catch (e) {}
    try { frm.page.remove_action_item(label); } catch (e) {}
  });
}

function stripLegacyActionMenuDom() {
  const blocked = [
    "Fetch Subscription Updates",
    "Force-Fetch Subscription Updates",
    "Cancel Subscription",
    "Stripe: Pause",
    "Stripe: Resume",
    "Stripe: Cancel"
  ];

  document.querySelectorAll('.dropdown-menu .dropdown-item, .actions-btn-group .btn, .menu-item').forEach((el) => {
    const t = (el.textContent || '').trim();
    if (blocked.includes(t)) {
      el.style.display = 'none';
      try { el.remove(); } catch (e) {}
    }
  });
}

function requestPaymentMethod(frm) {
  frappe.call({
    method: "stripe_integration.stripe_integration.subscription_sync.request_subscription_payment_method",
    args: {
      subscription_name: frm.doc.name,
      send_email: 1
    },
    freeze: true,
    freeze_message: __("Generating setup link and sending email..."),
    callback: (r) => {
      const out = r.message || {};
      if (!out.ok) {
        frappe.msgprint({
          title: __("Stripe"),
          indicator: "orange",
          message: __("Reason: {0}", [out.reason || "unknown"])
        });
        return;
      }

      const link = out.checkout_url || "";
      frappe.msgprint({
        title: __("Payment Method Request"),
        indicator: "green",
        message: `<div><a href="${link}" target="_blank">${link}</a><br><br>${out.email_sent ? "Email sent to customer." : "Link generated."}</div>`
      });
      frm.reload_doc();
    }
  });
}

function showSyncLog(frm) {
  if (!frm.doc.stripe_subscription_id) {
    frappe.msgprint({
      title: __("Missing Stripe Subscription ID"),
      indicator: "orange",
      message: __("No Stripe sync log is available until this subscription is linked to Stripe.")
    });
    return;
  }

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
