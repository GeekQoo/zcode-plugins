"""Offline regression tests; no real credentials, user files or API calls."""
import copy
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'hooks/kimi_quota.sh'
NORMAL = {
    'limits': [{'window': {'duration': 300}, 'detail': {
        'used': 43, 'limit': 100, 'remaining': 57,
        'resetTime': '2026-10-26T03:29:00Z'}}],
    'usages': {'limit_month_code': {'used_ratio': .019}},
}
LINE = 'Kimi ⚡ 5小时 43/100 · 11:29重置 · 本月1.9%'
KEY = 'synthetic-regression-credential'


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kimi-quota-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.response = self.root / 'response.json'
        self.response.write_text(json.dumps(NORMAL))
        self.calls = self.root / 'calls.jsonl'
        curl = self.bin / 'curl'
        curl.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
config = sys.stdin.read()
record = {'url': sys.argv[-1], 'key_in_argv': any(os.environ.get('KIMI_API_KEY', 'synthetic-config-key') in a for a in sys.argv[1:]), 'key_in_stdin': os.environ.get('KIMI_API_KEY', 'synthetic-config-key') in config}
with Path(os.environ['TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(record) + '\\n')
sys.stdout.write(Path(os.environ['TEST_RESPONSE']).read_text())
''')
        curl.chmod(0o700)
        self.env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home),
                    'TZ': 'Asia/Shanghai', 'LANG': 'en_US.UTF-8',
                    'KIMI_API_KEY': KEY, 'TEST_CALLS': str(self.calls),
                    'TEST_RESPONSE': str(self.response)}

    def run_hook(self, mode, sid='session-one', turn='turn-one', **fields):
        payload = {'session_id': sid, 'turnId': turn, **fields}
        process = subprocess.run(['/bin/bash', str(SCRIPT), mode],
            input=json.dumps(payload) + '\n', env=self.env, text=True,
            errors='replace', capture_output=True, timeout=8, umask=0o022)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, '')
        if mode == 'query':
            return process.stdout
        return json.loads(process.stdout) if process.stdout.strip() else None

    def requests(self):
        return [json.loads(row) for row in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def db_model(self, model, sid='session-one'):
        folder = self.home / '.zcode/v2'
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(folder / 'tasks-index.sqlite') as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, model TEXT)')
            db.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)', (sid, model))

    def start(self, sid='session-one', turn='turn-one', model='moonshot-kimi/k3'):
        return self.run_hook('sessionstart', sid=sid, turn=turn, model=model)

    def test_syntax_and_hook_contract(self):
        subprocess.run(['/bin/bash', '-n', str(SCRIPT)], check=True)
        hooks = json.loads((SCRIPT.parent / 'hooks.json').read_text())['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'UserPromptSubmit', 'Stop'})
        self.assertIn(' stop', hooks['Stop'][0]['hooks'][0]['command'])

    def test_first_turn_uses_host_model_without_database(self):
        start = self.start()
        self.assertEqual(start['hookSpecificOutput']['hookEventName'], 'SessionStart')
        self.assertEqual(self.requests(), [])
        prompt = self.run_hook('prompt')
        self.assertIn(LINE, prompt['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse((self.home / '.zcode/v2/tasks-index.sqlite').exists())

    def test_first_turn_host_model_overrides_stale_database(self):
        self.db_model('other/model')
        self.start()
        self.assertIsNotNone(self.run_hook('prompt'))

    def test_next_turn_database_can_switch_to_non_kimi(self):
        self.start()
        self.db_model('other/model')
        self.assertIsNone(self.run_hook('prompt', turn='turn-two'))
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])

    def test_non_kimi_and_unknown_never_query(self):
        self.assertIsNone(self.start(model='other/model'))
        self.assertIsNone(self.run_hook('prompt'))
        self.assertIsNone(self.run_hook('prompt', sid='unknown'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])

    def test_non_kimi_host_model_overrides_old_kimi_database(self):
        self.db_model('moonshot-kimi/k3')
        self.start(model='other/model')
        self.assertIsNone(self.run_hook('prompt'))
        self.assertEqual(self.requests(), [])

    def test_database_fallback_when_no_host_model(self):
        self.db_model('moonshot-kimi/k3')
        self.assertIsNotNone(self.run_hook('prompt'))

    def test_same_state_shown_every_turn_with_one_api_request(self):
        self.start()
        for n in range(3):
            result = self.run_hook('prompt', turn='turn-' + str(n))
            self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 1)

    def test_sessions_do_not_suppress_each_other(self):
        for sid in ['session-one', 'session-two']:
            self.start(sid=sid)
            self.assertIsNotNone(self.run_hook('prompt', sid=sid))
        self.assertEqual(len(self.requests()), 1)

    def test_stop_requests_one_followup_and_never_loops(self):
        self.start()
        self.run_hook('prompt')
        result = self.run_hook('stop', last_assistant_message='done')
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'Stop')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertIn('只输出下面这一行', result['hookSpecificOutput']['additionalContext'])
        self.assertIn('不要重写', result['hookSpecificOutput']['additionalContext'])
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done', stop_hook_active=True))
        self.assertEqual(len(self.requests()), 1)

    def test_stop_active_aliases_skip_without_query(self):
        self.start()
        for fields in [{'stop_hook_active': True}, {'stopHookActive': True},
                       {'stop_hook_active': False, 'stopHookActive': True}]:
            self.assertIsNone(self.run_hook('stop', **fields))
        self.assertEqual(self.requests(), [])

    def test_complete_footer_allows_end_with_blank_lines(self):
        self.start()
        self.run_hook('prompt')
        for text in ['done\n' + LINE, 'done\n' + LINE + '\r\n\n ', LINE + '  \n']:
            self.assertIsNone(self.run_hook('stop', last_assistant_message=text))

    def test_stop_reuses_turn_line_when_usage_cache_expires(self):
        self.start()
        self.run_hook('prompt')
        cache = self.home / '.cache/kimi-quota/usage.json'
        cache.write_text(json.dumps({'line': LINE, 'ts': 1}))
        changed = copy.deepcopy(NORMAL)
        changed['limits'][0]['detail']['used'] = 60
        self.response.write_text(json.dumps(changed))
        self.assertIsNone(self.run_hook('stop', last_assistant_message=LINE))
        self.assertEqual(len(self.requests()), 1)

    def test_stop_can_recover_missing_prompt_context(self):
        self.start()
        result = self.run_hook('stop', last_assistant_message='done')
        self.assertEqual(result['decision'], 'block')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])

    def test_next_turn_has_independent_stop_guard(self):
        self.start()
        self.run_hook('prompt')
        self.run_hook('stop', last_assistant_message='done')
        self.run_hook('prompt', turn='turn-two')
        result = self.run_hook('stop', turn='turn-two', last_assistant_message='next')
        self.assertEqual(result['decision'], 'block')

    def test_camel_case_ids_and_env_fallback(self):
        start = self.run_hook('sessionstart', sid=None, turn=None, sessionId='alias-session', turn_id='alias-turn', model='moonshot-kimi/k3')
        self.assertIsNotNone(start)
        self.assertIsNotNone(self.run_hook('prompt', sid=None, turn=None, sessionId='alias-session', turn_id='alias-turn'))
        self.env['ZCODE_SESSION_ID'] = 'env-session'
        self.start(sid=None)
        self.assertIsNotNone(self.run_hook('prompt', sid=None))

    def test_query_does_not_wait_for_stdin_eof(self):
        process = subprocess.Popen(['/bin/bash', str(SCRIPT), 'query'], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.wait(timeout=4)
            self.assertEqual(process.returncode, 0)
            self.assertIn(LINE, process.stdout.read().decode())
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            process.stdin.close(); process.stdout.close(); process.stderr.close()

    def test_query_missing_key_and_empty_response_have_controlled_error(self):
        self.env.pop('KIMI_API_KEY')
        self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(self.requests(), [])
        self.env['KIMI_API_KEY'] = KEY
        self.response.write_text('')
        self.assertIn('查询失败', self.run_hook('query'))

    def test_url_allowlist_and_no_key_in_argv(self):
        self.env['KIMI_QUOTA_URL'] = 'http://recipient.invalid/usages'
        self.run_hook('query')
        call = self.requests()[0]
        self.assertEqual(call['url'], 'https://api.kimi.com/coding/v1/usages')
        self.assertFalse(call['key_in_argv'])
        self.assertTrue(call['key_in_stdin'])

    def test_parse_failure_hides_payload_and_restricts_existing_log(self):
        folder = self.home / '.cache/kimi-quota'
        folder.mkdir(parents=True)
        log = folder / 'last-error.log'
        log.write_text('old'); log.chmod(0o644)
        self.response.write_text('{"account_id":"SYNTHETIC-ACCOUNT"}')
        result = subprocess.run(['/bin/bash', str(SCRIPT), 'query'], env=self.env,
            text=True, capture_output=True, timeout=4)
        self.assertNotIn('SYNTHETIC-ACCOUNT', result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_missing_used_and_reset_keep_fields_aligned(self):
        response = copy.deepcopy(NORMAL)
        detail = response['limits'][0]['detail']
        detail.pop('used'); detail.pop('resetTime'); detail['remaining'] = 64
        self.response.write_text(json.dumps(response))
        self.start()
        result = self.run_hook('prompt')
        self.assertIn('36/100 · 本月1.9%', result['hookSpecificOutput']['additionalContext'])

    def test_invalid_numbers_produce_no_hook_context(self):
        self.start()
        for field in ['used', 'limit']:
            response = copy.deepcopy(NORMAL)
            response['limits'][0]['detail'][field] = 'invalid-number'
            self.response.write_text(json.dumps(response))
            self.assertIsNone(self.run_hook('prompt'))
            self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))

    def test_corrupt_or_nonnumeric_cache_recovers(self):
        folder = self.home / '.cache/kimi-quota'
        folder.mkdir(parents=True)
        cache = folder / 'usage.json'
        for content in ['{broken', json.dumps({'line': LINE, 'ts': 'invalid'}),
                        json.dumps({'line': LINE, 'ts': '0008'})]:
            cache.write_text(content)
            self.start()
            self.assertIsNotNone(self.run_hook('prompt'))
            self.assertIsInstance(json.loads(cache.read_text())['ts'], int)

    def test_state_stays_private_and_contains_no_credential(self):
        self.start(sid='../session/path')
        self.run_hook('prompt', sid='../session/path')
        sessions = self.home / '.cache/kimi-quota/sessions'
        for path in sessions.iterdir():
            self.assertTrue(path.is_file())
            self.assertNotIn(KEY, path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(list(sessions.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
