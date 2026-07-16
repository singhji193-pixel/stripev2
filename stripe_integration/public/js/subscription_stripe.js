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
    removeStripeLifecycleButtons(frm);

    if (frm.is_new() || Number(frm.doc.custom_do_not_generate_invoices || 0)) return;

    addStripeAction(frm, __("Request Payment Method"), () => requestPaymentMethod(frm));
    if (!frm.doc.stripe_subscription_id) return;

    const canManageLifecycle = ["System Manager", "Accounts Manager"]
      .some((role) => (frappe.user_roles || []).includes(role));
    if (canManageLifecycle) {
      const isPaused = Boolean(
        Number(frm.doc.stripe_erpnext_pause_active || 0) ||
        Number(frm.doc.stripe_paused || 0)
      );
      const pauseState = frm.doc.stripe_pause_state || "";
      if (pauseState === "Pausing") {
        addStripeAction(frm, __("Retry Pause"), () => runStripeAction(frm, "pause"));
      } else if (pauseState === "Resuming") {
        addStripeAction(frm, __("Retry Resume"), () => runStripeAction(frm, "resume"));
      } else if (pauseState === "Cancelling") {
        addStripeAction(frm, __("Retry Cancel"), () => runStripeAction(frm, "cancel"));
      } else if (isPaused) {
        addStripeAction(frm, __("Resume"), () => confirmResume(frm));
      } else {
        addStripeAction(frm, __("Pause"), () => pauseSubscription(frm));
      }
      if (pauseState !== "Cancelling") {
        addStripeAction(frm, __("Cancel"), () => {
          frappe.confirm(
            __("Cancel this subscription in Stripe? This cannot be undone."),
            () => runStripeAction(frm, "cancel")
          );
        });
      }
    }
    addStripeAction(frm, __("View Sync Log"), () => showSyncLog(frm));
  }
});


function addStripeAction(frm, label, handler) {
  const group = __("Stripe");
  try { frm.remove_custom_button(label, group); } catch (e) {}
  frm.add_custom_button(label, handler, group);
}


function removeStripeLifecycleButtons(frm) {
  const group = __("Stripe");
  [__("Pause"), __("Resume"), __("Cancel"), __("Retry Pause"), __("Retry Resume"), __("Retry Cancel")].forEach((label) => {
    try { frm.remove_custom_button(label, group); } catch (e) {}
  });
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

function pauseSubscription(frm) {
  const pauseStart = frm.doc.current_invoice_start || __("the next billing period");
  frappe.prompt(
    [
      {
        fieldname: "pause_cycles",
        fieldtype: "Int",
        label: __("Billing Cycles"),
        default: 1,
        reqd: 1,
        description: __(
          "The hold begins on {0}. Existing invoices are unchanged; the fixed end date is extended by the skipped cycles.",
          [pauseStart]
        )
      }
    ],
    (values) => runStripeAction(frm, "pause", { pause_cycles: values.pause_cycles }),
    __("Pause Subscription Billing"),
    __("Pause")
  );
}


function confirmResume(frm) {
  frappe.confirm(
    __("Resume this subscription? Billing will restart on the next aligned billing boundary."),
    () => runStripeAction(frm, "resume")
  );
}


function runStripeAction(frm, action, extraArgs = {}) {
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
    args: Object.assign({
      subscription_name: frm.doc.name,
      action
    }, extraArgs),
    freeze: true,
    freeze_message: __("Syncing subscription with Stripe..."),
    callback: (r) => {
      const out = r.message || {};
      if (out.handled) {
        const boundary = out.resume_on ? ` (${out.resume_on})` : "";
        frappe.show_alert({
          message: out.scheduled
            ? __("Stripe resume scheduled for {0}.", [out.resume_on])
            : __("Stripe sync completed: {0}{1}", [action, boundary]),
          indicator: "green"
        });
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
