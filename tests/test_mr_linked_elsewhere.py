"""An MR authored by the agent's GitLab user but linked in Mesh to another card is that
card's MR (its own reviewer and merge gate), so a wait for its merge is accepted; behind a merge gate
a SHIP verdict on the MR head on its card stands in for the legacy ``verified-by:`` label."""
import datetime as dt
import unittest

from fakes import AGENT_ID, task
from mesh_runner.mesh import MeshError
from test_own_mr_by_author import authored
from test_runner import Bench
from test_unattended_ci_wait import MR, MR_URL, SHA, UnattendedWait, merged_line

LINK = {'url': MR_URL, 'link_type': 'pr', 'status': 'open'}
GATE = ('merge-gate', '{iid}', '{project}', '{sha}')
REVIEWER = 'reviewer-agent-id'


class MrLinkedToAnotherCard(Bench):
    flow, turn, assert_bounced = UnattendedWait.flow, UnattendedWait.turn, UnattendedWait.assert_bounced

    def bounced(self, a, r, t):
        self.assert_bounced(a, r, t, f'MR {MR} has no review label (verified-by:...)', 'nobody will merge it',
                            'until=merged will not arrive on its own')

    def other_card(self, r, linked=True):
        other = task(2, cat='review', assignee_id=REVIEWER)   # another lane holds it now
        r.mesh.tasks[other['id']] = other
        if linked:
            r.mesh.links = {**getattr(r.mesh, 'links', {}), other['id']: [dict(LINK)]}
        return other

    def test_named_card_with_the_link_takes_the_mr(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r)
        gl.mr_rows[MR]['title'] = f"perf(build): share bindings #{other['id'][:8]}"
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['target'], MR)

    def test_named_in_the_source_branch_too(self):
        r, t, gl = self.flow(mr=authored('builder', 'manual'), linked=False)
        other = self.other_card(r)
        gl.mr_rows[MR]['source_branch'] = f"perf/{other['id'][:8]}-bindings"
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_red_control_named_card_without_the_link_keeps_the_mr_ours(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r, linked=False)
        gl.mr_rows[MR]['title'] = f"perf(build): share bindings #{other['id'][:8]}"
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)

    def test_red_control_unknown_short_ids_are_skipped(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        gl.mr_rows[MR]['description'] = 'built 20261114, see deadbeef and cafebabe'
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)

    def test_another_open_card_of_the_agent_with_the_link_takes_the_mr(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r)          # the MR names no card: found among the lane's open cards
        held = r.store.create(self.agent.name, other['id'], 'claude', title='other')
        r.store.update(held, state='waiting')
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_link_on_this_task_still_makes_it_ours(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=True)
        other = self.other_card(r)
        gl.mr_rows[MR]['title'] = f"perf(build): share bindings #{other['id'][:8]}"
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)

    def deny(self, r, short):
        """Mesh answers 403 (``workspace access denied``) for ``short``: an 8-hex naming no card
        this agent can read, as a commit fragment in a release MR description did live."""
        real, asked = r.mesh.task_by_short, []
        def by_short(s):
            asked.append(s)
            if s == short:
                raise MeshError(403, 'workspace access denied')
            return real(s)
        r.mesh.task_by_short = by_short
        return asked

    def test_red_control_unreadable_short_id_keeps_the_mr_ours(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        self.deny(r, 'b3f7d100')
        gl.mr_rows[MR]['description'] = 'cut-off main; head b3f7d100 not included'
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)                # skipped, not a failed check that keeps the wait

    def test_unreadable_short_id_before_the_linked_card_is_skipped(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r)
        asked = self.deny(r, 'b3f7d100')
        gl.mr_rows[MR]['title'] = 'release: next (head b3f7d100)'
        gl.mr_rows[MR]['description'] = f"card #{other['id'][:8]}"
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(asked[:2], ['b3f7d100', other['id'][:8]])

    def test_the_first_linked_card_ends_the_search(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r)
        asked = self.deny(r, 'b3f7d100')
        gl.mr_rows[MR]['title'] = f"release: next (#{other['id'][:8]})"
        gl.mr_rows[MR]['description'] = 'head b3f7d100 not included'
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')
        self.assertNotIn('b3f7d100', asked)

    def test_task_id_in_the_mr_still_makes_it_ours(self):
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=False)
        other = self.other_card(r)
        gl.mr_rows[MR]['title'] = f"fix: #{t['id'][:8]} and #{other['id'][:8]}"
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)


class ShipVerdictBehindTheMergeGate(Bench):
    flow, turn, assert_bounced = UnattendedWait.flow, UnattendedWait.turn, UnattendedWait.assert_bounced
    bounced = MrLinkedToAnotherCard.bounced

    def verdict(self, r, tid, body, author=REVIEWER):
        r.mesh.add_comment(tid, body, author=author)

    def test_ship_on_the_head_accepts_the_wait_without_the_label(self):
        for line in (f'Verify: SHIP @{SHA}', f'Verify: SHIP @{SHA[:12]}', f'review verdict: SHIP on {SHA[:8]}'):
            with self.subTest(line=line):
                self.tearDown(), self.setUp()
                self.cfg.merge_command = GATE
                r, t, gl = self.flow(mr=authored('builder', 'success'), linked=True)
                self.verdict(r, t['id'], line)
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_ship_on_the_card_the_mr_is_linked_to(self):
        self.cfg.merge_command = GATE
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=True)
        other = MrLinkedToAnotherCard.other_card(self, r)
        gl.mr_rows[MR]['title'] = f"perf(build): share bindings #{other['id'][:8]}"
        self.verdict(r, other['id'], f'Verify: SHIP @{SHA}')
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def linked_both(self):
        self.cfg.merge_command = GATE
        r, t, gl = self.flow(mr=authored('builder', 'success'), linked=True)
        other = MrLinkedToAnotherCard.other_card(self, r)
        gl.mr_rows[MR]['title'] = f"perf(build): share bindings #{other['id'][:8]}"
        return r, t, other

    def test_newest_head_verdict_across_the_cards_decides(self):
        early = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        late = early + dt.timedelta(hours=1)
        cases = {   # (card of the SHIP, its time, card of the DO-NOT-SHIP, its time, accepted)
            'newer_rejection_on_the_linked_card': ('mine', early, 'other', late, False),
            'newer_rejection_on_the_waiting_card': ('other', early, 'mine', late, False),
            'newer_ship_on_the_waiting_card': ('mine', late, 'other', early, True),
            'newer_ship_on_the_linked_card': ('other', late, 'mine', early, True),
        }
        for name, (ship_on, ship_at, dns_on, dns_at, accepted) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, other = self.linked_both()
                card = {'mine': t['id'], 'other': other['id']}
                r.mesh.add_comment(card[ship_on], f'Verify: SHIP @{SHA}', author=REVIEWER, at=ship_at)
                r.mesh.add_comment(card[dns_on], f'Verify: DO-NOT-SHIP @{SHA}', author=REVIEWER, at=dns_at)
                a = self.turn(r, t, merged_line())
                if accepted:
                    self.assertEqual(a.state, 'waiting')
                else:
                    self.bounced(a, r, t)
                r.store.db.close()

    def test_red_controls_are_still_sent_back(self):
        old = 'c' * 40
        cases = {
            'no_verdict': (True, []),
            'no_merge_gate': (False, [f'Verify: SHIP @{SHA}']),
            'older_head': (True, [f'Verify: SHIP @{old}']),
            'no_commit_named': (True, ['Verify: SHIP']),
            'newer_do_not_ship': (True, [f'Verify: SHIP @{SHA}', f'Verify: DO-NOT-SHIP @{SHA}']),
            'enumerated': (True, [f'Verify: reply SHIP or DO-NOT-SHIP @{SHA}']),
            'own_prose': (True, [f'Twoj independent review: SHIP @{SHA}']),
        }
        for name, (gate, lines) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                if gate:
                    self.cfg.merge_command = GATE
                r, t, gl = self.flow(mr=authored('builder', 'success'), linked=True)
                for line in lines:
                    self.verdict(r, t['id'], line, author=AGENT_ID if name == 'own_prose' else REVIEWER)
                a = self.turn(r, t, merged_line())
                self.bounced(a, r, t)
                r.store.db.close()


del Bench, UnattendedWait   # imported fixtures only: their tests run in their own modules

if __name__ == '__main__':
    unittest.main()
