// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('TaxJar Settings', {
	is_sandbox: (frm) => {
		frm.toggle_reqd("api_key", !frm.doc.is_sandbox);
		frm.toggle_reqd("sandbox_api_key", frm.doc.is_sandbox);
	},

	on_load: (frm) => {
		frm.set_query('shipping_account_head', function() {
			return {
				filters: {
					'company': frm.doc.company
				}
			};
		});
		frm.set_query('tax_account_head', function() {
			return {
				filters: {
					'company': frm.doc.company
				}
			};
		});
	},

	refresh: (frm) => {
		frm.add_custom_button(__('Update Nexus List'), function() {
			frm.call({
				doc: frm.doc,
				method: 'update_nexus_list'
			});
		});
	},


});
