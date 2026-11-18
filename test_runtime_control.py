import os, tempfile, unittest
from pathlib import Path

class RuntimeTests(unittest.TestCase):
    def test_state_and_command(self):
        import runtime_control as r
        with tempfile.TemporaryDirectory() as td:
            old=(r.RUNTIME,r.STATE,r.COMMAND,r.LOCK)
            r.RUNTIME=Path(td); r.STATE=r.RUNTIME/'state.json'; r.COMMAND=r.RUNTIME/'command.json'; r.LOCK=r.RUNTIME/'lock'
            try:
                r.update_state(status='running', completed=3)
                self.assertEqual(r.read_state()['completed'],3)
                r.send_command('pause'); self.assertEqual(r.take_command(),'pause'); self.assertIsNone(r.take_command())
                self.assertTrue(r.acquire_lock()); r.release_lock(); self.assertFalse(r.LOCK.exists())
            finally: r.RUNTIME,r.STATE,r.COMMAND,r.LOCK=old
if __name__=='__main__': unittest.main()
