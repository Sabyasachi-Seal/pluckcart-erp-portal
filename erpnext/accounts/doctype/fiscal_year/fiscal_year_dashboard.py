from frappe import _


def get_data():
	return {
		"fieldname": "fiscal_year",
		"transactions": [
			{"label": _("Budgets"), "items": ["Budget"]},
			{"label": _("References"), "items": ["Period Closing Voucher"]},
			{
				"label": _("Target Details"),
				"items": ["Sales Person", "Sales Partner", "Territory", "Monthly Distribution"],
			},
		],
	}
