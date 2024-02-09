# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
from typing import Optional, Set, Tuple

import frappe
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.query_builder.functions import CombineDatetime, Sum
from frappe.utils import cint, cstr, flt, get_link_to_form, getdate, now, nowdate

import erpnext
from erpnext.stock.doctype.bin.bin import update_qty as update_bin_qty
from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from erpnext.stock.utils import (
	get_incoming_outgoing_rate_for_cancel,
	get_incoming_rate,
	get_or_make_bin,
	get_valuation_method,
)
from erpnext.stock.valuation import FIFOValuation, LIFOValuation, round_off_if_near_zero


class NegativeStockError(frappe.ValidationError):
	pass


def make_sl_entries(sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
	"""Create SL entries from SL entry dicts

	args:
	        - allow_negative_stock: disable negative stock valiations if true
	        - via_landed_cost_voucher: landed cost voucher cancels and reposts
	        entries of purchase document. This flag is used to identify if
	        cancellation and repost is happening via landed cost voucher, in
	        such cases certain validations need to be ignored (like negative
	                        stock)
	"""
	from erpnext.controllers.stock_controller import future_sle_exists

	if sl_entries:
		cancel = sl_entries[0].get("is_cancelled")
		if cancel:
			validate_cancellation(sl_entries)
			set_as_cancel(sl_entries[0].get("voucher_type"), sl_entries[0].get("voucher_no"))

		args = get_args_for_future_sle(sl_entries[0])
		future_sle_exists(args, sl_entries)

		for sle in sl_entries:
			if cancel:
				sle["actual_qty"] = -flt(sle.get("actual_qty"))

				if sle["actual_qty"] < 0 and not sle.get("outgoing_rate"):
					sle["outgoing_rate"] = get_incoming_outgoing_rate_for_cancel(
						sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
					)
					sle["incoming_rate"] = 0.0

				if sle["actual_qty"] > 0 and not sle.get("incoming_rate"):
					sle["incoming_rate"] = get_incoming_outgoing_rate_for_cancel(
						sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
					)
					sle["outgoing_rate"] = 0.0

			if sle.get("actual_qty") or sle.get("voucher_type") == "Stock Reconciliation":
				sle_doc = make_entry(sle, allow_negative_stock, via_landed_cost_voucher)

			args = sle_doc.as_dict()
			args["allow_zero_valuation_rate"] = sle.get("allow_zero_valuation_rate") or False

			if sle.get("voucher_type") == "Stock Reconciliation":
				# preserve previous_qty_after_transaction for qty reposting
				args.previous_qty_after_transaction = sle.get("previous_qty_after_transaction")

			is_stock_item = frappe.get_cached_value("Item", args.get("item_code"), "is_stock_item")
			if is_stock_item:
				bin_name = get_or_make_bin(args.get("item_code"), args.get("warehouse"))
				repost_current_voucher(args, allow_negative_stock, via_landed_cost_voucher)
				update_bin_qty(bin_name, args)
			else:
				frappe.msgprint(
					_("Item {0} ignored since it is not a stock item").format(args.get("item_code"))
				)


def repost_current_voucher(args, allow_negative_stock=False, via_landed_cost_voucher=False):
	if args.get("actual_qty") or args.get("voucher_type") == "Stock Reconciliation":
		if not args.get("posting_date"):
			args["posting_date"] = nowdate()

		if not (args.get("is_cancelled") and via_landed_cost_voucher):
			# Reposts only current voucher SL Entries
			# Updates valuation rate, stock value, stock queue for current transaction
			update_entries_after(
				{
					"item_code": args.get("item_code"),
					"warehouse": args.get("warehouse"),
					"posting_date": args.get("posting_date"),
					"posting_time": args.get("posting_time"),
					"voucher_type": args.get("voucher_type"),
					"voucher_no": args.get("voucher_no"),
					"sle_id": args.get("name"),
					"creation": args.get("creation"),
				},
				allow_zero_rate=args.get("allow_zero_valuation_rate") or False,
				allow_negative_stock=allow_negative_stock,
				via_landed_cost_voucher=via_landed_cost_voucher,
			)

		# update qty in future sle and Validate negative qty
		# For LCV: update future balances with -ve LCV SLE, which will be balanced by +ve LCV SLE
		update_qty_in_future_sle(args, allow_negative_stock)


def get_args_for_future_sle(row):
	return frappe._dict(
		{
			"voucher_type": row.get("voucher_type"),
			"voucher_no": row.get("voucher_no"),
			"posting_date": row.get("posting_date"),
			"posting_time": row.get("posting_time"),
		}
	)


def validate_cancellation(args):
	if args[0].get("is_cancelled"):
		repost_entry = frappe.db.get_value(
			"Repost Item Valuation",
			{"voucher_type": args[0].voucher_type, "voucher_no": args[0].voucher_no, "docstatus": 1},
			["name", "status"],
			as_dict=1,
		)

		if repost_entry:
			if repost_entry.status == "In Progress":
				frappe.throw(
					_(
						"Cannot cancel the transaction. Reposting of item valuation on submission is not completed yet."
					)
				)
			if repost_entry.status == "Queued":
				doc = frappe.get_doc("Repost Item Valuation", repost_entry.name)
				doc.status = "Skipped"
				doc.flags.ignore_permissions = True
				doc.cancel()


def set_as_cancel(voucher_type, voucher_no):
	frappe.db.sql(
		"""update `tabStock Ledger Entry` set is_cancelled=1,
		modified=%s, modified_by=%s
		where voucher_type=%s and voucher_no=%s and is_cancelled = 0""",
		(now(), frappe.session.user, voucher_type, voucher_no),
	)


def make_entry(args, allow_negative_stock=False, via_landed_cost_voucher=False):
	args["doctype"] = "Stock Ledger Entry"
	sle = frappe.get_doc(args)
	sle.flags.ignore_permissions = 1
	sle.allow_negative_stock = allow_negative_stock
	sle.via_landed_cost_voucher = via_landed_cost_voucher
	sle.submit()

	# Added to handle the case when the stock ledger entry is created from the repostig
	if args.get("creation_time") and args.get("voucher_type") == "Stock Reconciliation":
		sle.db_set("creation", args.get("creation_time"))

	return sle


def repost_future_sle(
	args=None,
	voucher_type=None,
	voucher_no=None,
	allow_negative_stock=None,
	via_landed_cost_voucher=False,
	doc=None,
):
	if not args:
		args = []  # set args to empty list if None to avoid enumerate error

	items_to_be_repost = get_items_to_be_repost(
		voucher_type=voucher_type, voucher_no=voucher_no, doc=doc
	)
	if items_to_be_repost:
		args = items_to_be_repost

	distinct_item_warehouses = get_distinct_item_warehouse(args, doc)
	affected_transactions = get_affected_transactions(doc)

	i = get_current_index(doc) or 0
	while i < len(args):
		validate_item_warehouse(args[i])

		obj = update_entries_after(
			{
				"item_code": args[i].get("item_code"),
				"warehouse": args[i].get("warehouse"),
				"posting_date": args[i].get("posting_date"),
				"posting_time": args[i].get("posting_time"),
				"creation": args[i].get("creation"),
				"distinct_item_warehouses": distinct_item_warehouses,
			},
			allow_negative_stock=allow_negative_stock,
			via_landed_cost_voucher=via_landed_cost_voucher,
		)
		affected_transactions.update(obj.affected_transactions)

		distinct_item_warehouses[
			(args[i].get("item_code"), args[i].get("warehouse"))
		].reposting_status = True

		if obj.new_items_found:
			for item_wh, data in distinct_item_warehouses.items():
				if ("args_idx" not in data and not data.reposting_status) or (
					data.sle_changed and data.reposting_status
				):
					data.args_idx = len(args)
					args.append(data.sle)
				elif data.sle_changed and not data.reposting_status:
					args[data.args_idx] = data.sle

				data.sle_changed = False
		i += 1

		if doc:
			update_args_in_repost_item_valuation(
				doc, i, args, distinct_item_warehouses, affected_transactions
			)


def validate_item_warehouse(args):
	for field in ["item_code", "warehouse", "posting_date", "posting_time"]:
		if args.get(field) in [None, ""]:
			validation_msg = f"The field {frappe.unscrub(field)} is required for the reposting"
			frappe.throw(_(validation_msg))


def update_args_in_repost_item_valuation(
	doc, index, args, distinct_item_warehouses, affected_transactions
):
	doc.db_set(
		{
			"items_to_be_repost": json.dumps(args, default=str),
			"distinct_item_and_warehouse": json.dumps(
				{str(k): v for k, v in distinct_item_warehouses.items()}, default=str
			),
			"current_index": index,
			"affected_transactions": frappe.as_json(affected_transactions),
		}
	)

	if not frappe.flags.in_test:
		frappe.db.commit()

	frappe.publish_realtime(
		"item_reposting_progress",
		{"name": doc.name, "items_to_be_repost": json.dumps(args, default=str), "current_index": index},
		doctype=doc.doctype,
		docname=doc.name,
	)


def get_items_to_be_repost(voucher_type=None, voucher_no=None, doc=None):
	items_to_be_repost = []
	if doc and doc.items_to_be_repost:
		items_to_be_repost = json.loads(doc.items_to_be_repost) or []

	if not items_to_be_repost and voucher_type and voucher_no:
		items_to_be_repost = frappe.db.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": voucher_type, "voucher_no": voucher_no},
			fields=["item_code", "warehouse", "posting_date", "posting_time", "creation"],
			order_by="creation asc",
			group_by="item_code, warehouse",
		)

	return items_to_be_repost or []


def get_distinct_item_warehouse(args=None, doc=None):
	distinct_item_warehouses = {}
	if doc and doc.distinct_item_and_warehouse:
		distinct_item_warehouses = json.loads(doc.distinct_item_and_warehouse)
		distinct_item_warehouses = {
			frappe.safe_eval(k): frappe._dict(v) for k, v in distinct_item_warehouses.items()
		}
	else:
		for i, d in enumerate(args):
			distinct_item_warehouses.setdefault(
				(d.item_code, d.warehouse), frappe._dict({"reposting_status": False, "sle": d, "args_idx": i})
			)

	return distinct_item_warehouses


def get_affected_transactions(doc) -> Set[Tuple[str, str]]:
	if not doc.affected_transactions:
		return set()

	transactions = frappe.parse_json(doc.affected_transactions)
	return {tuple(transaction) for transaction in transactions}


def get_current_index(doc=None):
	if doc and doc.current_index:
		return doc.current_index


class update_entries_after(object):
	"""
	update valution rate and qty after transaction
	from the current time-bucket onwards

	:param args: args as dict

	        args = {
	                "item_code": "ABC",
	                "warehouse": "XYZ",
	                "posting_date": "2012-12-12",
	                "posting_time": "12:00"
	        }
	"""

	def __init__(
		self,
		args,
		allow_zero_rate=False,
		allow_negative_stock=None,
		via_landed_cost_voucher=False,
		verbose=1,
	):
		self.exceptions = {}
		self.verbose = verbose
		self.allow_zero_rate = allow_zero_rate
		self.via_landed_cost_voucher = via_landed_cost_voucher
		self.item_code = args.get("item_code")
		self.allow_negative_stock = allow_negative_stock or is_negative_stock_allowed(
			item_code=self.item_code
		)

		self.args = frappe._dict(args)
		if self.args.sle_id:
			self.args["name"] = self.args.sle_id

		self.company = frappe.get_cached_value("Warehouse", self.args.warehouse, "company")
		self.set_precision()
		self.valuation_method = get_valuation_method(self.item_code)

		self.new_items_found = False
		self.distinct_item_warehouses = args.get("distinct_item_warehouses", frappe._dict())
		self.affected_transactions: Set[Tuple[str, str]] = set()

		self.data = frappe._dict()
		self.initialize_previous_data(self.args)
		self.build()

	def set_precision(self):
		self.flt_precision = cint(frappe.db.get_default("float_precision")) or 2
		self.currency_precision = get_field_precision(
			frappe.get_meta("Stock Ledger Entry").get_field("stock_value")
		)

	def initialize_previous_data(self, args):
		"""
		Get previous sl entries for current item for each related warehouse
		and assigns into self.data dict

		:Data Structure:

		self.data = {
		        warehouse1: {
		                'previus_sle': {},
		                'qty_after_transaction': 10,
		                'valuation_rate': 100,
		                'stock_value': 1000,
		                'prev_stock_value': 1000,
		                'stock_queue': '[[10, 100]]',
		                'stock_value_difference': 1000
		        }
		}

		"""
		self.data.setdefault(args.warehouse, frappe._dict())
		warehouse_dict = self.data[args.warehouse]
		previous_sle = get_previous_sle_of_current_voucher(args)
		warehouse_dict.previous_sle = previous_sle

		for key in ("qty_after_transaction", "valuation_rate", "stock_value"):
			setattr(warehouse_dict, key, flt(previous_sle.get(key)))

		warehouse_dict.update(
			{
				"prev_stock_value": previous_sle.stock_value or 0.0,
				"stock_queue": json.loads(previous_sle.stock_queue or "[]"),
				"stock_value_difference": 0.0,
			}
		)

	def build(self):
		from erpnext.controllers.stock_controller import future_sle_exists

		if self.args.get("sle_id"):
			self.process_sle_against_current_timestamp()
			if not future_sle_exists(self.args):
				self.update_bin()
		else:
			entries_to_fix = self.get_future_entries_to_fix()

			i = 0
			while i < len(entries_to_fix):
				sle = entries_to_fix[i]
				i += 1

				self.process_sle(sle)
				self.update_bin_data(sle)

				if sle.dependant_sle_voucher_detail_no:
					entries_to_fix = self.get_dependent_entries_to_fix(entries_to_fix, sle)

		if self.exceptions:
			self.raise_exceptions()

	def process_sle_against_current_timestamp(self):
		sl_entries = self.get_sle_against_current_voucher()
		for sle in sl_entries:
			self.process_sle(sle)

	def get_sle_against_current_voucher(self):
		self.args["time_format"] = "%H:%i:%s"

		return frappe.db.sql(
			"""
			select
				*, timestamp(posting_date, posting_time) as "timestamp"
			from
				`tabStock Ledger Entry`
			where
				item_code = %(item_code)s
				and warehouse = %(warehouse)s
				and is_cancelled = 0
				and (
					posting_date = %(posting_date)s and
					time_format(posting_time, %(time_format)s) = time_format(%(posting_time)s, %(time_format)s)
				)
			order by
				creation ASC
			for update
		""",
			self.args,
			as_dict=1,
		)

	def get_future_entries_to_fix(self):
		# includes current entry!
		args = self.data[self.args.warehouse].previous_sle or frappe._dict(
			{"item_code": self.item_code, "warehouse": self.args.warehouse}
		)

		return list(self.get_sle_after_datetime(args))

	def get_dependent_entries_to_fix(self, entries_to_fix, sle):
		dependant_sle = get_sle_by_voucher_detail_no(
			sle.dependant_sle_voucher_detail_no, excluded_sle=sle.name
		)

		if not dependant_sle:
			return entries_to_fix
		elif (
			dependant_sle.item_code == self.item_code and dependant_sle.warehouse == self.args.warehouse
		):
			return entries_to_fix
		elif dependant_sle.item_code != self.item_code:
			self.update_distinct_item_warehouses(dependant_sle)
			return entries_to_fix
		elif dependant_sle.item_code == self.item_code and dependant_sle.warehouse in self.data:
			return entries_to_fix
		else:
			self.initialize_previous_data(dependant_sle)
			self.update_distinct_item_warehouses(dependant_sle)
			return entries_to_fix

	def update_distinct_item_warehouses(self, dependant_sle):
		key = (dependant_sle.item_code, dependant_sle.warehouse)
		val = frappe._dict({"sle": dependant_sle})

		if key not in self.distinct_item_warehouses:
			self.distinct_item_warehouses[key] = val
			self.new_items_found = True
		else:
			existing_sle_posting_date = (
				self.distinct_item_warehouses[key].get("sle", {}).get("posting_date")
			)

			dependent_voucher_detail_nos = self.get_dependent_voucher_detail_nos(key)

			if getdate(dependant_sle.posting_date) < getdate(existing_sle_posting_date):
				val.sle_changed = True
				dependent_voucher_detail_nos.append(dependant_sle.voucher_detail_no)
				val.dependent_voucher_detail_nos = dependent_voucher_detail_nos
				self.distinct_item_warehouses[key] = val
				self.new_items_found = True
			elif dependant_sle.voucher_detail_no not in set(dependent_voucher_detail_nos):
				# Future dependent voucher needs to be repost to get the correct stock value
				# If dependent voucher has not reposted, then add it to the list
				dependent_voucher_detail_nos.append(dependant_sle.voucher_detail_no)
				self.new_items_found = True
				val.dependent_voucher_detail_nos = dependent_voucher_detail_nos
				self.distinct_item_warehouses[key] = val

	def get_dependent_voucher_detail_nos(self, key):
		if "dependent_voucher_detail_nos" not in self.distinct_item_warehouses[key]:
			self.distinct_item_warehouses[key].dependent_voucher_detail_nos = []

		return self.distinct_item_warehouses[key].dependent_voucher_detail_nos

	def process_sle(self, sle):
		from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

		# previous sle data for this warehouse
		self.wh_data = self.data[sle.warehouse]
		self.affected_transactions.add((sle.voucher_type, sle.voucher_no))

		if (sle.serial_no and not self.via_landed_cost_voucher) or not cint(self.allow_negative_stock):
			# validate negative stock for serialized items, fifo valuation
			# or when negative stock is not allowed for moving average
			if not self.validate_negative_stock(sle):
				self.wh_data.qty_after_transaction += flt(sle.actual_qty)
				return

		# Get dynamic incoming/outgoing rate
		if not self.args.get("sle_id"):
			self.get_dynamic_incoming_outgoing_rate(sle)

		if (
			sle.voucher_type == "Stock Reconciliation"
			and not self.args.get("sle_id")
			and sle.voucher_detail_no
			and (sle.batch_no or sle.serial_no)
		):
			self.reset_actual_qty_for_stock_reco(sle)

		if (
			sle.voucher_type in ["Purchase Receipt", "Purchase Invoice"]
			and sle.voucher_detail_no
			and sle.actual_qty < 0
			and is_internal_transfer(sle)
		):
			sle.outgoing_rate = get_incoming_rate_for_inter_company_transfer(sle)

		dimensions = get_inventory_dimensions()
		has_dimensions = False
		if dimensions:
			for dimension in dimensions:
				if sle.get(dimension.get("fieldname")):
					has_dimensions = True

		if get_serial_nos(sle.serial_no):
			self.get_serialized_values(sle)
			self.wh_data.qty_after_transaction += flt(sle.actual_qty)
			if sle.voucher_type == "Stock Reconciliation" and not sle.batch_no:
				self.wh_data.qty_after_transaction = sle.qty_after_transaction

			self.wh_data.stock_value = flt(self.wh_data.qty_after_transaction) * flt(
				self.wh_data.valuation_rate
			)
		elif sle.batch_no and frappe.db.get_value(
			"Batch", sle.batch_no, "use_batchwise_valuation", cache=True
		):
			self.update_batched_values(sle)
		else:
			if sle.voucher_type == "Stock Reconciliation" and not sle.batch_no and not has_dimensions:
				# assert
				self.wh_data.valuation_rate = sle.valuation_rate
				self.wh_data.qty_after_transaction = sle.qty_after_transaction
				self.wh_data.stock_value = flt(self.wh_data.qty_after_transaction) * flt(
					self.wh_data.valuation_rate
				)
				if self.valuation_method != "Moving Average":
					self.wh_data.stock_queue = [[self.wh_data.qty_after_transaction, self.wh_data.valuation_rate]]
			else:
				if self.valuation_method == "Moving Average":
					self.get_moving_average_values(sle)
					self.wh_data.qty_after_transaction += flt(sle.actual_qty)
					self.wh_data.stock_value = flt(self.wh_data.qty_after_transaction) * flt(
						self.wh_data.valuation_rate
					)
				else:
					self.update_queue_values(sle)

		# rounding as per precision
		self.wh_data.stock_value = flt(self.wh_data.stock_value, self.currency_precision)
		if not self.wh_data.qty_after_transaction:
			self.wh_data.stock_value = 0.0
		stock_value_difference = self.wh_data.stock_value - self.wh_data.prev_stock_value
		self.wh_data.prev_stock_value = self.wh_data.stock_value

		# update current sle
		sle.qty_after_transaction = self.wh_data.qty_after_transaction
		sle.valuation_rate = self.wh_data.valuation_rate
		sle.stock_value = self.wh_data.stock_value
		sle.stock_queue = json.dumps(self.wh_data.stock_queue)

		if not sle.is_adjustment_entry or not self.args.get("sle_id"):
			sle.stock_value_difference = stock_value_difference

		sle.doctype = "Stock Ledger Entry"
		frappe.get_doc(sle).db_update()

		if not self.args.get("sle_id"):
			self.update_outgoing_rate_on_transaction(sle)

	def reset_actual_qty_for_stock_reco(self, sle):
		doc = frappe.get_cached_doc("Stock Reconciliation", sle.voucher_no)
		doc.recalculate_current_qty(sle.voucher_detail_no, sle.creation, sle.actual_qty >= 0)

		if sle.actual_qty < 0:
			stock_reco_details = frappe.db.get_value(
				"Stock Reconciliation Item",
				sle.voucher_detail_no,
				["current_qty", "current_serial_no as sn_no"],
				as_dict=True,
			)

			sle.actual_qty = flt(stock_reco_details.current_qty) * -1

			if stock_reco_details.sn_no:
				sle.serial_no = stock_reco_details.sn_no
				sle.qty_after_transaction = 0.0

		if sle.serial_no:
			self.update_serial_no_status(sle)

	def update_serial_no_status(self, sle):
		from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

		serial_nos = get_serial_nos(sle.serial_no)
		warehouse = None
		status = "Delivered"
		if sle.actual_qty > 0:
			warehouse = sle.warehouse
			status = "Active"

		sn_table = frappe.qb.DocType("Serial No")

		query = (
			frappe.qb.update(sn_table)
			.set(sn_table.warehouse, warehouse)
			.set(sn_table.status, status)
			.where(sn_table.name.isin(serial_nos))
		)

		if sle.actual_qty > 0:
			query = query.set(sn_table.purchase_document_type, sle.voucher_type)
			query = query.set(sn_table.purchase_document_no, sle.voucher_no)
			query = query.set(sn_table.delivery_document_type, None)
			query = query.set(sn_table.delivery_document_no, None)
		else:
			query = query.set(sn_table.delivery_document_type, sle.voucher_type)
			query = query.set(sn_table.delivery_document_no, sle.voucher_no)

		query.run()

	def validate_negative_stock(self, sle):
		"""
		validate negative stock for entries current datetime onwards
		will not consider cancelled entries
		"""
		diff = self.wh_data.qty_after_transaction + flt(sle.actual_qty)
		diff = flt(diff, self.flt_precision)  # respect system precision

		if diff < 0 and abs(diff) > 0.0001:
			# negative stock!
			exc = sle.copy().update({"diff": diff})
			self.exceptions.setdefault(sle.warehouse, []).append(exc)
			return False
		else:
			return True

	def get_dynamic_incoming_outgoing_rate(self, sle):
		# Get updated incoming/outgoing rate from transaction
		if sle.recalculate_rate:
			rate = self.get_incoming_outgoing_rate_from_transaction(sle)

			if flt(sle.actual_qty) >= 0:
				sle.incoming_rate = rate
			else:
				sle.outgoing_rate = rate

	def get_incoming_outgoing_rate_from_transaction(self, sle):
		rate = 0
		# Material Transfer, Repack, Manufacturing
		if sle.voucher_type == "Stock Entry":
			self.recalculate_amounts_in_stock_entry(sle.voucher_no)
			rate = frappe.db.get_value("Stock Entry Detail", sle.voucher_detail_no, "valuation_rate")
		# Sales and Purchase Return
		elif sle.voucher_type in (
			"Purchase Receipt",
			"Purchase Invoice",
			"Delivery Note",
			"Sales Invoice",
			"Subcontracting Receipt",
		):
			if frappe.get_cached_value(sle.voucher_type, sle.voucher_no, "is_return"):
				from erpnext.controllers.sales_and_purchase_return import (
					get_rate_for_return,  # don't move this import to top
				)

				if self.valuation_method == "Moving Average":
					rate = get_incoming_rate(
						{
							"item_code": sle.item_code,
							"warehouse": sle.warehouse,
							"posting_date": sle.posting_date,
							"posting_time": sle.posting_time,
							"qty": sle.actual_qty,
							"serial_no": sle.get("serial_no"),
							"batch_no": sle.get("batch_no"),
							"company": sle.company,
							"voucher_type": sle.voucher_type,
							"voucher_no": sle.voucher_no,
							"allow_zero_valuation": self.allow_zero_rate,
							"sle": sle.name,
						}
					)

				else:
					rate = get_rate_for_return(
						sle.voucher_type,
						sle.voucher_no,
						sle.item_code,
						voucher_detail_no=sle.voucher_detail_no,
						sle=sle,
					)
			elif (
				sle.voucher_type in ["Purchase Receipt", "Purchase Invoice"]
				and sle.voucher_detail_no
				and is_internal_transfer(sle)
			):
				rate = get_incoming_rate_for_inter_company_transfer(sle)
			else:
				if sle.voucher_type in ("Purchase Receipt", "Purchase Invoice"):
					rate_field = "valuation_rate"
				elif sle.voucher_type == "Subcontracting Receipt":
					rate_field = "rate"
				else:
					rate_field = "incoming_rate"

				# check in item table
				item_code, incoming_rate = frappe.db.get_value(
					sle.voucher_type + " Item", sle.voucher_detail_no, ["item_code", rate_field]
				)

				if item_code == sle.item_code:
					rate = incoming_rate
				else:
					if sle.voucher_type in ("Delivery Note", "Sales Invoice"):
						ref_doctype = "Packed Item"
					elif sle == "Subcontracting Receipt":
						ref_doctype = "Subcontracting Receipt Supplied Item"
					else:
						ref_doctype = "Purchase Receipt Item Supplied"

					rate = frappe.db.get_value(
						ref_doctype,
						{"parent_detail_docname": sle.voucher_detail_no, "item_code": sle.item_code},
						rate_field,
					)

		return rate

	def update_outgoing_rate_on_transaction(self, sle):
		"""
		Update outgoing rate in Stock Entry, Delivery Note, Sales Invoice and Sales Return
		In case of Stock Entry, also calculate FG Item rate and total incoming/outgoing amount
		"""
		if sle.actual_qty and sle.voucher_detail_no:
			outgoing_rate = abs(flt(sle.stock_value_difference)) / abs(sle.actual_qty)

			if flt(sle.actual_qty) < 0 and sle.voucher_type == "Stock Entry":
				self.update_rate_on_stock_entry(sle, outgoing_rate)
			elif sle.voucher_type in ("Delivery Note", "Sales Invoice"):
				self.update_rate_on_delivery_and_sales_return(sle, outgoing_rate)
			elif flt(sle.actual_qty) < 0 and sle.voucher_type in ("Purchase Receipt", "Purchase Invoice"):
				self.update_rate_on_purchase_receipt(sle, outgoing_rate)
			elif flt(sle.actual_qty) < 0 and sle.voucher_type == "Subcontracting Receipt":
				self.update_rate_on_subcontracting_receipt(sle, outgoing_rate)
		elif sle.voucher_type == "Stock Reconciliation":
			self.update_rate_on_stock_reconciliation(sle)

	def update_rate_on_stock_entry(self, sle, outgoing_rate):
		frappe.db.set_value("Stock Entry Detail", sle.voucher_detail_no, "basic_rate", outgoing_rate)

		# Update outgoing item's rate, recalculate FG Item's rate and total incoming/outgoing amount
		if not sle.dependant_sle_voucher_detail_no:
			self.recalculate_amounts_in_stock_entry(sle.voucher_no)

	def recalculate_amounts_in_stock_entry(self, voucher_no):
		stock_entry = frappe.get_doc("Stock Entry", voucher_no, for_update=True)
		stock_entry.calculate_rate_and_amount(reset_outgoing_rate=False, raise_error_if_no_rate=False)
		stock_entry.db_update()
		for d in stock_entry.items:
			d.db_update()

	def update_rate_on_delivery_and_sales_return(self, sle, outgoing_rate):
		# Update item's incoming rate on transaction
		item_code = frappe.db.get_value(sle.voucher_type + " Item", sle.voucher_detail_no, "item_code")
		if item_code == sle.item_code:
			frappe.db.set_value(
				sle.voucher_type + " Item", sle.voucher_detail_no, "incoming_rate", outgoing_rate
			)
		else:
			# packed item
			frappe.db.set_value(
				"Packed Item",
				{"parent_detail_docname": sle.voucher_detail_no, "item_code": sle.item_code},
				"incoming_rate",
				outgoing_rate,
			)

	def update_rate_on_purchase_receipt(self, sle, outgoing_rate):
		if frappe.db.exists(sle.voucher_type + " Item", sle.voucher_detail_no):
			if sle.voucher_type in ["Purchase Receipt", "Purchase Invoice"] and frappe.get_cached_value(
				sle.voucher_type, sle.voucher_no, "is_internal_supplier"
			):
				frappe.db.set_value(
					f"{sle.voucher_type} Item", sle.voucher_detail_no, "valuation_rate", sle.outgoing_rate
				)
		else:
			frappe.db.set_value(
				"Purchase Receipt Item Supplied", sle.voucher_detail_no, "rate", outgoing_rate
			)

		# Recalculate subcontracted item's rate in case of subcontracted purchase receipt/invoice
		if frappe.get_cached_value(sle.voucher_type, sle.voucher_no, "is_subcontracted"):
			doc = frappe.get_doc(sle.voucher_type, sle.voucher_no)
			doc.update_valuation_rate(reset_outgoing_rate=False)
			for d in doc.items + doc.supplied_items:
				d.db_update()

	def update_rate_on_subcontracting_receipt(self, sle, outgoing_rate):
		if frappe.db.exists("Subcontracting Receipt Item", sle.voucher_detail_no):
			frappe.db.set_value("Subcontracting Receipt Item", sle.voucher_detail_no, "rate", outgoing_rate)
		else:
			frappe.db.set_value(
				"Subcontracting Receipt Supplied Item",
				sle.voucher_detail_no,
				{"rate": outgoing_rate, "amount": abs(sle.actual_qty) * outgoing_rate},
			)

		scr = frappe.get_doc("Subcontracting Receipt", sle.voucher_no, for_update=True)
		scr.calculate_items_qty_and_amount()
		scr.db_update()
		for d in scr.items:
			d.db_update()

	def update_rate_on_stock_reconciliation(self, sle):
		if not sle.serial_no and not sle.batch_no:
			sr = frappe.get_doc("Stock Reconciliation", sle.voucher_no, for_update=True)

			for item in sr.items:
				# Skip for Serial and Batch Items
				if item.name != sle.voucher_detail_no or item.serial_no or item.batch_no:
					continue

				previous_sle = get_previous_sle(
					{
						"item_code": item.item_code,
						"warehouse": item.warehouse,
						"posting_date": sr.posting_date,
						"posting_time": sr.posting_time,
						"sle": sle.name,
					}
				)

				item.current_qty = previous_sle.get("qty_after_transaction") or 0.0
				item.current_valuation_rate = previous_sle.get("valuation_rate") or 0.0
				item.current_amount = flt(item.current_qty) * flt(item.current_valuation_rate)

				item.amount = flt(item.qty) * flt(item.valuation_rate)
				item.quantity_difference = item.qty - item.current_qty
				item.amount_difference = item.amount - item.current_amount
			else:
				sr.difference_amount = sum([item.amount_difference for item in sr.items])
			sr.db_update()

			for item in sr.items:
				item.db_update()

	def get_serialized_values(self, sle):
		incoming_rate = flt(sle.incoming_rate)
		actual_qty = flt(sle.actual_qty)
		serial_nos = cstr(sle.serial_no).split("\n")

		if incoming_rate < 0:
			# wrong incoming rate
			incoming_rate = self.wh_data.valuation_rate

		stock_value_change = 0
		if actual_qty > 0:
			stock_value_change = actual_qty * incoming_rate
		else:
			# In case of delivery/stock issue, get average purchase rate
			# of serial nos of current entry
			if not sle.is_cancelled:
				outgoing_value = self.get_incoming_value_for_serial_nos(sle, serial_nos)
				stock_value_change = -1 * outgoing_value
			else:
				stock_value_change = actual_qty * sle.outgoing_rate

		new_stock_qty = self.wh_data.qty_after_transaction + actual_qty

		if new_stock_qty > 0:
			new_stock_value = (
				self.wh_data.qty_after_transaction * self.wh_data.valuation_rate
			) + stock_value_change
			if new_stock_value >= 0:
				# calculate new valuation rate only if stock value is positive
				# else it remains the same as that of previous entry
				self.wh_data.valuation_rate = new_stock_value / new_stock_qty

		if not self.wh_data.valuation_rate and sle.voucher_detail_no:
			allow_zero_rate = self.check_if_allow_zero_valuation_rate(
				sle.voucher_type, sle.voucher_detail_no
			)
			if not allow_zero_rate:
				self.wh_data.valuation_rate = self.get_fallback_rate(sle)

	def get_incoming_value_for_serial_nos(self, sle, serial_nos):
		# get rate from serial nos within same company
		all_serial_nos = frappe.get_all(
			"Serial No", fields=["purchase_rate", "name", "company"], filters={"name": ("in", serial_nos)}
		)

		incoming_values = sum(flt(d.purchase_rate) for d in all_serial_nos if d.company == sle.company)

		# Get rate for serial nos which has been transferred to other company
		invalid_serial_nos = [d.name for d in all_serial_nos if d.company != sle.company]
		for serial_no in invalid_serial_nos:
			incoming_rate = frappe.db.sql(
				"""
				select incoming_rate
				from `tabStock Ledger Entry`
				where
					company = %s
					and actual_qty > 0
					and is_cancelled = 0
					and (serial_no = %s
						or serial_no like %s
						or serial_no like %s
						or serial_no like %s
					)
				order by posting_date desc
				limit 1
			""",
				(sle.company, serial_no, serial_no + "\n%", "%\n" + serial_no, "%\n" + serial_no + "\n%"),
			)

			incoming_values += flt(incoming_rate[0][0]) if incoming_rate else 0

		return incoming_values

	def get_moving_average_values(self, sle):
		actual_qty = flt(sle.actual_qty)
		new_stock_qty = flt(self.wh_data.qty_after_transaction) + actual_qty
		if new_stock_qty >= 0:
			if actual_qty > 0:
				if flt(self.wh_data.qty_after_transaction) <= 0:
					self.wh_data.valuation_rate = sle.incoming_rate
				else:
					new_stock_value = (self.wh_data.qty_after_transaction * self.wh_data.valuation_rate) + (
						actual_qty * sle.incoming_rate
					)

					self.wh_data.valuation_rate = new_stock_value / new_stock_qty

			elif sle.outgoing_rate:
				if new_stock_qty:
					new_stock_value = (self.wh_data.qty_after_transaction * self.wh_data.valuation_rate) + (
						actual_qty * sle.outgoing_rate
					)

					self.wh_data.valuation_rate = new_stock_value / new_stock_qty
				else:
					self.wh_data.valuation_rate = sle.outgoing_rate
		else:
			if flt(self.wh_data.qty_after_transaction) >= 0 and sle.outgoing_rate:
				self.wh_data.valuation_rate = sle.outgoing_rate

			if not self.wh_data.valuation_rate and actual_qty > 0:
				self.wh_data.valuation_rate = sle.incoming_rate

			# Get valuation rate from previous SLE or Item master, if item does not have the
			# allow zero valuration rate flag set
			if not self.wh_data.valuation_rate and sle.voucher_detail_no:
				allow_zero_valuation_rate = self.check_if_allow_zero_valuation_rate(
					sle.voucher_type, sle.voucher_detail_no
				)
				if not allow_zero_valuation_rate:
					self.wh_data.valuation_rate = self.get_fallback_rate(sle)

	def update_queue_values(self, sle):
		incoming_rate = flt(sle.incoming_rate)
		actual_qty = flt(sle.actual_qty)
		outgoing_rate = flt(sle.outgoing_rate)

		self.wh_data.qty_after_transaction = round_off_if_near_zero(
			self.wh_data.qty_after_transaction + actual_qty
		)

		if self.valuation_method == "LIFO":
			stock_queue = LIFOValuation(self.wh_data.stock_queue)
		else:
			stock_queue = FIFOValuation(self.wh_data.stock_queue)

		_prev_qty, prev_stock_value = stock_queue.get_total_stock_and_value()

		if actual_qty > 0:
			stock_queue.add_stock(qty=actual_qty, rate=incoming_rate)
		else:

			def rate_generator() -> float:
				allow_zero_valuation_rate = self.check_if_allow_zero_valuation_rate(
					sle.voucher_type, sle.voucher_detail_no
				)
				if not allow_zero_valuation_rate:
					return self.get_fallback_rate(sle)
				else:
					return 0.0

			stock_queue.remove_stock(
				qty=abs(actual_qty), outgoing_rate=outgoing_rate, rate_generator=rate_generator
			)

		_qty, stock_value = stock_queue.get_total_stock_and_value()

		stock_value_difference = stock_value - prev_stock_value

		self.wh_data.stock_queue = stock_queue.state
		self.wh_data.stock_value = round_off_if_near_zero(
			self.wh_data.stock_value + stock_value_difference
		)

		if not self.wh_data.stock_queue:
			self.wh_data.stock_queue.append(
				[0, sle.incoming_rate or sle.outgoing_rate or self.wh_data.valuation_rate]
			)

		if self.wh_data.qty_after_transaction:
			self.wh_data.valuation_rate = self.wh_data.stock_value / self.wh_data.qty_after_transaction

	def update_batched_values(self, sle):
		incoming_rate = flt(sle.incoming_rate)
		actual_qty = flt(sle.actual_qty)

		self.wh_data.qty_after_transaction = round_off_if_near_zero(
			self.wh_data.qty_after_transaction + actual_qty
		)

		if actual_qty > 0:
			stock_value_difference = incoming_rate * actual_qty
		else:
			outgoing_rate = get_batch_incoming_rate(
				item_code=sle.item_code,
				warehouse=sle.warehouse,
				batch_no=sle.batch_no,
				posting_date=sle.posting_date,
				posting_time=sle.posting_time,
				creation=sle.creation,
			)
			if outgoing_rate is None:
				# This can *only* happen if qty available for the batch is zero.
				# in such case fall back various other rates.
				# future entries will correct the overall accounting as each
				# batch individually uses moving average rates.
				outgoing_rate = self.get_fallback_rate(sle)
			stock_value_difference = outgoing_rate * actual_qty

		self.wh_data.stock_value = round_off_if_near_zero(
			self.wh_data.stock_value + stock_value_difference
		)
		if self.wh_data.qty_after_transaction:
			self.wh_data.valuation_rate = self.wh_data.stock_value / self.wh_data.qty_after_transaction

	def check_if_allow_zero_valuation_rate(self, voucher_type, voucher_detail_no):
		ref_item_dt = ""

		if voucher_type == "Stock Entry":
			ref_item_dt = voucher_type + " Detail"
		elif voucher_type in ["Purchase Invoice", "Sales Invoice", "Delivery Note", "Purchase Receipt"]:
			ref_item_dt = voucher_type + " Item"

		if ref_item_dt:
			return frappe.db.get_value(ref_item_dt, voucher_detail_no, "allow_zero_valuation_rate")
		else:
			return 0

	def get_fallback_rate(self, sle) -> float:
		"""When exact incoming rate isn't available use any of other "average" rates as fallback.
		This should only get used for negative stock."""
		return get_valuation_rate(
			sle.item_code,
			sle.warehouse,
			sle.voucher_type,
			sle.voucher_no,
			self.allow_zero_rate,
			currency=erpnext.get_company_currency(sle.company),
			company=sle.company,
			batch_no=sle.batch_no,
		)

	def get_sle_before_datetime(self, args):
		"""get previous stock ledger entry before current time-bucket"""
		sle = get_stock_ledger_entries(args, "<", "desc", "limit 1", for_update=False)
		sle = sle[0] if sle else frappe._dict()
		return sle

	def get_sle_after_datetime(self, args):
		"""get Stock Ledger Entries after a particular datetime, for reposting"""
		return get_stock_ledger_entries(args, ">", "asc", for_update=True, check_serial_no=False)

	def raise_exceptions(self):
		msg_list = []
		for warehouse, exceptions in self.exceptions.items():
			deficiency = min(e["diff"] for e in exceptions)

			if (
				exceptions[0]["voucher_type"],
				exceptions[0]["voucher_no"],
			) in frappe.local.flags.currently_saving:

				msg = _("{0} units of {1} needed in {2} to complete this transaction.").format(
					abs(deficiency),
					frappe.get_desk_link("Item", exceptions[0]["item_code"]),
					frappe.get_desk_link("Warehouse", warehouse),
				)
			else:
				msg = _(
					"{0} units of {1} needed in {2} on {3} {4} for {5} to complete this transaction."
				).format(
					abs(deficiency),
					frappe.get_desk_link("Item", exceptions[0]["item_code"]),
					frappe.get_desk_link("Warehouse", warehouse),
					exceptions[0]["posting_date"],
					exceptions[0]["posting_time"],
					frappe.get_desk_link(exceptions[0]["voucher_type"], exceptions[0]["voucher_no"]),
				)

			if msg:
				msg_list.append(msg)

		if msg_list:
			message = "\n\n".join(msg_list)
			if self.verbose:
				frappe.throw(message, NegativeStockError, title=_("Insufficient Stock"))
			else:
				raise NegativeStockError(message)

	def update_bin_data(self, sle):
		bin_name = get_or_make_bin(sle.item_code, sle.warehouse)
		values_to_update = {
			"actual_qty": sle.qty_after_transaction,
			"stock_value": sle.stock_value,
		}

		if sle.valuation_rate is not None:
			values_to_update["valuation_rate"] = sle.valuation_rate

		frappe.db.set_value("Bin", bin_name, values_to_update)

	def update_bin(self):
		# update bin for each warehouse
		for warehouse, data in self.data.items():
			bin_name = get_or_make_bin(self.item_code, warehouse)

			updated_values = {"actual_qty": data.qty_after_transaction, "stock_value": data.stock_value}
			if data.valuation_rate is not None:
				updated_values["valuation_rate"] = data.valuation_rate
			frappe.db.set_value("Bin", bin_name, updated_values, update_modified=True)


def get_previous_sle_of_current_voucher(args, operator="<", exclude_current_voucher=False):
	"""get stock ledger entries filtered by specific posting datetime conditions"""

	args["time_format"] = "%H:%i:%s"
	if not args.get("posting_date"):
		args["posting_date"] = "1900-01-01"
	if not args.get("posting_time"):
		args["posting_time"] = "00:00"

	voucher_condition = ""
	if exclude_current_voucher:
		voucher_no = args.get("voucher_no")
		voucher_condition = f"and voucher_no != '{voucher_no}'"

	sle = frappe.db.sql(
		"""
		select *, timestamp(posting_date, posting_time) as "timestamp"
		from `tabStock Ledger Entry`
		where item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and is_cancelled = 0
			{voucher_condition}
			and (
				posting_date < %(posting_date)s or
				(
					posting_date = %(posting_date)s and
					time_format(posting_time, %(time_format)s) {operator} time_format(%(posting_time)s, %(time_format)s)
				)
			)
		order by timestamp(posting_date, posting_time) desc, creation desc
		limit 1
		for update""".format(
			operator=operator, voucher_condition=voucher_condition
		),
		args,
		as_dict=1,
	)

	return sle[0] if sle else frappe._dict()


def get_previous_sle(args, for_update=False, extra_cond=None):
	"""
	get the last sle on or before the current time-bucket,
	to get actual qty before transaction, this function
	is called from various transaction like stock entry, reco etc

	args = {
	        "item_code": "ABC",
	        "warehouse": "XYZ",
	        "posting_date": "2012-12-12",
	        "posting_time": "12:00",
	        "sle": "name of reference Stock Ledger Entry"
	}
	"""
	args["name"] = args.get("sle", None) or ""
	sle = get_stock_ledger_entries(
		args, "<=", "desc", "limit 1", for_update=for_update, extra_cond=extra_cond
	)
	return sle and sle[0] or {}


def get_stock_ledger_entries(
	previous_sle,
	operator=None,
	order="desc",
	limit=None,
	for_update=False,
	debug=False,
	check_serial_no=True,
	extra_cond=None,
):
	"""get stock ledger entries filtered by specific posting datetime conditions"""
	conditions = " and timestamp(posting_date, posting_time) {0} timestamp(%(posting_date)s, %(posting_time)s)".format(
		operator
	)
	if previous_sle.get("warehouse"):
		conditions += " and warehouse = %(warehouse)s"
	elif previous_sle.get("warehouse_condition"):
		conditions += " and " + previous_sle.get("warehouse_condition")

	if check_serial_no and previous_sle.get("serial_no"):
		# conditions += " and serial_no like {}".format(frappe.db.escape('%{0}%'.format(previous_sle.get("serial_no"))))
		serial_no = previous_sle.get("serial_no")
		conditions += (
			""" and
			(
				serial_no = {0}
				or serial_no like {1}
				or serial_no like {2}
				or serial_no like {3}
			)
		"""
		).format(
			frappe.db.escape(serial_no),
			frappe.db.escape("{}\n%".format(serial_no)),
			frappe.db.escape("%\n{}".format(serial_no)),
			frappe.db.escape("%\n{}\n%".format(serial_no)),
		)

	if not previous_sle.get("posting_date"):
		previous_sle["posting_date"] = "1900-01-01"
	if not previous_sle.get("posting_time"):
		previous_sle["posting_time"] = "00:00"

	if operator in (">", "<=") and previous_sle.get("name"):
		conditions += " and name!=%(name)s"

	if operator in (">", "<=") and previous_sle.get("voucher_no"):
		conditions += " and voucher_no!=%(voucher_no)s"

	if extra_cond:
		conditions += f"{extra_cond}"

	return frappe.db.sql(
		"""
		select *, timestamp(posting_date, posting_time) as "timestamp"
		from `tabStock Ledger Entry`
		where item_code = %%(item_code)s
		and is_cancelled = 0
		%(conditions)s
		order by timestamp(posting_date, posting_time) %(order)s, creation %(order)s
		%(limit)s %(for_update)s"""
		% {
			"conditions": conditions,
			"limit": limit or "",
			"for_update": for_update and "for update" or "",
			"order": order,
		},
		previous_sle,
		as_dict=1,
		debug=debug,
	)


def get_sle_by_voucher_detail_no(voucher_detail_no, excluded_sle=None):
	return frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_detail_no": voucher_detail_no, "name": ["!=", excluded_sle], "is_cancelled": 0},
		[
			"item_code",
			"warehouse",
			"actual_qty",
			"qty_after_transaction",
			"posting_date",
			"posting_time",
			"voucher_detail_no",
			"timestamp(posting_date, posting_time) as timestamp",
		],
		as_dict=1,
	)


def get_batch_incoming_rate(
	item_code, warehouse, batch_no, posting_date, posting_time, creation=None
):

	sle = frappe.qb.DocType("Stock Ledger Entry")

	timestamp_condition = CombineDatetime(sle.posting_date, sle.posting_time) < CombineDatetime(
		posting_date, posting_time
	)
	if creation:
		timestamp_condition |= (
			CombineDatetime(sle.posting_date, sle.posting_time)
			== CombineDatetime(posting_date, posting_time)
		) & (sle.creation < creation)

	batch_details = (
		frappe.qb.from_(sle)
		.select(Sum(sle.stock_value_difference).as_("batch_value"), Sum(sle.actual_qty).as_("batch_qty"))
		.where(
			(sle.item_code == item_code)
			& (sle.warehouse == warehouse)
			& (sle.batch_no == batch_no)
			& (sle.is_cancelled == 0)
		)
		.where(timestamp_condition)
	).run(as_dict=True)

	if batch_details and batch_details[0].batch_qty:
		return batch_details[0].batch_value / batch_details[0].batch_qty


def get_valuation_rate(
	item_code,
	warehouse,
	voucher_type,
	voucher_no,
	allow_zero_rate=False,
	currency=None,
	company=None,
	raise_error_if_no_rate=True,
	batch_no=None,
):

	if not company:
		company = frappe.get_cached_value("Warehouse", warehouse, "company")

	last_valuation_rate = None

	# Get moving average rate of a specific batch number
	if warehouse and batch_no and frappe.db.get_value("Batch", batch_no, "use_batchwise_valuation"):
		last_valuation_rate = frappe.db.sql(
			"""
			select sum(stock_value_difference) / sum(actual_qty)
			from `tabStock Ledger Entry`
			where
				item_code = %s
				AND warehouse = %s
				AND batch_no = %s
				AND is_cancelled = 0
				AND NOT (voucher_no = %s AND voucher_type = %s)
			""",
			(item_code, warehouse, batch_no, voucher_no, voucher_type),
		)

	# Get valuation rate from last sle for the same item and warehouse
	if not last_valuation_rate or last_valuation_rate[0][0] is None:
		last_valuation_rate = frappe.db.sql(
			"""select valuation_rate
			from `tabStock Ledger Entry` force index (item_warehouse)
			where
				item_code = %s
				AND warehouse = %s
				AND valuation_rate >= 0
				AND is_cancelled = 0
				AND NOT (voucher_no = %s AND voucher_type = %s)
			order by posting_date desc, posting_time desc, name desc limit 1""",
			(item_code, warehouse, voucher_no, voucher_type),
		)

	if last_valuation_rate:
		return flt(last_valuation_rate[0][0])

	# If negative stock allowed, and item delivered without any incoming entry,
	# system does not found any SLE, then take valuation rate from Item
	valuation_rate = frappe.db.get_value("Item", item_code, "valuation_rate")

	if not valuation_rate:
		# try Item Standard rate
		valuation_rate = frappe.db.get_value("Item", item_code, "standard_rate")

		if not valuation_rate:
			# try in price list
			valuation_rate = frappe.db.get_value(
				"Item Price", dict(item_code=item_code, buying=1, currency=currency), "price_list_rate"
			)

	if (
		not allow_zero_rate
		and not valuation_rate
		and raise_error_if_no_rate
		and cint(erpnext.is_perpetual_inventory_enabled(company))
	):
		form_link = get_link_to_form("Item", item_code)

		message = _(
			"Valuation Rate for the Item {0}, is required to do accounting entries for {1} {2}."
		).format(form_link, voucher_type, voucher_no)
		message += "<br><br>" + _("Here are the options to proceed:")
		solutions = (
			"<li>"
			+ _(
				"If the item is transacting as a Zero Valuation Rate item in this entry, please enable 'Allow Zero Valuation Rate' in the {0} Item table."
			).format(voucher_type)
			+ "</li>"
		)
		solutions += (
			"<li>"
			+ _("If not, you can Cancel / Submit this entry")
			+ " {0} ".format(frappe.bold("after"))
			+ _("performing either one below:")
			+ "</li>"
		)
		sub_solutions = "<ul><li>" + _("Create an incoming stock transaction for the Item.") + "</li>"
		sub_solutions += "<li>" + _("Mention Valuation Rate in the Item master.") + "</li></ul>"
		msg = message + solutions + sub_solutions + "</li>"

		frappe.throw(msg=msg, title=_("Valuation Rate Missing"))

	return valuation_rate


def update_qty_in_future_sle(args, allow_negative_stock=False):
	"""Recalculate Qty after Transaction in future SLEs based on current SLE."""
	datetime_limit_condition = ""
	qty_shift = args.actual_qty

	args["time_format"] = "%H:%i:%s"

	# find difference/shift in qty caused by stock reconciliation
	if args.voucher_type == "Stock Reconciliation":
		qty_shift = get_stock_reco_qty_shift(args)

	# find the next nearest stock reco so that we only recalculate SLEs till that point
	next_stock_reco_detail = get_next_stock_reco(args)
	if next_stock_reco_detail:
		detail = next_stock_reco_detail[0]

		# add condition to update SLEs before this date & time
		datetime_limit_condition = get_datetime_limit_condition(detail)

	frappe.db.sql(
		f"""
		update `tabStock Ledger Entry`
		set qty_after_transaction = qty_after_transaction + {qty_shift}
		where
			item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and voucher_no != %(voucher_no)s
			and is_cancelled = 0
			and (
				posting_date > %(posting_date)s or
				(
					posting_date = %(posting_date)s and
					time_format(posting_time, %(time_format)s) > time_format(%(posting_time)s, %(time_format)s)
				)
			)
		{datetime_limit_condition}
		""",
		args,
	)

	validate_negative_qty_in_future_sle(args, allow_negative_stock)


def get_stock_reco_qty_shift(args):
	stock_reco_qty_shift = 0
	if args.get("is_cancelled"):
		if args.get("previous_qty_after_transaction"):
			# get qty (balance) that was set at submission
			last_balance = args.get("previous_qty_after_transaction")
			stock_reco_qty_shift = flt(args.qty_after_transaction) - flt(last_balance)
		else:
			stock_reco_qty_shift = flt(args.actual_qty)
	else:
		# reco is being submitted
		last_balance = get_previous_sle_of_current_voucher(args, "<=", exclude_current_voucher=True).get(
			"qty_after_transaction"
		)

		if last_balance is not None:
			stock_reco_qty_shift = flt(args.qty_after_transaction) - flt(last_balance)
		else:
			stock_reco_qty_shift = args.qty_after_transaction

	return stock_reco_qty_shift


def get_next_stock_reco(kwargs):
	"""Returns next nearest stock reconciliaton's details."""

	sle = frappe.qb.DocType("Stock Ledger Entry")

	query = (
		frappe.qb.from_(sle)
		.select(
			sle.name,
			sle.posting_date,
			sle.posting_time,
			sle.creation,
			sle.voucher_no,
			sle.item_code,
			sle.batch_no,
			sle.actual_qty,
		)
		.where(
			(sle.item_code == kwargs.get("item_code"))
			& (sle.warehouse == kwargs.get("warehouse"))
			& (sle.voucher_type == "Stock Reconciliation")
			& (sle.voucher_no != kwargs.get("voucher_no"))
			& (sle.is_cancelled == 0)
			& (
				(
					CombineDatetime(sle.posting_date, sle.posting_time)
					> CombineDatetime(kwargs.get("posting_date"), kwargs.get("posting_time"))
				)
				| (
					(
						CombineDatetime(sle.posting_date, sle.posting_time)
						== CombineDatetime(kwargs.get("posting_date"), kwargs.get("posting_time"))
					)
					& (sle.creation > kwargs.get("creation"))
				)
			)
		)
		.orderby(CombineDatetime(sle.posting_date, sle.posting_time))
		.orderby(sle.creation)
		.limit(1)
	)

	if kwargs.get("batch_no"):
		query = query.where(sle.batch_no == kwargs.get("batch_no"))

	return query.run(as_dict=True)


def get_datetime_limit_condition(detail):
	return f"""
		and
		(timestamp(posting_date, posting_time) < timestamp('{detail.posting_date}', '{detail.posting_time}')
			or (
				timestamp(posting_date, posting_time) = timestamp('{detail.posting_date}', '{detail.posting_time}')
				and creation < '{detail.creation}'
			)
		)"""


def validate_negative_qty_in_future_sle(args, allow_negative_stock=False):
	if allow_negative_stock or is_negative_stock_allowed(item_code=args.item_code):
		return
	if not (args.actual_qty < 0 or args.voucher_type == "Stock Reconciliation"):
		return

	neg_sle = get_future_sle_with_negative_qty(args)

	if is_negative_with_precision(neg_sle):
		message = _(
			"{0} units of {1} needed in {2} on {3} {4} for {5} to complete this transaction."
		).format(
			abs(neg_sle[0]["qty_after_transaction"]),
			frappe.get_desk_link("Item", args.item_code),
			frappe.get_desk_link("Warehouse", args.warehouse),
			neg_sle[0]["posting_date"],
			neg_sle[0]["posting_time"],
			frappe.get_desk_link(neg_sle[0]["voucher_type"], neg_sle[0]["voucher_no"]),
		)

		frappe.throw(message, NegativeStockError, title=_("Insufficient Stock"))

	if not args.batch_no:
		return

	neg_batch_sle = get_future_sle_with_negative_batch_qty(args)
	if is_negative_with_precision(neg_batch_sle, is_batch=True):
		message = _(
			"{0} units of {1} needed in {2} on {3} {4} for {5} to complete this transaction."
		).format(
			abs(neg_batch_sle[0]["cumulative_total"]),
			frappe.get_desk_link("Batch", args.batch_no),
			frappe.get_desk_link("Warehouse", args.warehouse),
			neg_batch_sle[0]["posting_date"],
			neg_batch_sle[0]["posting_time"],
			frappe.get_desk_link(neg_batch_sle[0]["voucher_type"], neg_batch_sle[0]["voucher_no"]),
		)
		frappe.throw(message, NegativeStockError, title=_("Insufficient Stock for Batch"))


def is_negative_with_precision(neg_sle, is_batch=False):
	"""
	Returns whether system precision rounded qty is insufficient.
	E.g: -0.0003 in precision 3 (0.000) is sufficient for the user.
	"""

	if not neg_sle:
		return False

	field = "cumulative_total" if is_batch else "qty_after_transaction"
	precision = cint(frappe.db.get_default("float_precision")) or 2
	qty_deficit = flt(neg_sle[0][field], precision)

	return qty_deficit < 0 and abs(qty_deficit) > 0.0001


def get_future_sle_with_negative_qty(sle):
	SLE = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(SLE)
		.select(
			SLE.qty_after_transaction, SLE.posting_date, SLE.posting_time, SLE.voucher_type, SLE.voucher_no
		)
		.where(
			(SLE.item_code == sle.item_code)
			& (SLE.warehouse == sle.warehouse)
			& (SLE.voucher_no != sle.voucher_no)
			& (
				CombineDatetime(SLE.posting_date, SLE.posting_time)
				>= CombineDatetime(sle.posting_date, sle.posting_time)
			)
			& (SLE.is_cancelled == 0)
			& (SLE.qty_after_transaction < 0)
		)
		.orderby(CombineDatetime(SLE.posting_date, SLE.posting_time))
		.limit(1)
	)

	if sle.voucher_type == "Stock Reconciliation" and sle.batch_no:
		query = query.where(SLE.batch_no == sle.batch_no)

	return query.run(as_dict=True)


def get_future_sle_with_negative_batch_qty(args):
	return frappe.db.sql(
		"""
		with batch_ledger as (
			select
				posting_date, posting_time, voucher_type, voucher_no,
				sum(actual_qty) over (order by posting_date, posting_time, creation) as cumulative_total
			from `tabStock Ledger Entry`
			where
				item_code = %(item_code)s
				and warehouse = %(warehouse)s
				and batch_no=%(batch_no)s
				and is_cancelled = 0
			order by posting_date, posting_time, creation
		)
		select * from batch_ledger
		where
			cumulative_total < 0.0
			and timestamp(posting_date, posting_time) >= timestamp(%(posting_date)s, %(posting_time)s)
		limit 1
	""",
		args,
		as_dict=1,
	)


def is_negative_stock_allowed(*, item_code: Optional[str] = None) -> bool:
	if cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock", cache=True)):
		return True
	if item_code and cint(frappe.db.get_value("Item", item_code, "allow_negative_stock", cache=True)):
		return True
	return False


def get_incoming_rate_for_inter_company_transfer(sle) -> float:
	"""
	For inter company transfer, incoming rate is the average of the outgoing rate
	"""
	rate = 0.0

	field = "delivery_note_item" if sle.voucher_type == "Purchase Receipt" else "sales_invoice_item"

	doctype = "Delivery Note Item" if sle.voucher_type == "Purchase Receipt" else "Sales Invoice Item"

	reference_name = frappe.get_cached_value(sle.voucher_type + " Item", sle.voucher_detail_no, field)

	if reference_name:
		rate = frappe.get_cached_value(
			doctype,
			reference_name,
			"incoming_rate",
		)

	return rate


def is_internal_transfer(sle):
	data = frappe.get_cached_value(
		sle.voucher_type,
		sle.voucher_no,
		["is_internal_supplier", "represents_company", "company"],
		as_dict=True,
	)

	if data.is_internal_supplier and data.represents_company == data.company:
		return True


def get_stock_value_difference(item_code, warehouse, posting_date, posting_time, voucher_no=None):
	table = frappe.qb.DocType("Stock Ledger Entry")

	query = (
		frappe.qb.from_(table)
		.select(Sum(table.stock_value_difference).as_("value"))
		.where(
			(table.is_cancelled == 0)
			& (table.item_code == item_code)
			& (table.warehouse == warehouse)
			& (
				(table.posting_date < posting_date)
				| ((table.posting_date == posting_date) & (table.posting_time <= posting_time))
			)
		)
	)

	if voucher_no:
		query = query.where(table.voucher_no != voucher_no)

	difference_amount = query.run()
	return flt(difference_amount[0][0]) if difference_amount else 0
