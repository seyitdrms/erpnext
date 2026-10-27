import frappe


def execute():
	if frappe.db.get_single_value("CRM Settings", "enable_frappe_crm_data_synchronization"):
		frappe.get_single("CRM Settings").custom_fields_for_frappe_crm_data_sync()
