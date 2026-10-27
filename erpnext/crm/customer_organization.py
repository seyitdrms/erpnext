from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, get_url

CUSTOMER_ORGANIZATION_INDEX = "unique_customer_crm_organization"


@contextmanager
def allow_crm_identity_change(doc):
	"""Authorize only this server-side document object for the duration of a controlled sync save."""
	allowed = frappe.flags.get("frappe_crm_identity_documents", ())
	frappe.flags.frappe_crm_identity_documents = (*allowed, id(doc))
	try:
		yield
	finally:
		if allowed:
			frappe.flags.frappe_crm_identity_documents = allowed
		else:
			frappe.flags.pop("frappe_crm_identity_documents", None)


def _validate_crm_identity_change(doc, fields):
	fields = [field for field in fields if doc.meta.has_field(field)]
	stored = (
		frappe.db.get_value(doc.doctype, doc.name, fields, as_dict=True, for_update=True)
		if doc.name
		else None
	)
	previous = tuple(stored.get(field) or None for field in fields) if stored else (None,) * len(fields)
	current = tuple(doc.get(field) or None for field in fields)
	changed = previous != current
	if changed and id(doc) not in frappe.flags.get("frappe_crm_identity_documents", ()):
		frappe.throw(
			_("CRM synchronization identities can only be changed through the synchronization APIs."),
			frappe.PermissionError,
		)
	return changed


def normalize_crm_site(crm_site=None):
	if not crm_site:
		if "crm" not in frappe.get_installed_apps():
			frappe.throw(_("CRM Site is required when Frappe CRM is installed on another site."))
		crm_site = get_url(allow_header_override=False)

	try:
		value = cstr(crm_site).strip()
		parsed = urlsplit(value)
		if (
			parsed.scheme.lower() not in ("http", "https")
			or not parsed.hostname
			or parsed.username is not None
			or parsed.password is not None
			or parsed.query
			or parsed.fragment
			or "\\" in value
			or any(character.isspace() for character in value)
		):
			raise ValueError
		host = parsed.hostname.encode("idna").decode("ascii").lower()
		if ":" in host:
			host = f"[{host}]"
		port = parsed.port
		if port and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
			host = f"{host}:{port}"
		result = urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", ""))
		if len(result) > 255:
			raise ValueError
		return result
	except (ValueError, UnicodeError):
		frappe.throw(_("CRM Site must be an HTTP or HTTPS site URL without credentials, query or fragment."))


def normalize_crm_organization(organization):
	if not isinstance(organization, str):
		frappe.throw(_("A valid CRM record ID is required."))
	organization = organization.strip()
	if not organization or len(organization) > 140:
		frappe.throw(_("A valid CRM record ID is required."))
	return organization


def require_customer_organization_fields():
	meta = frappe.get_meta("Customer")
	for fieldname in ("crm_organization", "crm_site"):
		field = meta.get_field(fieldname)
		if not field:
			frappe.throw(
				_(
					"Save CRM Settings with Frappe CRM data synchronization enabled to set up organization links."
				)
			)
		if field.fieldtype != "Data":
			frappe.throw(_("The Customer field {0} has an incompatible definition.").format(fieldname))


def validate_customer_organization_field_definitions():
	for doctype, record_field in (
		("Customer", "crm_organization"),
		("Contact", "crm_record"),
		("Address", "crm_record"),
	):
		meta = frappe.get_meta(doctype)
		for fieldname in (record_field, "crm_site"):
			field = meta.get_field(fieldname)
			if field and field.fieldtype != "Data":
				frappe.throw(
					_("The {0} field {1} has an incompatible definition.").format(doctype, fieldname)
				)


def add_customer_organization_constraint():
	require_customer_organization_fields()
	for doctype, record_field, index in (
		("Customer", "crm_organization", CUSTOMER_ORGANIZATION_INDEX),
		("Contact", "crm_record", "unique_contact_crm_record"),
		("Address", "crm_record", "unique_address_crm_record"),
	):
		if frappe.db.has_index(f"tab{doctype}", index):
			continue
		# Unlinked records must not share an empty-string identity in the unique index.
		record = frappe.qb.DocType(doctype)
		identity = record[record_field]
		(
			frappe.qb.update(record)
			.set(identity, None)
			.set(record.crm_site, None)
			.where(
				(identity.isnull() | (identity == "")) & (record.crm_site.isnull() | (record.crm_site == ""))
			)
		).run()
		frappe.db.add_unique(doctype, ["crm_site", record_field], constraint_name=index)


def validate_customer_organization(customer):
	if not customer.meta.has_field("crm_organization") and not customer.meta.has_field("crm_site"):
		return

	organization = customer.get("crm_organization")
	site = customer.get("crm_site")
	if not organization and not site:
		customer.set("crm_organization", None)
		customer.set("crm_site", None)
	else:
		require_customer_organization_fields()
		if not organization:
			frappe.throw(_("CRM Organization is required when CRM Site is set."))
		if customer.customer_type != "Company":
			frappe.throw(_("Only Company customers can be linked to a CRM Organization."))
		organization = normalize_crm_organization(organization)
		site = normalize_crm_site(site)
		customer.set("crm_organization", organization)
		customer.set("crm_site", site)
	pair_changed = _validate_crm_identity_change(customer, ("crm_site", "crm_organization"))
	if organization:
		if (
			pair_changed
			and "crm" in frappe.get_installed_apps()
			and site == normalize_crm_site(get_url(allow_header_override=False))
		):
			crm_organization = frappe.get_doc("CRM Organization", organization)
			crm_organization.check_permission("read")
			crm_organization.check_permission("write")
			if (
				crm_organization.get("erpnext_customer")
				and crm_organization.erpnext_customer != customer.name
			):
				frappe.throw(_("This CRM Organization already identifies another ERPNext Customer."))
			if (
				crm_organization.get("erpnext_site")
				and normalize_crm_site(crm_organization.erpnext_site) != site
			):
				frappe.throw(_("This CRM Organization already identifies another ERPNext site."))
		other = frappe.db.exists(
			"Customer",
			{"crm_site": site, "crm_organization": organization, "name": ["!=", customer.name or ""]},
		)
		if other:
			frappe.throw(_("This CRM Organization is already linked to another Customer."))


def get_customer_for_organization_write(customer, modified=None):
	require_customer_organization_fields()
	customer = normalize_crm_organization(customer)
	doc = frappe.get_doc("Customer", customer, for_update=True)
	doc.check_permission("read")
	doc.check_permission("write")
	if doc.customer_type != "Company":
		frappe.throw(_("Only Company customers can be linked to a CRM Organization."))
	if modified and get_datetime(doc.modified) != get_datetime(modified):
		frappe.throw(
			_("The Customer has changed. Reload it before updating its CRM Organization."),
			frappe.TimestampMismatchError,
		)
	return doc


def validate_customer_organization_merge(customer, new_name):
	fields = [field for field in ("crm_organization", "crm_site") if customer.meta.has_field(field)]
	if not fields:
		return
	target = frappe.db.get_value("Customer", new_name, fields, as_dict=True, for_update=True)
	if any(customer.get(field) for field in fields) or (
		target and any(target.get(field) for field in fields)
	):
		frappe.throw(_("Unlink the CRM Organization before merging either Customer."))


def validate_customer_organization_deletion(customer):
	if customer.get("crm_organization") or customer.get("crm_site"):
		frappe.throw(_("Unlink the CRM Organization before deleting this Customer."))


def validate_crm_record(doc, method=None):
	if not doc.meta.has_field("crm_record") and not doc.meta.has_field("crm_site"):
		return
	record = doc.get("crm_record")
	site = doc.get("crm_site")
	if not record and not site:
		doc.set("crm_record", None)
		doc.set("crm_site", None)
		_validate_crm_identity_change(doc, ("crm_site", "crm_record"))
		return
	if not record or not site:
		frappe.throw(_("CRM Record and CRM Site must be set together."))
	record = normalize_crm_organization(record)
	site = normalize_crm_site(site)
	doc.set("crm_record", record)
	doc.set("crm_site", site)
	_validate_crm_identity_change(doc, ("crm_site", "crm_record"))
	if frappe.db.exists(
		doc.doctype, {"crm_site": site, "crm_record": record, "name": ["!=", doc.name or ""]}
	):
		frappe.throw(_("This CRM record is already linked to another {0}.").format(doc.doctype))
