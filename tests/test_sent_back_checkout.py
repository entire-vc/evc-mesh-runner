"""A typed wait the runner sends back must resume with a live Mesh checkout.

Mesh's ``wait_for`` (and ``ask_agent`` / ``ask_human``) releases the caller's checkout in the same
step. When the runner then refuses that wait (here: a pipeline in a terminal canceled state) and
puts the attempt back to ``ready``, the token it still carries is dead. The resume must acquire a
new lease for the same session; otherwise every turn command of the resumed session gets 409
"the caller does not hold this card's checkout"."""
from fakes import FakeGitLab, FakeMesh, next_turn, task, wait_pipeline, write_replies
from test_runner import Bench

SHA = 'a' * 40


class SentBackTypedWaitKeepsCheckout(Bench):
    def test_resume_after_canceled_pipeline_bounce_holds_the_checkout(self):
        t = task(0x98027a16)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        gl.pipeline_rows['g/app#16355'] = {'sha': SHA, 'id': 16355, 'status': 'canceled'}
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        write_replies(self.replies, {
            t['id'][:8]: wait_pipeline('g/app', 16355, until='green', after_sha=SHA,
                                       after_pipeline_id=16355, after_status='failed'),
            t['id'][:8] + ':resume': next_turn('retried the job'),
        })
        self.assertEqual(r.start_new(1), 0)
        a = self.finish_turn(r)
        self.assertEqual(a.state, 'ready', 'the canceled-pipeline wait is sent back')
        self.assertIn('is canceled; nothing will change it', a.wake_text)
        self.assertNotIn(t['id'], mesh.checkouts, 'Mesh released the lease with wait_for')
        r.resume_ready(1)
        running = r.store.by_state(self.agent.name, ('running',))
        self.assertEqual(len(running), 1, 'the bounced attempt resumes')
        resumed = running[0]
        self.assertEqual(resumed.id, a.id, 'same session')
        self.assertIn(t['id'], mesh.checkouts,
                      'the resumed session holds the card: its turn commands must not get 409')
        self.assertEqual(mesh.checkouts[t['id']]['session'], resumed.id)
        self.assertTrue(resumed.checkout.get('token'))
        self.finish_turn(r)


if __name__ == '__main__':
    import unittest
    unittest.main()
