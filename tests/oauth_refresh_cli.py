#!/usr/bin/env python3
"""Exercise the built CLI with synthetic credentials and a local curl replacement."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else Path("zig-out/bin/codex-auth").resolve()
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "user-refresh-regression"
KEY = USER_ID + "::" + ACCOUNT_ID
EMAIL = "refresh-regression@example.test"


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def token(exp):
    return encode({"alg": "none"}) + "." + encode({
        "email": EMAIL, "exp": exp,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": ACCOUNT_ID, "chatgpt_user_id": USER_ID,
            "chatgpt_plan_type": "plus",
        },
    }) + ".synthetic"


MOCK_CURL = r"""import json,os,sys,time
from pathlib import Path
root=Path(os.environ['REFRESH_TEST_ROOT']); mode=os.environ['REFRESH_TEST_MODE']
config=sys.stdin.read(); kind='oauth' if '/oauth/token' in config else 'usage' if '/wham/usage' in config else 'other'
old=(root/'old-token').read_text(); initial=kind=='usage' and old in config
with (root/'requests.jsonl').open('a') as output:
 output.write(json.dumps({'kind':kind,'initial':initial})+'\n')
def reply(value,status=200): print(json.dumps(value)+'\n'+str(status),end='')
if kind=='oauth':
 if mode in ('timeout','forced-timeout'):sys.exit(28)
 if mode=='concurrent':time.sleep(0.3)
 if mode=='persistence':os.chmod(Path(os.environ['CODEX_HOME'])/'accounts',0o500)
 reply(json.loads((root/'response.json').read_text()));sys.exit(0)
if kind=='usage':
 if mode in ('replay-failure','forced-timeout') and initial:reply({'error':{'code':'token_expired'}},401);sys.exit(0)
 if mode=='replay-failure':sys.exit(7)
 if mode=='concurrent' and initial:
  deadline=time.monotonic()+5
  while time.monotonic()<deadline:
   events=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
   if sum(event['kind']=='usage' and event['initial'] for event in events)>=2:break
   time.sleep(0.01)
  reply({'error':{'code':'token_expired'}},401);sys.exit(0)
 reply({'plan_type':'plus','rate_limit':{'primary_window':{'used_percent':10,'limit_window_seconds':18000,'reset_at':4102444800}}});sys.exit(0)
reply({'accounts':{}})
"""


@unittest.skipIf(os.name == "nt", "POSIX curl launcher and permission fault injection")
class RefreshCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-auth-refresh-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.accounts = self.home / "accounts"
        self.accounts.mkdir(parents=True)
        self.addCleanup(lambda: self.accounts.chmod(0o700))
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        curl = fake_bin / "curl"
        curl.write_text("#!" + sys.executable + "\n" + MOCK_CURL)
        curl.chmod(0o700)
        self.log = self.root / "requests.jsonl"
        self.log.write_text("")
        self.snapshot = self.accounts / (base64.urlsafe_b64encode(KEY.encode()).decode().rstrip("=") + ".auth.json")
        self.env = dict(os.environ, CODEX_HOME=str(self.home),
                        REFRESH_TEST_ROOT=str(self.root), REFRESH_TEST_MODE="success",
                        PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        self.seed()

    def seed(self, fresh=False):
        now = int(time.time())
        old_access = token(now + 86400 if fresh else now - 60)
        self.auth = {"auth_mode": "chatgpt", "tokens": {
            "access_token": old_access, "id_token": token(now + 86400),
            "refresh_token": "synthetic-old", "account_id": ACCOUNT_ID,
        }, "last_refresh": "2026-01-01T00:00:00Z"}
        self.snapshot.write_text(json.dumps(self.auth))
        (self.home / "auth.json").write_text(json.dumps(self.auth))
        (self.root / "old-token").write_text(old_access)
        (self.root / "response.json").write_text(json.dumps({
            "access_token": token(now + 864000), "refresh_token": "synthetic-rotated",
            "id_token": token(now + 864000),
        }))
        record = {"account_key": KEY, "chatgpt_account_id": ACCOUNT_ID,
                  "chatgpt_user_id": USER_ID, "email": EMAIL, "alias": "regression",
                  "account_name": None, "plan": "plus", "auth_mode": "chatgpt",
                  "created_at": now, "last_used_at": None, "last_usage": None,
                  "last_usage_at": None, "last_local_rollout": None}
        (self.accounts / "registry.json").write_text(json.dumps({
            "schema_version": 4, "active_account_key": KEY,
            "previous_active_account_key": None, "accounts": [record],
        }))

    def run_cli(self, *args):
        return subprocess.run([str(BINARY), *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def events(self, kind):
        return [event for line in self.log.read_text().splitlines()
                if (event := json.loads(line))["kind"] == kind]

    def test_successful_refresh_rotates_tokens_with_three_root_keys(self):
        result = self.run_cli("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events("oauth")), 1)
        for path in [self.snapshot, self.home / "auth.json"]:
            self.assertEqual(json.loads(path.read_text())["tokens"]["refresh_token"], "synthetic-rotated")
        self.assertFalse(list(self.accounts.glob("*.refresh-state.json")))

    def test_timeout_is_not_retried_by_a_later_invocation(self):
        self.env["REFRESH_TEST_MODE"] = "timeout"
        for _ in range(2):
            result = self.run_cli("list", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            refresh = json.loads(result.stdout)["accounts"][0]["usage"]["refresh"]
            self.assertEqual(refresh["error_code"], "RefreshUncertain")
        self.assertEqual(len(self.events("oauth")), 1)

    def test_replay_transport_failure_remains_an_error(self):
        self.seed(fresh=True)
        self.env["REFRESH_TEST_MODE"] = "replay-failure"
        result = self.run_cli("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        refresh = json.loads(result.stdout)["accounts"][0]["usage"]["refresh"]
        self.assertEqual(refresh["status"], "error")
        self.assertEqual(refresh["error_code"], "RequestFailed")
        self.assertEqual(len(self.events("oauth")), 1)

    def test_forced_refresh_timeout_preserves_its_error(self):
        self.seed(fresh=True)
        self.env["REFRESH_TEST_MODE"] = "forced-timeout"
        result = self.run_cli("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        refresh = json.loads(result.stdout)["accounts"][0]["usage"]["refresh"]
        self.assertEqual(refresh["error_code"], "RefreshUncertain")
        self.assertEqual(len(self.events("oauth")), 1)

    def test_partial_save_blocks_switch_and_preserves_recovery(self):
        self.env["REFRESH_TEST_MODE"] = "persistence"
        result = self.run_cli("switch", EMAIL, "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["tokens"]["refresh_token"], "synthetic-rotated")
        self.accounts.chmod(0o700)
        self.env["REFRESH_TEST_MODE"] = "success"
        recovered = self.run_cli("list", "--json")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(self.snapshot.read_text())["tokens"]["refresh_token"], "synthetic-rotated")
        self.assertEqual(len(self.events("oauth")), 1)
        self.assertFalse(list(self.accounts.glob("*.refresh-state.json")))

    def test_concurrent_expired_usage_replays_for_both_callers(self):
        self.seed(fresh=True)
        self.env["REFRESH_TEST_MODE"] = "concurrent"
        processes = [subprocess.Popen([str(BINARY), "list", "--json"], cwd=self.root,
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["accounts"][0]["usage"]["refresh"]["status"], "ok")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(len(self.events("oauth")), 1)


if __name__ == "__main__":
    unittest.main()
