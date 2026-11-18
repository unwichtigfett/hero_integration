"""Project delivery uses the configured combined runtime."""
import unittest
from unittest.mock import Mock, patch
from hero_integration.services import project_send as ps, project_delivery


class TestProjectSend(unittest.TestCase):
    def test_fixed_operation_backend_is_removed(self):
        for name in ('legacy_preview', 'legacy_send', 'READ_DOCUMENT', 'MUTATION_DOCUMENT', '_read_hero', '_require_schema'):
            with self.subTest(name=name):
                self.assertFalse(hasattr(ps, name))

    def test_preview_forwards_project_phase_and_reader(self):
        client = Mock()
        with patch.object(project_delivery, 'preview', return_value={'combined': True}) as preview:
            self.assertEqual(ps.preview('P-1', phase_id=7, client=client), {'combined': True})
        preview.assert_called_once_with('P-1', phase_id=7, client=client)

    def test_send_forwards_the_reviewed_delivery_and_confirmation(self):
        client, mutation = Mock(), Mock()
        delivery = {'project': 'P-1', 'activities': []}
        with patch.object(project_delivery, 'send', return_value={'status': 'SENT'}) as send:
            self.assertEqual(ps.send('P-1', phase_id=7, fingerprint='proof', confirmed=True,
                client=client, mutation_client=mutation, delivery=delivery), {'status': 'SENT'})
        send.assert_called_once_with('P-1', phase_id=7, fingerprint='proof', delivery=delivery,
            confirmed=True, client=client, mutation_client=mutation)

    def test_runtime_rejection_is_reported_without_retry(self):
        with patch.object(project_delivery, 'send', side_effect=ValueError('Changed')) as send:
            with self.assertRaises(ps.ProjectSendError):
                ps.send('P-1', fingerprint='proof', confirmed=True)
        self.assertEqual(send.call_count, 1)
