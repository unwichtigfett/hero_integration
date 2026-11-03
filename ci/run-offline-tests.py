"""Run database-independent tests; the full suite also runs on a Frappe site."""

from __future__ import annotations

import sys
import unittest
import argparse

from frappe.tests import IntegrationTestCase


SITE_ONLY_CLASSES = {
    "hero_integration.tests.test_business_numbers_frappe.TestBusinessNumbersOnERPNext",
    "hero_integration.tests.test_native_task_deadline_frappe.TestNativeTaskDeadline",
}


def individual_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from individual_tests(item)
        else:
            yield item


discovered = unittest.defaultTestLoader.discover("hero_integration/tests", top_level_dir=".")
offline = unittest.TestSuite()
site_only = 0
for test in individual_tests(discovered):
    cls = type(test)
    class_name = f"{cls.__module__}.{cls.__qualname__}"
    if issubclass(cls, IntegrationTestCase) or class_name in SITE_ONLY_CLASSES:
        site_only += 1
    else:
        offline.addTest(test)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--junit-xml-output")
args = parser.parse_args()
print(f"Site-dependent tests reserved for the full site suite: {site_only}", flush=True)
if args.junit_xml_output:
    import xmlrunner

    with open(args.junit_xml_output, "wb") as output:
        result = xmlrunner.XMLTestRunner(output=output, verbosity=1).run(offline)
else:
    result = unittest.TextTestRunner(verbosity=1).run(offline)
sys.exit(0 if result.wasSuccessful() else 1)
