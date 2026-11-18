"""Offline category lifecycle, write authorization and source-address boundaries."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from hero_integration.client.graphql import HeroClientError


APP = Path(__file__).parents[1]
FIELD = "custom_project_image_category"


class Denied(Exception):
    pass


class Stale(Exception):
    pass


class ConfigurationError(Exception):
    pass


class BindingError(Exception):
    pass


class Doc:
    def __init__(self, name, *, writable=True, readable=True, **values):
        self.name = name
        self.values = {"modified": "v1", **values}
        self.previous = deepcopy(self.values)
        self.writable, self.readable = writable, readable
        self.saves = 0
        self.on_save = None

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def check_permission(self, permission):
        if not (self.readable if permission == "read" else self.writable):
            raise Denied()

    def get_doc_before_save(self):
        return self.previous

    def save(self):
        if self.on_save:
            self.on_save(self)
        self.saves += 1
        self.values["modified"] = "v2"


class Cache:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def get_value(self, key):
        self.reads += 1
        return deepcopy(self.values.get(key))

    def set_value(self, key, value, **kwargs):
        assert kwargs == {"expires_in_sec": 300}
        self.values[key] = deepcopy(value)


def load(path, fake):
    spec = importlib.util.spec_from_file_location(path.stem + "_isolated", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.project = Doc("PROJECT-1")
        self.image = Doc("IMAGE-1", attached_to_doctype="Project", attached_to_name="PROJECT-1", file_type="PNG")
        self.category = Doc("Dach", disabled=0)
        self.docs = {("Project", "PROJECT-1"): self.project, ("File", "IMAGE-1"): self.image,
            ("Project Image Category", "Dach"): self.category}
        self.exposed = []
        self.fake = NS(session=NS(user="reader@example.invalid"), local=NS(site="dev.example.invalid"),
            PermissionError=Denied, TimestampMismatchError=Stale, cache=Cache(),
            throw=lambda message, exception=ValueError: (_ for _ in ()).throw(exception(message)),
            _=lambda text: text, get_doc=lambda doctype, name: self.docs[doctype, name],
            whitelist=lambda **options: self.exposed.append(options) or (lambda function: function))

        def text(value, length, required=True):
            if not isinstance(value, str) or len(value) > length or (required and not value.strip()):
                raise ValueError()
            return value.strip()

        def read(doctype, name):
            doc = self.fake.get_doc(doctype, name)
            doc.check_permission("read")
            return doc

        modules = {
            "frappe": self.fake,
            "hero_integration.services.project_workspace": NS(IMAGE_TYPES=("PNG", "JPG"), _text=text, _read=read),
            "hero_integration.services.project_send": NS(ProjectSendError=BindingError),
            "hero_integration.client.settings": NS(HeroConfigurationError=ConfigurationError, get_configuration=Mock()),
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.categories = load(APP / "services/project_image_categories.py", self.fake)
        self.image.on_save = self.categories.validate_file_category
        self.address = load(APP / "services/project_site_address.py", self.fake)
        self.real_client = self.address._client
        self.link = NS(name="LINK-1", source_id="42", source_instance="source-a")
        self.client = NS(endpoint=NS(url="https://hero.example.invalid/graphql"),
            query=Mock(return_value={"project_match": {"id": 42,
                "address": {"full_address": "Synthetic Site 1, 12345 Example City"}}}))
        self.address._binding = Mock(return_value=self.link)
        self.address._client = Mock(return_value=(self.client, 1))

    def test_import_category_uses_mapping_or_available_idempotent_fallback(self):
        active = {"Dach"}
        created = []
        self.fake.db = NS(exists=lambda dt, key: (key in active if isinstance(key, str)
            else key.get("name") in active and not key.get("disabled")))
        self.fake.only_for = Mock()
        def get_doc(values):
            def insert():
                name = values["category_name"]
                active.add(name)
                created.append(name)
                return NS(name=name)
            return NS(insert=insert)
        self.fake.get_doc = get_doc
        self.image.previous = None
        self.image.set(FIELD, "Dach")
        self.categories.prepare_import_category(self.image, {FIELD: "Dach"})
        self.assertEqual(self.image.get(FIELD), "Dach")
        self.assertEqual(created, [])
        for value in (None, "missing", "HERO-Import"):
            self.image.set(FIELD, value)
            self.categories.prepare_import_category(self.image, {FIELD: value})
            self.assertEqual(self.image.get(FIELD), "HERO-Import")
        self.assertEqual(created, ["HERO-Import"])
        self.fake.only_for.assert_called_once_with("System Manager")

    def test_import_does_not_backfill_legacy_empty_or_change_unmapped_category(self):
        self.fake.db = NS(exists=Mock(side_effect=AssertionError("no category lookup")))
        for value in (None, "Dach"):
            self.image.set(FIELD, value)
            self.categories.prepare_import_category(self.image, {"file_name": "new.png"})
            self.assertEqual(self.image.get(FIELD), value)

    def test_import_relink_classifies_legacy_image_before_native_validation(self):
        self.image.previous["attached_to_doctype"] = None
        self.fake.db = NS(exists=lambda dt, key: True)
        self.categories.prepare_import_category(self.image, {})
        self.assertEqual(self.image.get(FIELD), "HERO-Import")

    def save(self, **kwargs):
        return self.categories.set_image_category("PROJECT-1", "IMAGE-1", "Dach", "v1", **kwargs)

    def test_transport_and_admin_configuration_permissions(self):
        self.assertEqual(self.exposed, [{"methods": ["GET"]}, {"methods": ["POST"]}, {"methods": ["GET"]}])
        meta = json.loads((APP / "hero_integration/doctype/project_image_category/project_image_category.json").read_text())
        read = next(row for row in meta["permissions"] if row["role"] == "All")
        admin = next(row for row in meta["permissions"] if row["role"] == "System Manager")
        self.assertEqual(read, {"role": "All", "read": 1})
        self.assertTrue(all(admin[key] for key in ("read", "write", "create", "delete")))
        self.assertEqual(self.categories.DEFAULTS, ("Dach", "Elektro"))

    def test_assignment_uses_native_save_and_preserves_project(self):
        result = self.save()
        self.assertEqual(result, {"file": "IMAGE-1", "category": "Dach", "modified": "v2"})
        self.assertEqual(self.image.saves, 1)
        self.assertEqual(self.project.saves, 0)

    def test_readonly_project_or_file_cannot_be_classified(self):
        for target in (self.project, self.image):
            target.writable = False
            with self.assertRaises(Denied):
                self.save()
            self.assertEqual(self.image.saves, 0)
            target.writable = True

    def test_guest_cannot_change_or_fetch_business_data(self):
        self.fake.session.user = "Guest"
        with self.assertRaises(Denied):
            self.save()
        with self.assertRaises(Denied):
            self.address.get_site_address("PROJECT-1")
        self.assertEqual(self.image.saves, 0)
        self.client.query.assert_not_called()

    def test_other_project_and_non_images_are_rejected(self):
        for field, value in (("attached_to_name", "OTHER"), ("attached_to_doctype", "Customer"), ("file_type", "PDF")):
            original = self.image.values[field]
            self.image.values[field] = value
            with self.assertRaises(Denied):
                self.save()
            self.image.values[field] = original
        self.assertEqual(self.image.saves, 0)

    def test_stale_expected_modified_never_saves(self):
        self.image.values["modified"] = "changed"
        with self.assertRaises(Stale):
            self.save()
        self.assertEqual(self.image.saves, 0)

    def test_disabled_category_rejects_new_assignment_but_not_unchanged_existing_use(self):
        self.category.values["disabled"] = 1
        with self.assertRaises(ValueError):
            self.save()
        self.image.previous[FIELD] = "Dach"
        self.categories.validate_file_category(self.image)
        self.image.values[FIELD] = None
        with self.assertRaisesRegex(ValueError, "required"):
            self.categories.validate_file_category(self.image)

    def test_direct_native_file_updates_obey_the_same_project_guard(self):
        self.project.writable = False
        self.image.values[FIELD] = "Dach"
        with self.assertRaises(Denied):
            self.categories.validate_file_category(self.image)

    def test_moving_a_categorized_file_checks_the_original_and_new_project(self):
        self.image.previous[FIELD] = "Dach"
        self.image.values[FIELD] = "Dach"
        self.image.values["attached_to_name"] = "OTHER"
        self.docs["Project", "OTHER"] = Doc("OTHER", writable=False)
        with self.assertRaises(Denied):
            self.categories.validate_file_category(self.image)

    def test_existing_foreign_category_field_definition_is_not_overwritten(self):
        field = NS(fieldtype="Link", options="Project Image Category", read_only=0, permlevel=0)
        self.fake.get_meta = lambda doctype: NS(get_field=lambda name: field)
        self.categories.ensure_field()
        field.options = "Customer"
        with self.assertRaises(ValueError):
            self.categories.ensure_field()

    def test_non_project_file_cannot_store_a_category_even_through_native_crud(self):
        self.image.values.update({FIELD: "Dach", "attached_to_doctype": "Customer"})
        with self.assertRaises(ValueError):
            self.categories.validate_file_category(self.image)

    def test_new_project_image_requires_a_category_but_legacy_images_remain_valid(self):
        self.image.previous = None
        with self.assertRaisesRegex(ValueError, "before uploading"):
            self.categories.validate_file_category(self.image)
        self.image.previous = deepcopy(self.image.values)
        self.categories.validate_file_category(self.image)

    def test_native_upload_folder_binds_initial_category_without_filename_guessing(self):
        self.image.previous = None
        self.image.values["folder"] = "Home/Attachments/managed-folder"
        self.fake.db = NS(get_value=Mock(return_value=NS(name="Dach", disabled=0)))
        self.categories.validate_file_category(self.image)
        self.assertEqual(self.image.get(FIELD), "Dach")
        self.fake.db.get_value.assert_called_once_with("Project Image Category",
            {"upload_folder": "Home/Attachments/managed-folder"}, ["name", "disabled"], as_dict=True)

    def test_disabled_or_missing_upload_folder_binding_cannot_create_an_uncategorized_image(self):
        self.image.previous = None
        self.image.values["folder"] = "Home/Attachments/missing"
        for value in [None, NS(name="Dach", disabled=1)]:
            self.fake.db = NS(get_value=Mock(return_value=value))
            with self.assertRaisesRegex(ValueError, "before uploading"):
                self.categories.validate_file_category(self.image)

    def test_existing_unclassified_file_newly_attached_to_project_also_requires_category(self):
        self.image.previous["attached_to_doctype"] = "Customer"
        with self.assertRaisesRegex(ValueError, "before uploading"):
            self.categories.validate_file_category(self.image)

    def test_upload_options_use_native_folder_and_check_project_and_category_access(self):
        self.category.values["upload_folder"] = "FOLDER-1"
        self.docs["File", "FOLDER-1"] = Doc("FOLDER-1", is_folder=1)
        self.assertEqual(self.categories.get_upload_options("PROJECT-1", "Dach"),
            {"project": "PROJECT-1", "category": "Dach", "folder": "FOLDER-1"})
        self.project.writable = False
        with self.assertRaises(Denied):
            self.categories.get_upload_options("PROJECT-1", "Dach")
        self.project.writable = True
        self.category.values["disabled"] = 1
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.categories.get_upload_options("PROJECT-1", "Dach")

    def test_category_clear_is_rejected_and_empty_existing_references_are_not_backfilled(self):
        with self.assertRaisesRegex(ValueError, "required"):
            self.categories.set_image_category("PROJECT-1", "IMAGE-1", "", "v1")
        self.categories.validate_file_category(self.image)
        self.assertIsNone(self.image.get(FIELD))

    def test_managed_folder_creation_is_admin_only_and_never_adopts_unknown_folders(self):
        self.fake.only_for = Mock()
        self.fake.db = NS(get_value=Mock(return_value=1))
        self.fake.generate_hash = lambda length: "abcdef12"
        created = []
        folder = NS(name="Home/Attachments/new-managed-folder")
        def create(data):
            created.append(data)
            return NS(insert=lambda: folder)
        self.fake.get_doc = create
        self.category.db_set = lambda field, value, **kwargs: self.category.set(field, value)
        self.assertEqual(self.categories.ensure_upload_folder(self.category), folder.name)
        self.assertEqual(created[0]["is_folder"], 1)
        self.assertEqual(created[0]["folder"], "Home/Attachments")
        self.fake.only_for.assert_called_with("System Manager")
        self.assertEqual(self.category.get("upload_folder"), folder.name)

    def test_defaults_are_create_only_and_ordinary_migration_does_not_reseed(self):
        created = []
        self.fake.db = NS(exists=lambda doctype, name: name == "Dach")
        self.fake.get_doc = lambda data: NS(insert=lambda: created.append(data["category_name"]))
        with patch.object(self.categories, "ensure_field"):
            self.categories.install_defaults()
        self.assertEqual(created, ["Elektro"])
        source = (APP / "install.py").read_text().split("def after_migrate()", 1)[1].split("def ensure_pending", 1)[0]
        self.assertIn("ensure_field()", source)
        self.assertNotIn("install_defaults()", source)

    def test_address_is_the_confirmed_hero_field_not_the_customer_address(self):
        self.project.values["custom_adresse"] = "CUSTOMER-ADDRESS"
        result = self.address.get_site_address("PROJECT-1")
        self.assertEqual(result["address"], "Synthetic Site 1, 12345 Example City")
        self.assertEqual(result["source"], "ProjectMatch.address.full_address")
        args = self.client.query.call_args.args
        self.assertIn("address { full_address }", args[0])
        self.assertNotIn("customer", args[0])
        self.assertEqual(args[1], {"id": 42})

    def test_project_permission_precedes_cache_binding_and_network(self):
        self.address.get_site_address("PROJECT-1")
        reads = self.fake.cache.reads
        self.project.readable = False
        with self.assertRaises(Denied):
            self.address.get_site_address("PROJECT-1")
        self.assertEqual(self.fake.cache.reads, reads)
        self.assertEqual(self.client.query.call_count, 1)

    def test_cache_is_bounded_and_changes_with_source_revision(self):
        self.address.get_site_address("PROJECT-1")
        self.address.get_site_address("PROJECT-1")
        self.assertEqual(self.client.query.call_count, 1)
        self.address._client.return_value = (self.client, 2)
        self.address.get_site_address("PROJECT-1")
        self.assertEqual(self.client.query.call_count, 2)

    def test_wrong_upstream_identity_or_missing_address_has_no_customer_fallback(self):
        for response, state in [
            ({"project_match": {"id": 99, "address": {"full_address": "Other"}}}, "unavailable"),
            ({"project_match": {"id": 42, "address": None}}, "missing"),
        ]:
            self.fake.cache.values.clear()
            self.client.query.return_value = response
            result = self.address.get_site_address("PROJECT-1")
            self.assertEqual(result["state"], state)
            self.assertIsNone(result["address"])

    def test_unavailable_or_unbound_hero_is_explicit_and_does_not_leak_raw_errors(self):
        self.client.query.side_effect = HeroClientError("sensitive upstream diagnostic")
        result = self.address.get_site_address("PROJECT-1")
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("sensitive", str(result))
        self.address._binding.side_effect = BindingError()
        self.assertEqual(self.address.get_site_address("PROJECT-1")["state"], "not_linked")

    def test_unconfirmed_or_different_hero_account_cannot_be_used(self):
        config = NS(source_binding_confirmed=False, source_instance="source-a")
        with patch.object(self.address, "get_configuration", return_value=config):
            with self.assertRaises(ConfigurationError):
                self.real_client(self.link)
            config.source_binding_confirmed = True
            config.source_instance = "other-source"
            with self.assertRaises(ConfigurationError):
                self.real_client(self.link)


if __name__ == "__main__":
    unittest.main()
