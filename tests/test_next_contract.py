import unittest

from mesh_runner.outcome import CONTRACT


class NextContract(unittest.TestCase):
    def test_next_requires_a_piece_of_work(self):
        # A turn that only reads the task and writes a plan must not end with NEXT.
        line = next(i for i, l in enumerate(CONTRACT.splitlines()) if "↻ NEXT" in l)
        text = " ".join(CONTRACT.splitlines()[line:line + 3])
        self.assertIn("piece of work", text)
        self.assertIn("Reading the task and", text)
        self.assertIn("keep working in the same turn", text)


if __name__ == "__main__":
    unittest.main()
