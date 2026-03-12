frappe.ui.form.on("Sales Invoice", {
  refresh(frm) {
    if (frm.doc.docstatus !== 1) return;

    const allowedRoles = ["System Manager", "Accounts Manager", "Accounts User"];
    const hasStripeActionRole = allowedRoles.some((role) => frappe.user.has_role(role));
    if (!hasStripeActionRole) return;

    const withPasswordGate = (action) => {
      frappe.prompt(
        [
          {
            fieldname: "gate_password",
            label: "Approval Password",
            fieldtype: "Password",
            reqd: 1,
            description: "Required for Stripe payment actions"
          }
        ],
        (values) => action(values.gate_password),
        "Stripe Approval",
        "Continue"
      );
    };

    const hasActiveStripeLink = !!(frm.doc.stripe_checkout_session_id || frm.doc.stripe_checkout_url);

    if (hasActiveStripeLink) {
      frm.add_custom_button("Void Stripe Link", () => {
        frappe.confirm(
          "This will void the current Stripe checkout link/session. Continue?",
          () => {
            withPasswordGate((gatePassword) => {
              frappe.call({
                method: "stripe_integration.stripe_integration.api.void_payment_link_stripe",
                args: { invoice_name: frm.doc.name, gate_password: gatePassword },
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
            });
          }
        );
      }, "Payments");
    }

    const canRefund = !!frm.doc.stripe_payment_intent_id && (frm.doc.paid_amount || 0) > 0;
    if (canRefund) {
      frm.add_custom_button("Refund Stripe Payment", () => {
        frappe.confirm(
          "This creates a full Stripe refund and cancels the linked ERP Payment Entry. Continue?",
          () => {
            withPasswordGate((gatePassword) => {
              frappe.call({
                method: "stripe_integration.stripe_integration.api.refund_payment_stripe",
                args: { invoice_name: frm.doc.name, gate_password: gatePassword },
                freeze: true,
                freeze_message: "Creating Stripe refund...",
                callback: (r) => {
                  const res = r.message || {};
                  if (!res.ok) {
                    frappe.msgprint({
                      title: "Stripe",
                      message: "Failed to refund payment. Check Error Log.",
                      indicator: "red"
                    });
                    return;
                  }
                  frappe.show_alert({
                    message: `Refund ${res.refund_id || ""} created (${res.currency} ${res.amount})`,
                    indicator: "green"
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
      withPasswordGate((gatePassword) => {
        frappe.call({
          method: "stripe_integration.stripe_integration.api.request_payment_stripe",
          args: { invoice_name: frm.doc.name, gate_password: gatePassword },
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
      });
    }, "Payments");
  }
});
