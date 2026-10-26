"""Offline regression tests; no real credentials, user files or API calls."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'hooks/kimi_quota.sh'
NORMAL = {
    'limits': [{'window': {'duration': 300}, 'detail': {
        'used': 43, 'limit': 100, 'remaining': 57,
        'resetTime': '2026-10-26T03:29:00Z'}}],
    'usages': {
        'limit_month_total': {'used_ratio': .021, 'reset_time': '2026-11-01T00:00:00Z'},
        'limit_month_code': {'used_ratio': .019, 'reset_time': '2026-11-23T00:00:00Z'},
    },
}
LINE = '> Kimi 已用 · 5h **43%** · 月 **2.1%**｜重置 5h 11:29 · 月 11/01 08:00'
KEY = 'synthetic-regression-credential'
DEFAULT_URL = 'https://api.kimi.com/coding/v1/usages'


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

    def cache_scope(self, key=None, url=DEFAULT_URL):
        key = self.env.get('KIMI_API_KEY', 'synthetic-config-key') if key is None else key
        return hashlib.sha256((url + '\0' + key).encode()).hexdigest()

    def cache_file(self, key=None, url=DEFAULT_URL):
        path = self.home / '.cache/kimi-quota/usage-v2' / (self.cache_scope(key, url) + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def cache_data(self, line=LINE, ts=None):
        return {'line': line, 'ts': int(time.time()) if ts is None else ts,
                'format': 2, 'scope': self.cache_scope()}

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
        self.assertEqual(self.run_hook('stop', last_assistant_message='done')['decision'], 'block')

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
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
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
        cache = self.cache_file()
        cache.write_text(json.dumps(self.cache_data(ts=1)))
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
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('5h **36%**', text)
        self.assertIn('月 **2.1%**', text)
        self.assertIn('重置 5h —', text)

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
        cache = self.cache_file()
        for content in ['{broken', json.dumps(self.cache_data(ts='invalid')),
                        json.dumps(self.cache_data(ts='0008'))]:
            cache.write_text(content)
            self.start()
            self.assertIsNotNone(self.run_hook('prompt'))
            self.assertIsInstance(json.loads(cache.read_text())['ts'], int)

    def test_state_stays_private_and_contains_no_credential(self):
        self.start(sid='../session/path')
        self.run_hook('prompt', sid='../session/path')
        sessions = self.home / '.cache/kimi-quota/sessions-v3'
        for path in sessions.iterdir():
            self.assertTrue(path.is_file())
            self.assertNotIn(KEY, path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(list(sessions.iterdir())), 1)

    def test_five_hour_percentage_uses_denominator_and_real_counters(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail'].update(used='50', limit='200')
        response['usages']['limit_5h'] = {'used_ratio': 0}
        self.response.write_text(json.dumps(response))
        self.assertIn('5h **25%**', self.run_hook('query'))

    def test_month_total_percentage_and_reset_stay_in_same_pool(self):
        text = self.run_hook('query')
        self.assertIn('月 **2.1%**', text)
        self.assertIn('月 11/01 08:00', text)
        self.assertNotIn('11/23', text)

    def test_month_code_fallback_when_total_pool_absent(self):
        response = copy.deepcopy(NORMAL)
        response['usages'].pop('limit_month_total')
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('月 **1.9%**', text)
        self.assertIn('月 11/23 08:00', text)

    def test_missing_month_is_unknown_instead_of_zero(self):
        response = copy.deepcopy(NORMAL)
        response.pop('usages')
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('月 **—**', text)
        self.assertTrue(text.rstrip().endswith('月 —'))

    def test_month_total_without_ratio_does_not_borrow_code_pool(self):
        response = copy.deepcopy(NORMAL)
        response['usages']['limit_month_total'].pop('used_ratio')
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('月 **—**', text)
        self.assertTrue(text.rstrip().endswith('月 11/01 08:00'))

    def test_ratio_only_api_response_is_supported(self):
        response = copy.deepcopy(NORMAL)
        response.pop('limits')
        response['usages']['limit_5h'] = {'used_ratio': .25, 'reset_time': '2026-10-26T03:29:00Z'}
        self.response.write_text(json.dumps(response))
        self.assertIn('5h **25%**', self.run_hook('query'))

    def test_zero_limit_is_unknown_without_division_error(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['limit'] = 0
        self.response.write_text(json.dumps(response))
        self.assertIn('5h **—**', self.run_hook('query'))

    def test_reset_offsets_fractional_seconds_and_missing_times(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['resetTime'] = '2026-10-26T11:29:00.123456+08:00'
        response['usages']['limit_month_total']['reset_time'] = '2026-11-01T01:00:00+01:00'
        self.response.write_text(json.dumps(response))
        self.assertEqual(self.run_hook('query').strip(), LINE)
        response['usages']['limit_month_total'].pop('reset_time')
        self.response.write_text(json.dumps(response))
        self.assertTrue(self.run_hook('query').strip().endswith('月 —'))

    def test_five_hour_reset_snake_case_alias(self):
        response = copy.deepcopy(NORMAL)
        detail = response['limits'][0]['detail']
        detail['reset_time'] = detail.pop('resetTime')
        self.response.write_text(json.dumps(response))
        self.assertEqual(self.run_hook('query').strip(), LINE)

    def test_old_usage_and_session_formats_are_not_reused(self):
        folder = self.home / '.cache/kimi-quota'
        folder.mkdir(parents=True)
        (folder / 'usage-v2.json').write_text(json.dumps({
            'line': 'Kimi ⚡ 5小时 43/100 · 本月1.9%', 'ts': 9999999999}))
        self.start()
        sessions = folder / 'sessions-v3/session-one.json'
        state = json.loads(sessions.read_text())
        state.update(turn_id='turn-one', line='old footer', stop_requested=True)
        sessions.write_text(json.dumps(state))
        result = self.run_hook('stop', last_assistant_message='old footer')
        self.assertEqual(result['decision'], 'block')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(json.loads(self.cache_file().read_text())['format'], 2)

    def test_stop_accepts_quote_spacing_but_requires_quote_style(self):
        self.start()
        self.run_hook('prompt')
        alternate = LINE.replace('> ', '>').replace('**', '')
        self.assertIsNone(self.run_hook('stop', last_assistant_message=alternate))
        result = self.run_hook('stop', last_assistant_message=LINE[2:])
        self.assertEqual(result['decision'], 'block')

    def test_manual_query_is_one_compact_markdown_line(self):
        self.assertEqual(self.run_hook('query').splitlines(), [LINE])

    def test_legacy_writer_cannot_replace_new_format(self):
        self.start()
        self.run_hook('prompt')
        folder = self.home / '.cache/kimi-quota'
        (folder / 'usage.json').write_text(json.dumps({
            'line': 'legacy footer', 'format': 2, 'ts': 9999999999}))
        old_sessions = folder / 'sessions'
        old_sessions.mkdir()
        (old_sessions / 'session-one.json').write_text(json.dumps({
            'model': 'other/model', 'turn_id': 'turn-two',
            'line': 'legacy footer', 'line_format': 2, 'stop_requested': True}))
        result = self.run_hook('prompt', turn='turn-two')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 1)

    def test_manual_refresh_new_footer_is_not_replaced_by_old_snapshot(self):
        self.start()
        self.run_hook('prompt')
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['used'] = 80
        response['usages']['limit_month_total']['used_ratio'] = .9
        self.response.write_text(json.dumps(response))
        fresh = self.run_hook('query').strip()  # query 没有 env SID，也不读输入中的 ID。
        self.assertIn('80%', fresh)
        self.assertIsNone(self.run_hook('stop', last_assistant_message=fresh))
        self.assertEqual(len(self.requests()), 2)

    def test_other_session_refresh_does_not_force_a_new_footer(self):
        self.start()
        self.run_hook('prompt')
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response))
        self.run_hook('query', sid='other-session')
        self.assertIsNone(self.run_hook('stop', last_assistant_message=LINE))

    def refreshed_query(self, used=80):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['used'] = used
        self.response.write_text(json.dumps(response))
        return self.run_hook('query').strip()

    def test_manual_result_stays_valid_when_usage_cache_expires(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        self.cache_file().write_text(json.dumps(self.cache_data(line=fresh, ts=1)))
        self.assertIsNone(self.run_hook('stop', last_assistant_message=fresh))
        self.assertEqual(len(self.requests()), 2)

    def test_manual_result_stays_valid_after_another_query(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        later = self.refreshed_query(82)
        self.assertNotEqual(fresh, later)
        self.assertIsNone(self.run_hook('stop', last_assistant_message=fresh))

    def test_query_receipts_never_supply_expired_quota_for_a_new_turn(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        self.cache_file().write_text(json.dumps(self.cache_data(line=fresh, ts=1)))
        self.response.write_text('')
        self.assertIsNone(self.run_hook('prompt', turn='turn-two'))
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))

    def test_query_receipts_are_bounded_and_private(self):
        self.start()
        self.run_hook('prompt')
        receipt = self.home / '.cache/kimi-quota/query-v1' / (self.cache_scope() + '.json')
        receipt.parent.mkdir(parents=True)
        now = int(time.time())
        receipt.write_text(json.dumps({'format': 1, 'scope': self.cache_scope(), 'receipts':
            [{'line': 'old-' + str(i), 'ts': now - 90000} for i in range(3)] +
            [{'line': 'recent-' + str(i), 'ts': now} for i in range(40)]}))
        receipt.chmod(0o600)
        fresh = self.refreshed_query()
        entries = json.loads(receipt.read_text())['receipts']
        self.assertEqual(len(entries), 32)
        self.assertEqual(entries[-1]['line'], fresh)
        self.assertFalse(any(row['line'].startswith('old-') for row in entries))
        self.assertNotIn(KEY, receipt.read_text())
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_query_receipts_before_prompt_or_expired_are_rejected(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        self.refreshed_query(82)
        receipt = self.home / '.cache/kimi-quota/query-v1' / (self.cache_scope() + '.json')
        state_file = self.home / '.cache/kimi-quota/sessions-v3/session-one.json'
        original = json.loads(state_file.read_text())
        for timestamp, prompt_ts in [(int(time.time()) - 10, int(time.time())),
                                     (int(time.time()) - 90000, 1)]:
            with self.subTest(timestamp=timestamp):
                state = dict(original, prompt_ts=prompt_ts)
                state_file.write_text(json.dumps(state))
                receipt.write_text(json.dumps({'format': 1, 'scope': self.cache_scope(),
                    'receipts': [{'line': fresh, 'ts': timestamp}]}))
                result = self.run_hook('stop', last_assistant_message=fresh)
                self.assertEqual(result['decision'], 'block')
                self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])

    def test_query_receipts_from_another_scope_are_rejected(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        self.refreshed_query(82)
        receipt = self.home / '.cache/kimi-quota/query-v1' / (self.cache_scope() + '.json')
        content = json.loads(receipt.read_text())
        content['scope'] = 'wrong-account'
        receipt.write_text(json.dumps(content))
        result = self.run_hook('stop', last_assistant_message=fresh)
        self.assertEqual(result['decision'], 'block')

    def test_query_receipts_do_not_cross_credentials(self):
        self.start()
        self.run_hook('prompt')
        fresh = self.refreshed_query()
        self.env['KIMI_API_KEY'] = 'synthetic-second-account'
        other = self.refreshed_query(82)
        result = self.run_hook('stop', last_assistant_message=fresh)
        self.assertIn(other, result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn(fresh, result['hookSpecificOutput']['additionalContext'])

    def test_nan_values_never_become_null_percent(self):
        fields = [('detail', 'used'), ('detail', 'limit'), ('detail', 'remaining'),
                  ('limit_5h', 'used_ratio'), ('limit_month_total', 'used_ratio')]
        self.start()
        for pool, field in fields:
            with self.subTest(pool=pool, field=field):
                response = copy.deepcopy(NORMAL)
                if pool == 'detail':
                    response['limits'][0]['detail'][field] = 'NaN'
                else:
                    if pool == 'limit_5h':
                        response.pop('limits')
                    response['usages'][pool] = {field: 'NaN'}
                self.response.write_text(json.dumps(response))
                self.assertNotIn('null%', self.run_hook('query'))
                self.assertIsNone(self.run_hook('prompt'))

    def test_partial_five_hour_fields_preserve_valid_month_data(self):
        for missing in [('limit',), ('used', 'remaining'), ('used', 'remaining', 'limit')]:
            with self.subTest(missing=missing):
                response = copy.deepcopy(NORMAL)
                for name in missing:
                    response['limits'][0]['detail'].pop(name, None)
                self.response.write_text(json.dumps(response))
                text = self.run_hook('query')
                self.assertIn('5h **—**', text)
                self.assertIn('月 **2.1%**', text)

    def test_expired_cache_is_not_displayed_during_outage(self):
        self.start()
        self.run_hook('prompt')
        self.cache_file().write_text(json.dumps(self.cache_data(ts=1)))
        self.response.write_text('')
        self.assertIsNone(self.run_hook('prompt', turn='turn-two'))
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))

    def test_different_credentials_do_not_share_usage_or_turn_snapshot(self):
        self.start()
        self.run_hook('prompt')
        old_cache = self.cache_file()
        self.env['KIMI_API_KEY'] = 'synthetic-second-account'
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response))
        # 同一 turn 的旧会话快照也不能跨凭据复用。
        result = self.run_hook('stop', last_assistant_message=LINE)
        self.assertIn('80%', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('43%', result['hookSpecificOutput']['additionalContext'])
        self.assertNotEqual(self.cache_file(), old_cache)
        self.assertEqual(len(self.requests()), 2)

    def test_different_official_urls_do_not_share_cache(self):
        self.start()
        self.run_hook('prompt')
        self.env['KIMI_QUOTA_URL'] = 'https://api.kimi.ai/coding/v1/usages'
        self.start(sid='session-two')
        self.run_hook('prompt', sid='session-two')
        self.assertEqual(len(self.requests()), 2)

    def test_database_model_is_rechecked_at_stop_in_both_directions(self):
        self.start()
        self.db_model('moonshot-kimi/k3')
        self.run_hook('prompt', turn='turn-two')
        self.db_model('other/model')
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('prompt', turn='turn-three'))
        self.db_model('moonshot-kimi/k3')
        result = self.run_hook('stop', turn='turn-three', last_assistant_message='done')
        self.assertEqual(result['decision'], 'block')

    def test_hash_failure_never_reuses_old_credential_data(self):
        self.start()
        self.run_hook('prompt')
        old_cache = self.cache_file()
        self.env['KIMI_API_KEY'] = 'synthetic-second-account'
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response))
        fake = self.bin / 'shasum'
        for n, program in enumerate(['#!/bin/sh\nexit 1\n', '#!/bin/sh\nprintf bad-hash\n']):
            fake.write_text(program); fake.chmod(0o700)
            result = self.run_hook('prompt', turn='hash-failure-' + str(n))
            self.assertIn('80%', result['hookSpecificOutput']['additionalContext'])
            self.assertFalse(self.cache_file().exists())
            self.assertIn('43%', json.loads(old_cache.read_text())['line'])

    def test_cache_files_do_not_expose_raw_key(self):
        self.start()
        self.run_hook('prompt')
        for path in (self.home / '.cache/kimi-quota').rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY, path.name)
                self.assertNotIn(KEY, path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_previous_version_cannot_overwrite_scoped_turn_snapshot(self):
        self.start()
        self.run_hook('prompt')
        old_sessions = self.home / '.cache/kimi-quota/sessions-v2'
        old_sessions.mkdir()
        (old_sessions / 'session-one.json').write_text(json.dumps({
            'model': 'other/model', 'model_turn_id': 'turn-one',
            'line': 'old version footer', 'line_scope': self.cache_scope(),
            'line_format': 2, 'turn_id': 'turn-one', 'stop_requested': True}))
        self.assertIsNone(self.run_hook('stop', last_assistant_message=LINE))
        self.assertEqual(len(self.requests()), 1)


if __name__ == '__main__':
    unittest.main()
