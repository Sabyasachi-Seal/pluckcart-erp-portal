from frappe import _


def get_data():
	return {
		"fieldname": "task",
		"transactions": [
			{"label": _("Activity"), "items": ["Timesheet"]},
		],
	}
