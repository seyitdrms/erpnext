import json

import click
import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, get_url

from erpnext.crm.customer_organization import (
	allow_crm_identity_change,
	get_customer_for_organization_write,
	normalize_crm_organization,
	normalize_crm_site,
	require_customer_organization_fields,
)


@frappe.whitelist()
def create_prospect_against_crm_deal():
	validate_frappe_crm_sync()

	doc = frappe.form_dict
	prospect = frappe.new_doc("Prospect")
	prospect.company_name = doc.organization or doc.lead_name
	prospect.no_of_employees = doc.no_of_employees
	prospect.prospect_owner = doc.deal_owner
	prospect.company = doc.erpnext_company
	prospect.crm_deal = doc.crm_deal
	prospect.territory = doc.territory
	prospect.industry = doc.industry
	prospect.website = doc.website
	prospect.annual_revenue = doc.annual_revenue

	try:
		prospect_name = frappe.db.get_value("Prospect", {"company_name": prospect.company_name})
		if not prospect_name:
			prospect.insert()
			prospect_name = prospect.name
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(),
			f"Error while creating prospect against CRM Deal: {frappe.form_dict.get('crm_deal_id')}",
		)
		pass

	if doc.contacts and len(doc.contacts):
		create_contacts(frappe.parse_json(doc.contacts), prospect.company_name, "Prospect", prospect_name)

	create_address("Prospect", prospect_name, doc.address)
	frappe.response["message"] = prospect_name


def create_contacts(contacts, organization=None, link_doctype=None, link_docname=None):
	for c in contacts:
		c = frappe._dict(c)
		existing_contact = contact_exists(c.email, c.mobile_no)
		if existing_contact:
			contact = frappe.get_doc("Contact", existing_contact)
		else:
			contact = frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": c.get("full_name"),
					"gender": c.get("gender"),
					"company_name": organization,
				}
			)

			if c.get("email"):
				contact.append("email_ids", {"email_id": c.get("email"), "is_primary": 1})

			if c.get("mobile_no"):
				contact.append("phone_nos", {"phone": c.get("mobile_no"), "is_primary_mobile_no": 1})

		link_doc(contact, link_doctype, link_docname)

		contact.save(ignore_permissions=True)


def create_address(doctype, docname, address):
	if not address:
		return
	address = frappe.parse_json(address)
	frappe.db.savepoint("crm_create_address")
	try:
		_address = frappe.db.exists("Address", address.get("name"))
		if not _address:
			new_address_doc = frappe.new_doc("Address")
			for field in [
				"address_title",
				"address_type",
				"address_line1",
				"address_line2",
				"city",
				"county",
				"state",
				"pincode",
				"country",
			]:
				if address.get(field):
					new_address_doc.set(field, address.get(field))

			new_address_doc.append("links", {"link_doctype": doctype, "link_name": docname})
			new_address_doc.insert(ignore_mandatory=True)
			return new_address_doc.name
		else:
			address = frappe.get_doc("Address", _address)
			link_doc(address, doctype, docname)
			address.save(ignore_permissions=True)
			return address.name
	except Exception:
		frappe.db.rollback(save_point="crm_create_address")
		frappe.log_error(frappe.get_traceback(), f"Error while creating address for {docname}")


def link_doc(doc, link_doctype, link_docname):
	already_linked = any(
		[(link.link_doctype == link_doctype and link.link_name == link_docname) for link in doc.links]
	)
	if not already_linked:
		doc.append(
			"links", {"link_doctype": link_doctype, "link_name": link_docname, "link_title": link_docname}
		)


def contact_exists(email, mobile_no):
	email_exist = frappe.db.exists("Contact Email", {"email_id": email})
	mobile_exist = frappe.db.exists("Contact Phone", {"phone": mobile_no})

	doctype = "Contact Email" if email_exist else "Contact Phone"
	name = email_exist or mobile_exist

	if name:
		return frappe.db.get_value(doctype, name, "parent")

	return False


CUSTOMER_ALLOWED_FIELDS = {
	"customer_name",
	"customer_group",
	"customer_type",
	"territory",
	"default_currency",
	"industry",
	"website",
	"crm_deal",
}

CUSTOMER_SYNC_FIELDS = [
	"name",
	"customer_name",
	"customer_type",
	"tax_id",
	"website",
	"industry",
	"territory",
	"default_currency",
	"customer_primary_address",
	"modified",
	"disabled",
	"crm_organization",
	"crm_site",
	"crm_deal",
]

CONTACT_SYNC_FIELDS = [
	"name",
	"first_name",
	"middle_name",
	"last_name",
	"full_name",
	"email_id",
	"mobile_no",
	"phone",
	"gender",
	"company_name",
	"designation",
	"modified",
	"crm_record",
	"crm_site",
]
ADDRESS_SYNC_FIELDS = [
	"name",
	"address_title",
	"address_type",
	"address_line1",
	"address_line2",
	"city",
	"county",
	"state",
	"pincode",
	"country",
	"email_id",
	"phone",
	"is_primary_address",
	"is_shipping_address",
	"disabled",
	"modified",
	"crm_record",
	"crm_site",
]


def _readable_sync_fields(doctype, fields, required=()):
	permitted = set(frappe.get_meta(doctype).get_permitted_fieldnames(user=frappe.session.user))
	permitted.update(("name", "modified"))
	if any(field not in permitted for field in required):
		frappe.throw(
			_("Your permissions do not allow reading {0} synchronization identities.").format(doctype),
			frappe.PermissionError,
		)
	return [field for field in fields if field in permitted]


def _customer_sync_fields():
	return _readable_sync_fields(
		"Customer",
		CUSTOMER_SYNC_FIELDS,
		required=("name", "customer_name", "customer_type", "modified", "crm_organization", "crm_site"),
	)


def _customer_sync_data(customer):
	fields = _customer_sync_fields()
	customer.apply_fieldlevel_read_permissions()
	return {field: customer.get(field) for field in fields}


def _check_customer_modified(customer, modified):
	if modified and get_datetime(customer.modified) != get_datetime(modified):
		frappe.throw(
			_("The Customer has changed. Reload it before updating its CRM Organization."),
			frappe.TimestampMismatchError,
		)


@frappe.whitelist(methods=["GET"])
def get_customers_for_crm(start: int = 0, page_length: int = 100, modified_after: str | None = None):
	"""Read a permitted Company page with ``has_more`` and ``next_start`` metadata."""
	validate_frappe_crm_sync()
	require_customer_organization_fields()
	if start < 0 or not 1 <= page_length <= 100:
		frappe.throw(_("Choose a non-negative start and a page length between 1 and 100."))
	filters = {"customer_type": "Company"}
	if modified_after:
		filters["modified"] = [">", get_datetime(modified_after)]
	customers = frappe.get_list(
		"Customer",
		fields=_customer_sync_fields(),
		filters=filters,
		order_by="name asc",
		limit_start=start,
		limit_page_length=page_length + 1,
	)
	has_more = len(customers) > page_length
	return {
		"customers": customers[:page_length],
		"has_more": has_more,
		"next_start": start + page_length if has_more else None,
	}


def _linked_records_for_crm(doctype, customer):
	fields = _readable_sync_fields(
		doctype, CONTACT_SYNC_FIELDS if doctype == "Contact" else ADDRESS_SYNC_FIELDS
	)
	tables = _readable_sync_fields(doctype, ["links", "email_ids", "phone_nos"])
	records = []
	for start in range(0, 10000, 100):
		parents = frappe.get_list(
			doctype,
			fields=["name"],
			filters=[
				["Dynamic Link", "link_doctype", "=", "Customer"],
				["Dynamic Link", "link_name", "=", customer],
			],
			distinct=True,
			order_by="name asc",
			limit_start=start,
			limit_page_length=100,
		)
		for parent in parents:
			doc = frappe.get_doc(doctype, parent.name)
			doc.check_permission("read")
			doc.apply_fieldlevel_read_permissions()
			data = {field: doc.get(field) for field in fields}
			# Other linked parties need their own permissions and are not part of this export.
			if "links" in tables:
				data["links"] = [
					{"link_doctype": row.link_doctype, "link_name": row.link_name}
					for row in (doc.get("links") or [])
					if row.link_doctype == "Customer" and row.link_name == customer
				]
			if doctype == "Contact" and "email_ids" in tables:
				data["email_ids"] = [
					{"email_id": row.email_id, "is_primary": row.is_primary}
					for row in (doc.get("email_ids") or [])
				]
			if doctype == "Contact" and "phone_nos" in tables:
				data["phone_nos"] = [
					{
						"phone": row.phone,
						"is_primary_phone": row.is_primary_phone,
						"is_primary_mobile_no": row.is_primary_mobile_no,
					}
					for row in (doc.get("phone_nos") or [])
				]
			records.append(data)
		if len(parents) < 100:
			return records
	frappe.throw(
		_("Too many related records. Synchronize this Customer's contacts and addresses separately.")
	)


@frappe.whitelist(methods=["GET"])
def get_customer_for_crm(customer: str, include_related: bool = True):
	"""Read a Company and optional permitted Contact/Address data without changing identities."""
	validate_frappe_crm_sync()
	require_customer_organization_fields()
	doc = frappe.get_doc("Customer", customer)
	doc.check_permission("read")
	if doc.customer_type != "Company":
		frappe.throw(_("Only Company customers can be synchronized to CRM Organizations."))
	return {
		"customer": _customer_sync_data(doc),
		"contacts": _linked_records_for_crm("Contact", customer) if include_related else [],
		"addresses": _linked_records_for_crm("Address", customer) if include_related else [],
	}


@frappe.whitelist(methods=["GET"])
def resolve_customer_for_crm(
	organization: str,
	crm_site: str,
	customer: str | None = None,
	include_related: bool = False,
):
	"""Resolve an exact site/Organization identity without writing a link.

	Return Customer details, or None when no Customer ID was expected and no link exists.
	``renamed_from`` is supplied only when the expected ID is absent and the exact link resolves.
	"""
	validate_frappe_crm_sync()
	require_customer_organization_fields()
	_customer_sync_fields()
	organization = normalize_crm_organization(organization)
	site = normalize_crm_site(crm_site)
	# Check every exact identity match before returning its native ID or treating it as absent.
	matches = frappe.get_all(
		"Customer",
		filters={"crm_site": site, "crm_organization": organization},
		fields=["name"],
		limit_page_length=2,
	)
	for match in matches:
		frappe.get_doc("Customer", match.name).check_permission("read")
	if len(matches) > 1:
		frappe.throw(_("More than one Customer identifies this CRM Organization. Review the existing links."))
	matched_customer = matches[0].name if matches else None
	renamed_from = None
	if customer is not None:
		customer = normalize_crm_organization(customer)
		if frappe.db.exists("Customer", customer):
			doc = frappe.get_doc("Customer", customer)
			doc.check_permission("read")
			current = (doc.get("crm_site"), doc.get("crm_organization"))
			if (any(current) and current != (site, organization)) or (
				matched_customer and matched_customer != customer
			):
				frappe.throw(
					_(
						"The Customer identity conflicts with the CRM Organization link. Review the existing links."
					)
				)
			matched_customer = customer
		elif matched_customer:
			renamed_from = customer
		else:
			frappe.throw(_("The linked Customer no longer exists."), frappe.DoesNotExistError)
	if not matched_customer:
		return None
	result = get_customer_for_crm(matched_customer, include_related=include_related)
	if renamed_from:
		result["renamed_from"] = renamed_from
	return result


@frappe.whitelist(methods=["POST"])
def link_customer_to_organization(
	customer: str,
	organization: str,
	crm_site: str | None = None,
	modified: str | None = None,
	previous_organization: str | None = None,
):
	"""Persist an exact Company/Organization pair under native Customer write permissions.

	The same pair is idempotent. ``previous_organization`` permits an exact Organization rename;
	other existing identities must be explicitly unlinked first.
	"""
	validate_frappe_crm_sync()
	organization = normalize_crm_organization(organization)
	site = normalize_crm_site(crm_site)
	doc = get_customer_for_organization_write(customer)
	current = (doc.get("crm_site"), doc.get("crm_organization"))
	if current == (site, organization):
		return _customer_sync_data(doc)
	if previous_organization is not None:
		previous = normalize_crm_organization(previous_organization)
		if current != (site, previous):
			frappe.throw(
				_("The Customer's current CRM Organization does not match the organization being renamed.")
			)
	elif any(current):
		frappe.throw(
			_(
				"This Customer is linked to another CRM Organization or site. Unlink it before changing its identity."
			)
		)
	_check_customer_modified(doc, modified)
	doc.crm_organization = organization
	doc.crm_site = site
	with allow_crm_identity_change(doc):
		doc.save()
	return _customer_sync_data(doc)


@frappe.whitelist(methods=["POST"])
def unlink_customer_from_organization(
	customer: str, organization: str, crm_site: str | None = None, modified: str | None = None
):
	"""Clear only the exact requested pair; an already-empty identity is an idempotent success."""
	validate_frappe_crm_sync()
	organization = normalize_crm_organization(organization)
	site = normalize_crm_site(crm_site)
	doc = get_customer_for_organization_write(customer)
	current = (doc.get("crm_site"), doc.get("crm_organization"))
	if not any(current):
		return _customer_sync_data(doc)
	if current != (site, organization):
		frappe.throw(_("The Customer's CRM Organization or site does not match the requested link."))
	_check_customer_modified(doc, modified)
	doc.crm_organization = None
	doc.crm_site = None
	with allow_crm_identity_change(doc):
		doc.save()
	return _customer_sync_data(doc)


@frappe.whitelist(methods=["POST"])
def create_customer(customer_data: dict | None = None):
	"""Create a legacy deal Customer or synchronize an explicitly identified CRM Organization.

	Organization requests return a native Company Customer ID and preserve existing ERPNext values.
	Requests without Organization identity fields retain the legacy deal integration behavior.
	"""
	validate_frappe_crm_sync()

	if not customer_data:
		customer_data = frappe.form_dict
	if "crm_organization" in customer_data or "crm_site" in customer_data or customer_data.get("customer"):
		return _create_customer_for_organization(customer_data)

	try:
		customer_name = frappe.db.exists("Customer", {"customer_name": customer_data.get("customer_name")})
		if not customer_name:
			customer = frappe.new_doc("Customer")
			for field in CUSTOMER_ALLOWED_FIELDS:
				if customer_data.get(field) is not None:
					customer.set(field, customer_data.get(field))

			# If CRM is installed on the site, User Permission cannot be ignored while saving Customer Records.
			customer.insert(ignore_permissions=not is_crm_installed())
			customer_name = customer.name
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Error while creating customer against Frappe CRM Deal")
		return

	# Link contacts/address under a savepoint so a failure here does NOT discard the Customer just
	# created (a full rollback would; MariaDB kept it pre-migration). Linking is best-effort.
	frappe.db.savepoint("crm_customer_links")
	try:
		contacts = frappe.parse_json(customer_data.get("contacts"))
		create_contacts(contacts, customer_name, "Customer", customer_name)
		create_address("Customer", customer_name, customer_data.get("address"))
	except Exception:
		frappe.db.rollback(save_point="crm_customer_links")
		frappe.log_error(frappe.get_traceback(), "Error while linking contacts/address to new Customer")
		# keep the Customer, but preserve the pre-existing contract of returning None on a linking failure
		# so CRM callers still see the failure signal
		return

	return customer_name


def _organization_customer_candidate(customer_data, site, organization):
	explicit = customer_data.get("customer")
	if explicit:
		return normalize_crm_organization(explicit)
	linked = frappe.db.get_value(
		"Customer", {"crm_site": site, "crm_organization": organization}, "name", for_update=True
	)
	if linked:
		return linked
	if tax_id := cstr(customer_data.get("tax_id")).strip():
		candidates = frappe.get_all(
			"Customer",
			filters={"customer_type": "Company", "tax_id": tax_id},
			pluck="name",
			limit_page_length=2,
		)
		if len(candidates) > 1:
			frappe.throw(
				_("Several Company customers have this Tax ID. Select the existing Customer explicitly.")
			)
		if candidates:
			return candidates[0]
	if frappe.db.exists("Customer", {"customer_name": customer_data.get("customer_name")}):
		frappe.throw(
			_("A Customer with this name already exists. Select its Customer ID instead of matching by name.")
		)


def _create_customer_for_organization(customer_data):
	require_customer_organization_fields()
	frappe.has_permission("Customer", "read", throw=True)
	_customer_sync_fields()
	if customer_data.get("customer_type") not in (None, "", "Company"):
		frappe.throw(_("Only Company customers can be linked to a CRM Organization."))
	organization = normalize_crm_organization(customer_data.get("crm_organization"))
	site = normalize_crm_site(customer_data.get("crm_site"))
	if not customer_data.get("customer_name"):
		frappe.throw(_("Customer Name is required."))

	candidate = _organization_customer_candidate(customer_data, site, organization)
	if candidate:
		customer = get_customer_for_organization_write(candidate)
		current = (customer.get("crm_site"), customer.get("crm_organization"))
		if any(current) and current != (site, organization):
			frappe.throw(
				_(
					"This Customer is linked to another CRM Organization or site. Select its existing organization."
				)
			)
		if current != (site, organization):
			_check_customer_modified(customer, customer_data.get("modified"))
			customer.crm_organization = organization
			customer.crm_site = site
			with allow_crm_identity_change(customer):
				customer.save()
	else:
		customer = frappe.new_doc("Customer")
		for field in (CUSTOMER_ALLOWED_FIELDS | {"tax_id"}) - {"crm_deal"}:
			if customer_data.get(field) is not None:
				customer.set(field, customer_data.get(field))
		customer.customer_type = "Company"
		customer.crm_organization = organization
		customer.crm_site = site
		with allow_crm_identity_change(customer):
			customer.insert()

	_sync_organization_customer_relations(customer_data, customer, site)
	return customer.name


def _crm_relation_record(doctype, data, site, same_site, customer):
	record = data.get("crm_record") or data.get("contact") or data.get("name")
	record = normalize_crm_organization(record)
	if same_site:
		doc = frappe.get_doc(doctype, record, for_update=True)
		doc.check_permission("read")
		doc.check_permission("write")
		return doc

	meta = frappe.get_meta(doctype)
	if any(not meta.has_field(field) for field in ("crm_record", "crm_site")):
		frappe.throw(_("Save CRM Settings to set up Contact and Address synchronization fields."))
	name = data.get("erpnext_record")
	if name:
		name = normalize_crm_organization(name)
		if not data.get("erpnext_site") or normalize_crm_site(data.get("erpnext_site")) != normalize_crm_site(
			get_url(allow_header_override=False)
		):
			frappe.throw(_("The {0} ERPNext site does not match this site.").format(doctype))
		doc = frappe.get_doc(doctype, name, for_update=True)
		doc.check_permission("read")
		doc.check_permission("write")
		if not any(
			row.link_doctype == "Customer" and row.link_name == customer for row in doc.get("links", [])
		):
			frappe.throw(_("The selected {0} is not linked to this Customer.").format(doctype))
		current = (doc.get("crm_site"), doc.get("crm_record"))
		if any(current) and current != (site, record):
			frappe.throw(
				_("The selected {0} is already linked to another CRM record or site.").format(doctype)
			)
		doc.crm_record = record
		doc.crm_site = site
		return doc
	name = frappe.db.get_value(doctype, {"crm_site": site, "crm_record": record}, "name", for_update=True)
	if name:
		doc = frappe.get_doc(doctype, name, for_update=True)
		doc.check_permission("read")
		doc.check_permission("write")
	else:
		doc = frappe.new_doc(doctype)
		doc.check_permission("create")
	doc.crm_record = record
	doc.crm_site = site
	return doc


def _append_contact_details(contact, data):
	for field in ("first_name", "middle_name", "last_name", "gender", "company_name", "designation"):
		if data.get(field) is not None:
			contact.set(field, data.get(field))
	if not contact.first_name:
		contact.first_name = data.get("full_name")
	email = data.get("email") or data.get("email_id")
	if email and not any(row.email_id == email for row in contact.get("email_ids", [])):
		contact.append("email_ids", {"email_id": email, "is_primary": not bool(contact.get("email_ids"))})
	mobile = data.get("mobile_no")
	if mobile and not any(row.phone == mobile for row in contact.get("phone_nos", [])):
		contact.append(
			"phone_nos",
			{
				"phone": mobile,
				"is_primary_mobile_no": not any(
					row.is_primary_mobile_no for row in contact.get("phone_nos", [])
				),
			},
		)


def _sync_organization_customer_relations(customer_data, customer, site):
	same_site = is_crm_installed() and site == normalize_crm_site(get_url(allow_header_override=False))
	contacts = frappe.parse_json(customer_data.get("contacts")) or []
	if not isinstance(contacts, list):
		frappe.throw(_("Contacts must be a list of CRM records."))
	for data in contacts:
		if not isinstance(data, dict):
			frappe.throw(_("Each Contact must contain its CRM record ID."))
		contact = _crm_relation_record("Contact", data, site, same_site, customer.name)
		if not same_site:
			_append_contact_details(contact, data)
		link_doc(contact, "Customer", customer.name)
		with allow_crm_identity_change(contact):
			contact.save()

	address_data = frappe.parse_json(customer_data.get("address"))
	if not address_data:
		return
	if not isinstance(address_data, dict):
		frappe.throw(_("Address must contain its CRM record ID."))
	address = _crm_relation_record("Address", address_data, site, same_site, customer.name)
	if not same_site:
		for field in ADDRESS_SYNC_FIELDS:
			if (
				field not in ("name", "modified", "crm_record", "crm_site")
				and address_data.get(field) is not None
			):
				address.set(field, address_data.get(field))
	link_doc(address, "Customer", customer.name)
	with allow_crm_identity_change(address):
		address.save()


def validate_frappe_crm_sync():
	CRMSettings = frappe.get_single("CRM Settings")
	if not CRMSettings.enable_frappe_crm_data_synchronization:
		frappe.throw(
			_("Frappe CRM data synchronization is not enabled on ERPNext. Contact System Manager of ERPNext.")
		)

	# Skip allowed_users validation if CRM is installed on the site.
	if is_crm_installed():
		return

	allowed_users = [d.user for d in CRMSettings.allowed_users]

	if frappe.session.user not in allowed_users:
		frappe.throw(
			_(
				"User not allowed to synchronize data from Frappe CRM on ERPNext. Contact System Manager of ERPNext."
			),
			exc=frappe.PermissionError,
		)


def is_crm_installed():
	return "crm" in frappe.get_installed_apps()


def remove_allowed_users_on_crm_install():
	try:
		CRMSettings = frappe.get_single("CRM Settings")

		if not CRMSettings.enable_frappe_crm_data_synchronization:
			return

		CRMSettings.allowed_users = []
		CRMSettings.save()
		click.secho("Removed 'Allowed Users' from CRM Settings.")
	except Exception:
		click.secho("'Allowed Users' from CRM Settings couldn't be cleared.")


def disable_frappe_crm_data_synchronization_on_crm_uninstall():
	try:
		CRMSettings = frappe.get_single("CRM Settings")

		if not CRMSettings.enable_frappe_crm_data_synchronization:
			return

		CRMSettings.enable_frappe_crm_data_synchronization = 0
		CRMSettings.save()
		click.secho("'Enable Frappe CRM Data Synchronization' on CRM Settings has been disabled.")
	except Exception:
		click.secho("'Enable Frappe CRM Data Synchronization' on CRM Settings could not be disabled.")
