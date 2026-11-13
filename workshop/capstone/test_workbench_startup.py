"""The documented fresh-state command can be repeated without erasing evidence."""

from http.client import HTTPConnection
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit


class FreshWorkbenchTests(unittest.TestCase):
    def launch(self, base):
        app = Path(__file__).resolve().with_name("app.py")
        process = subprocess.Popen(
            [sys.executable, "-B", str(app), "--solution", "--offline",
             "--state", str(base), "--new-state", "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        output = queue.Queue()

        def read():
            for line in process.stdout:
                output.put(line.rstrip())
            output.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        seen, url, state = [], None, None
        try:
            while url is None or state is None:
                line = output.get(timeout=30)
                if line is None:
                    self.fail("Workbench stopped before startup: " + "\n".join(seen))
                seen.append(line)
                if line.startswith("On-call workbench: "):
                    url = line.removeprefix("On-call workbench: ")
                if line.startswith("Workbench state: "):
                    state = Path(line.removeprefix("Workbench state: "))
            address = urlsplit(url)
            connection = HTTPConnection(address.hostname, address.port, timeout=5)
            try:
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200, payload)
            finally:
                connection.close()
            self.assertEqual(Path(payload["root"]), state)
            self.assertEqual(len(payload["instances"]), 2)
            self.assertTrue(all(len(instance["services"]) == 4 for instance in payload["instances"]))
            return state
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail("Workbench did not stop its owned HTTP environments")
            reader.join(3)
            process.stdout.close()
            self.assertEqual(process.returncode, 0, "\n".join(seen))

    def test_repeating_new_state_command_preserves_previous_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "workbench"
            old = base / "environments/instance-a/environment.json"
            old.parent.mkdir(parents=True)
            old.write_text('{"prior_exercise": true}\n')
            before = old.read_bytes()
            first = self.launch(base)
            evidence = first / "retained-evidence.json"
            evidence.write_text('{"keep": true}\n')
            second = self.launch(base)
            self.assertNotEqual(first, second)
            for state in (first, second):
                self.assertEqual(state.parent, base.parent.resolve())
                self.assertTrue(state.name.startswith(base.name + "-"))
                self.assertTrue((state / "workbench.json").is_file())
            self.assertEqual(old.read_bytes(), before)
            self.assertEqual(evidence.read_text(), '{"keep": true}\n')


if __name__ == "__main__":
    unittest.main()
