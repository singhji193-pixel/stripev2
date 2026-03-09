import frappe

def run():
    script_name = "Sales Invoice - Request Payment (Stripe)"
    module = "Stripe Integration"

    if frappe.db.exists("Client Script", script_name):
        doc = frappe.get_doc("Client Script", script_name)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = script_name

    doc.dt = "Sales Invoice"
    doc.module = module
    doc.enabled = 1

    doc.script = """
frappe.ui.form.on('Sales Invoice', {
    refresh: function(frm) {
        if (frm.doc.docstatus !== 1) return;
        if ((frm.doc.outstanding_amount || 0) <= 0) return;
        if (frm.doc.status === 'Paid') return;

        // Add to standard Custom Buttons
        frm.add_custom_button(__('Request Payment (Stripe)'), function() {
            frappe.call({
                method: 'stripe_integration.stripe_integration.api.request_payment_stripe',
                args: { invoice_name: frm.doc.name },
                freeze: true,
                freeze_message: __('Creating Stripe payment link...'),
                callback: function(r) {
                    const res = r.message || {};
                    if (!res.ok) {
                        frappe.msgprint({
                            title: __('Stripe'),
                            message: __('Failed to generate payment link. Check Error Log.'),
                            indicator: 'red'
                        });
                        return;
                    }

                    const url = res.checkout_url;
                    frappe.msgprint({
                        title: __('Stripe payment link generated'),
                        indicator: 'green',
                        message: `<a href="${url}" target="_blank">${url}</a><br><br><small>Email sent to customer.</small>`
                    });

                    frm.reload_doc();
                },
                error: function() {
                    frappe.msgprint({
                        title: __('Stripe'),
                        message: __('Error calling server method. Check Error Log.'),
                        indicator: 'red'
                    });
                }
            });
        }, __('Payments'));
        
        // Also explicitly add to Actions menu for Mobile views where Custom Buttons might be hidden
        if (frm.page && frm.page.add_action_item) {
            frm.page.add_action_item(__('Request Payment (Stripe)'), function() {
                frappe.call({
                    method: 'stripe_integration.stripe_integration.api.request_payment_stripe',
                    args: { invoice_name: frm.doc.name },
                    freeze: true,
                    freeze_message: __('Creating Stripe payment link...'),
                    callback: function(r) {
                        const res = r.message || {};
                        if (!res.ok) {
                            frappe.msgprint({
                                title: __('Stripe'),
                                message: __('Failed to generate payment link. Check Error Log.'),
                                indicator: 'red'
                            });
                            return;
                        }

                        const url = res.checkout_url;
                        frappe.msgprint({
                            title: __('Stripe payment link generated'),
                            indicator: 'green',
                            message: `<a href="${url}" target="_blank">${url}</a><br><br><small>Email sent to customer.</small>`
                        });

                        frm.reload_doc();
                    }
                });
            });
        }
    }
});
    """

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print("Client script created/updated successfully")
