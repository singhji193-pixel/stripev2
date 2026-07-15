frappe.ui.form.on("Subscription", {
  setup(frm) {
    const companyFilter = () => ({ filters: { company: frm.doc.company || "" } });
    frm.set_query("sales_tax_template", companyFilter);

    // Do NOT filter Subscription Plan by `company` in client query.
    // On this stack, users may not have permission to read `Subscription Plan.company`,
    // which causes: "You do not have permission to access field: Subscription Plan.company".
    // Company safety is enforced server-side during bootstrap/validation.
  },

  company(frm) {
    frm.set_value("sales_tax_template", null);
    (frm.doc.plans || []).forEach((row) => {
      frappe.model.set_value(row.doctype, row.name, "plan", null);
    });
  },

  refresh(frm) {
    removeLegacyButtons(frm);
    stripLegacyActionMenuDom();
    setTimeout(() => { removeLegacyButtons(frm); stripLegacyActionMenuDom(); }, 200);
    setTimeout(() => { removeLegacyButtons(frm); stripLegacyActionMenuDom(); }, 900);

    addStripeAction(frm, __("Request Payment Method"), () => requestPaymentMethod(frm));
    addStripeAction(frm, __("Pause"), () => runStripeAction(frm, "pause"));
    addStripeAction(frm, __("Resume"), () => runStripeAction(frm, "resume"));
    addStripeAction(frm, __("Cancel"), () => {
      frappe.confirm(
        __("Cancel this subscription in Stripe? This cannot be undone."),
        () => runStripeAction(frm, "cancel")
      );
    });
    addStripeAction(frm, __("View Sync Log"), () => showSyncLog(frm));
  }
});


function addStripeAction(frm, label, handler) {
  const group = __("Stripe");
  try { frm.remove_custom_button(label, group); } catch (e) {}
  frm.add_custom_button(label, handler, group);
}


function removeLegacyButtons(frm) {
  [
    __("Fetch Subscription Updates"),
    __("Force-Fetch Subscription Updates"),
    __("Cancel Subscription"),
    "Fetch Subscription Updates",
    "Force-Fetch Subscription Updates",
    "Cancel Subscription"
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
    "Cancel Subscription"
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

      if (out.subscription_created) {
        frappe.show_alert({
          message: out.reused_saved_payment_method
            ? __("Stripe subscription created using the customer's saved payment method.")
            : __("Stripe subscription created."),
          indicator: "green"
        });
        frm.reload_doc();
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

function runStripeAction(frm, action) {
  if (!frm.doc.stripe_subscription_id) {
    frappe.msgprint({
      title: __("Missing Stripe Subscription ID"),
      indicator: "orange",
      message: __("This subscription is not linked to Stripe yet. Set stripe_subscription_id first, then retry.")
    });
    return;
  }

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
