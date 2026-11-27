import unittest
from unittest.mock import Mock, patch

from hero_integration.services import file_transfer


class TestFileDetailIdentity(unittest.TestCase):
    def test_incomplete_or_contradictory_batches_cannot_advance_intake(self):
        rows = [{"id": 1, "uuid": "u1"}, {"id": 2, "uuid": "u2"}]
        first = dict(rows[0])
        for reply in ({}, {"file_uploads": None}, {"file_uploads": [first]},
                      {"file_uploads": [first, first]}, {"file_uploads": [first, {"uuid": "u2", "id": 9}]},
                      {"file_uploads": [first, {"uuid": "other", "id": 2}]}):
            with self.subTest(reply=reply), patch.object(file_transfer, "_detail_request"), self.assertRaises(file_transfer.TransferError):
                file_transfer.details(Mock(query=Mock(return_value=reply)), rows, None, "source")

    def test_missing_detail_allows_only_the_verified_sibling_to_advance(self):
        rows = [{"id":1,"uuid":"one"},{"id":2,"uuid":"two"}]
        with patch.object(file_transfer,"_detail_request"):
            client = Mock(query=Mock(return_value={"file_uploads":[rows[1]]}))
            self.assertEqual(file_transfer.details(client,rows,None,"source",allow_missing=True),{"two":rows[1]})
            for extra in (rows[1], {"id":9,"uuid":"other"}, {"id":9,"uuid":"one"}):
                client.query.return_value = {"file_uploads":[rows[1],extra]}
                with self.assertRaises(file_transfer.TransferError):
                    file_transfer.details(client,rows,None,"source",allow_missing=True)

    def test_matching_batch_may_arrive_in_another_order(self):
        rows = [{"id": 1, "uuid": "u1"}, {"id": 2, "uuid": "u2"}]
        with patch.object(file_transfer, "_detail_request"):
            found = file_transfer.details(Mock(query=Mock(return_value={"file_uploads": rows[::-1]})), rows, None, "source")
        self.assertEqual(found, {row["uuid"]: row for row in rows})
