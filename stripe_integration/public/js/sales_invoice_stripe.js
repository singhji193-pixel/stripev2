frappe.ui.form.on("Sales Invoice", {
  refresh(frm) {
    if (frm.doc.docstatus !== 1) return;

    const hasActiveStripeLink = !!(frm.doc.stripe_checkout_session_id || frm.doc.stripe_checkout_url);

    if (hasActiveStripeLink) {
      frm.add_custom_button("Void Stripe Link", () => {
        frappe.confirm(
          "This will void the current Stripe checkout link/session. Continue?",
          () => {
            frappe.call({
              method: "stripe_integration.stripe_integration.api.void_payment_link_stripe",
              args: { invoice_name: frm.doc.name },
              freeze: true,
              freeze_message: "Voiding Stripe payment link...",
              callback: (r) => {
                const res = r.message || {};
                if (!res.ok) {
                  frappe.msgprint({
                    title: "Stripe",
                    message: "Failed to void Stripe link/session. Check Error Log.",
                    indicator: "red"
                  });
                  return;
                }
                frappe.show_alert({ message: "Stripe link/session voided", indicator: "green" });
                frm.reload_doc();
              },
              error: () => {
                frappe.msgprint({
                  title: "Stripe",
                  message: "Error calling server method. Check Error Log.",
                  indicator: "red"
                });
              }
            });
          }
        );
      }, "Payments");
    }

    if ((frm.doc.outstanding_amount || 0) <= 0) return;
    if (frm.doc.status === "Paid") return;

    const splitType = (frm.doc.payment_split_type || "Full Payment").trim();
    const processed = !!frm.doc.custom_stripe_payment_processed;

    let label = "Request Payment (Stripe)";
    if (splitType === "Split Payment") {
      label = processed ? "Request Remaining Balance (Stripe)" : "Request Deposit (Stripe)";
    }

    frm.add_custom_button(label, () => {
      frappe.call({
        method: "stripe_integration.stripe_integration.api.request_payment_stripe",
        args: { invoice_name: frm.doc.name },
        freeze: true,
        freeze_message: "Creating Stripe payment link...",
        callback: (r) => {
          const res = r.message || {};
          if (!res.ok) {
            frappe.msgprint({
              title: "Stripe",
              message: "Failed to generate payment link. Check Error Log.",
              indicator: "red"
            });
            return;
          }

          const url = res.checkout_url;
          const mode = res.mode || "";
          const kind = res.request_kind || "";
          const amt = res.amount;
          const cur = res.currency;

          frappe.msgprint({
            title: "Stripe link generated",
            indicator: "green",
            message: `Type: <b>${kind}</b><br>Amount: <b>${cur} ${amt}</b><br>Mode: <b>${mode}</b><br><br><a href="${url}" target="_blank">${url}</a><br><br><small>Email sent to customer.</small>`
          });

          frm.reload_doc();
        },
        error: () => {
          frappe.msgprint({
            title: "Stripe",
            message: "Error calling server method. Check Error Log.",
            indicator: "red"
          });
        }
      });
    }, "Payments");
  }
});
