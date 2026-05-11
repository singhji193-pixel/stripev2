frappe.ui.form.on("Sales Invoice", {
  setup(frm) {
    const companyFilter = () => ({ filters: { company: frm.doc.company || "" } });

    frm.set_query("debit_to", companyFilter);
    frm.set_query("cost_center", companyFilter);
    frm.set_query("taxes_and_charges", () => ({
      filters: {
        company: frm.doc.company || "",
        docstatus: ["<", 2]
      }
    }));

    frm.set_query("income_account", "items", companyFilter);
    frm.set_query("expense_account", "items", companyFilter);
    frm.set_query("cost_center", "items", companyFilter);
  },

  company(frm) {
    // Clear potentially cross-company selections when company changes.
    ["debit_to", "cost_center", "taxes_and_charges"].forEach((f) => frm.set_value(f, null));
    (frm.doc.items || []).forEach((row) => {
      frappe.model.set_value(row.doctype, row.name, "income_account", null);
      frappe.model.set_value(row.doctype, row.name, "expense_account", null);
      frappe.model.set_value(row.doctype, row.name, "cost_center", null);
    });
  },

  refresh(frm) {
    // Keep print output brand-correct per company inside the ERP Print action.
    // Frappe stores this per-user as last_print_format for the doctype.
    const company = (frm.doc.company || "").trim();
    const desiredPrintFormat = company === "COEngine Service Inc."
      ? "COEngine Beautiful Invoice"
      : company === "CoreOrbit Systems Ltd."
        ? "CoreOrbit Beautiful Invoice"
        : null;

    if (desiredPrintFormat) {
      frappe.model.user_settings.save("Sales Invoice", {
        last_print_format: desiredPrintFormat
      });
    }

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

    const hasStripeRef = !!(frm.doc.stripe_payment_intent_id || frm.doc.stripe_invoice_id);
    const hasCollectedAmount = (frm.doc.status === "Paid" || frm.doc.status === "Partly Paid" || (frm.doc.grand_total || 0) > (frm.doc.outstanding_amount || 0));
    const isNotReturn = frm.doc.status !== "Return";
    const canRefund = hasStripeRef && hasCollectedAmount && isNotReturn;
    if (canRefund) {
      frm.add_custom_button("Refund Stripe Payment", () => {
        frappe.confirm(
          "This creates a Stripe refund and cancels the linked ERP Payment Entry. A submitted Credit Note is required first. Continue?",
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
                error: (err) => {
                  let message = "Error calling server method. Check Error Log.";
                  const raw = err?.message || err?.responseJSON?._server_messages || "";
                  if (String(raw).includes("credit_note_required_before_refund")) {
                    message = "Submit a Credit Note linked to this invoice before running Stripe refund.";
                  }
                  frappe.msgprint({
                    title: "Stripe",
                    message,
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
