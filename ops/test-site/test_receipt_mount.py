from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from controller import RECEIPT_DEST, Sandbox


class ReceiptMountTest(unittest.TestCase):
    def test_only_exact_writable_worker_bind_is_admitted(self):
        with TemporaryDirectory() as tmp:
            sandbox = object.__new__(Sandbox)
            sandbox.state = Path(tmp)
            source = sandbox.state / 'mutation-receipts'
            source.mkdir()
            m = dict(Type='bind', RW=True, Source=str(source), Destination=RECEIPT_DEST)
            check = lambda mount, sender: sandbox.check_receipt_mounts({'Mounts': [mount]}, sender)
            self.assertTrue(check(m, True))
            for change, sender in [({}, False), ({'RW': False}, True), ({'Type': 'volume'}, True),
                                   ({'Source': tmp}, True), ({'Destination': '/other'}, True),
                                   ({'Destination': RECEIPT_DEST + '/nested'}, True)]:
                with self.subTest(change=change, sender=sender), self.assertRaises(RuntimeError):
                    check(dict(m, **change), sender)
            self.assertFalse(check(dict(m, Source='/elsewhere', Destination='/elsewhere'), True))
