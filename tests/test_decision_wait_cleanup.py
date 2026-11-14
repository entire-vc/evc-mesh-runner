"""The reopen policy consumes durable Mesh receipts, never inferred pair ownership.

These are data-policy tests; no Mesh service or repository is mocked. The
server's owner/version compare-and-delete is covered by its database tests.
"""
import unittest

from mesh_runner import card_wait_reconcile


REQUEST = '00000000-0000-0000-0000-000000000064'
SOURCE = '00000000-0000-0000-0000-000000000001'
AGENT = '00000000-0000-0000-0000-000000000002'
WAIT = '00000000-0000-0000-0000-000000000003'
EDGE = '00000000-0000-0000-0000-000000000004'


class DecisionWaitOwnership(unittest.TestCase):
    def setUp(self):
        self.edge = dict(id=EDGE, task_id=REQUEST, depends_on_task_id=SOURCE,
                         dependency_type='blocks', condition='task_done', condition_satisfied=False,
                         generation=7, owner_wait_id=WAIT, owner_agent_id=AGENT)
        self.receipt = dict(id=WAIT, task_id=REQUEST, agent_id=AGENT, kind='card',
                            target_task_id=SOURCE, target_condition='task_done',
                            dependency_owned=True, dependency_id=EDGE, dependency_generation=7)

    def owned(self, edge=None, receipt=None):
        return card_wait_reconcile.owned_decision_wait(
            self.edge if edge is None else edge, self.receipt if receipt is None else receipt,
            REQUEST, SOURCE, AGENT)

    def test_exact_old_wait_is_eligible_for_compare_delete(self):
        self.assertEqual(self.owned(), (EDGE, 7, WAIT))

    def test_foreign_edge_on_same_pair_is_preserved(self):
        self.assertIsNone(self.owned(dict(self.edge, id='foreign', owner_wait_id=None)))
        self.assertIsNone(self.owned(dict(self.edge, owner_agent_id='foreign')))
        self.assertIsNone(self.owned(dict(self.edge, owner_wait_id='another-wait')))

    def test_operator_edit_and_ownership_transfer_are_preserved(self):
        for changes in ({'generation': 8}, {'owner_wait_id': None}, {'condition': 'accepted'},
                        {'dependency_type': 'relates_to'}, {'depends_on_task_id': AGENT}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.owned(dict(self.edge, **changes)))

    def test_receipt_must_prove_the_exact_card_agent_target_and_edge(self):
        for changes in ({'id': 'different'}, {'task_id': SOURCE}, {'agent_id': 'foreign'},
                        {'kind': 'decision'}, {'target_task_id': REQUEST},
                        {'dependency_id': 'different'}, {'dependency_generation': 8},
                        {'dependency_owned': False}, {'dependency_owned': 1},
                        {'target_condition': 'accepted'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.owned(receipt=dict(self.receipt, **changes)))

    def test_missing_legacy_provenance_is_not_backfilled(self):
        for field in ('owner_wait_id', 'owner_agent_id', 'generation'):
            edge = dict(self.edge)
            edge.pop(field)
            self.assertIsNone(self.owned(edge))
        for field in ('dependency_owned', 'dependency_id', 'dependency_generation'):
            receipt = dict(self.receipt)
            receipt.pop(field)
            self.assertIsNone(self.owned(receipt=receipt))

    def test_malformed_generation_is_not_delete_authority(self):
        for generation in (None, True, '7', -1):
            with self.subTest(generation=generation):
                self.assertIsNone(self.owned(dict(self.edge, generation=generation),
                                             dict(self.receipt, dependency_generation=generation)))

    def test_resolution_does_not_erase_durable_ownership(self):
        self.assertEqual(self.owned(receipt=dict(self.receipt, resolved_at='2026-11-13T05:40:00Z',
                                                resolution='superseded')), (EDGE, 7, WAIT))

    def test_unreadable_receipt_is_preserved(self):
        for receipt in (None, [], 'unreadable'):
            self.assertIsNone(card_wait_reconcile.owned_decision_wait(
                self.edge, receipt, REQUEST, SOURCE, AGENT))
