import frappe

from erpnext.setup.install import setup_currency_exchange


def execute():
	frappe.reload_doc("accounts", "doctype", "currency_exchange_settings")
	setup_currency_exchange()
