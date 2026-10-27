"""Offline regression tests; no real credentials, user files or API calls."""
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
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
LINE = '`Kimi` 已用 · 5h 🟢 **43%** · 月 🟢 **2.1%** · 重置 5h 11:29 · 月 11/01 08:00'
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
        curl.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
from pathlib import Path
config = sys.stdin.read()
record = {'url': sys.argv[-1], 'key_in_argv': any(os.environ.get('KIMI_API_KEY', 'synthetic-config-key') in a for a in sys.argv[1:]), 'key_in_stdin': os.environ.get('KIMI_API_KEY', 'synthetic-config-key') in config}
with Path(os.environ['TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(record) + '\\n')
time.sleep(float(os.environ.get('TEST_DELAY', '0')))
sys.stdout.write(Path(os.environ['TEST_RESPONSE']).read_text())
''')
        curl.chmod(0o700)
        self.env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home),
                    'TZ': 'Asia/Shanghai', 'LANG': 'en_US.UTF-8',
                    'KIMI_API_KEY': KEY, 'TEST_CALLS': str(self.calls),
                    'TEST_RESPONSE': str(self.response)}

        self.now = int(time.time())
        self.env['TEST_NOW'] = str(self.now)
        date = self.bin / 'date'
        date.write_text('#!' + sys.executable + '\n' + "import os,sys\nif sys.argv[1:]==['+%s']: print(os.environ['TEST_NOW'])\nelse: os.execv('/bin/date',['date']+sys.argv[1:])\n")
        date.chmod(0o700)
        clock_stat = self.bin / 'stat'
        clock_stat.write_text('#!' + sys.executable + '\n' + "import os,sys,time\nif sys.argv[1:3]==['-f','%m']: print(int(os.stat(sys.argv[3]).st_mtime)+int(os.environ['TEST_NOW'])-int(time.time()))\nelse: os.execv('/usr/bin/stat',['stat']+sys.argv[1:])\n")
        clock_stat.chmod(0o700)


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


    def test_five_hour_percentage_uses_denominator_and_real_counters(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail'].update(used='50', limit='200')
        response['usages']['limit_5h'] = {'used_ratio': 0}
        self.response.write_text(json.dumps(response))
        self.assertIn('5h 🟢 **25%**', self.run_hook('query'))


    def test_month_total_percentage_and_reset_stay_in_same_pool(self):
        text = self.run_hook('query')
        self.assertIn('月 🟢 **2.1%**', text)
        self.assertIn('月 11/01 08:00', text)
        self.assertNotIn('11/23', text)


    def test_month_code_fallback_when_total_pool_absent(self):
        response = copy.deepcopy(NORMAL)
        response['usages'].pop('limit_month_total')
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('月 🟢 **1.9%**', text)
        self.assertIn('月 11/23 08:00', text)


    def test_missing_month_is_unknown_instead_of_zero(self):
        response = copy.deepcopy(NORMAL)
        response.pop('usages')
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('月 **—**', text)
        self.assertEqual(text.count('🟢'), 1)
        self.assertNotIn('🟢 **—**', text)
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
        self.assertIn('5h 🟢 **25%**', self.run_hook('query'))


    def test_zero_limit_is_unknown_without_division_error(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail']['limit'] = 0
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertIn('5h **—**', text)
        self.assertNotIn('5h 🟢', text)
        self.assertNotIn('5h 🔴', text)


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


    def test_manual_query_is_one_compact_markdown_line(self):
        self.assertEqual(self.run_hook('query').splitlines(), [LINE])


    def test_red_green_thresholds_use_real_ratio_for_each_window(self):
        cases = [(0, .8, '🟢 **0%**', '🟢 **80%**'),
                 (.8, .8001, '🟢 **80%**', '🔴 **>80%**'),
                 (.8001, .5, '🔴 **>80%**', '🟢 **50%**'),
                 (.79999, .81, '🟢 **80%**', '🔴 **81%**'),
                 (1, 1.2, '🔴 **100%**', '🔴 **120%**')]
        for ratio_only in [False, True]:
            for five, month, five_text, month_text in cases:
                with self.subTest(ratio_only=ratio_only, five=five, month=month):
                    response = copy.deepcopy(NORMAL)
                    if ratio_only:
                        response.pop('limits')
                        response['usages']['limit_5h'] = {'used_ratio': five}
                    else:
                        response['limits'][0]['detail'].update(used=str(five * 100), limit='100')
                    response['usages']['limit_month_total']['used_ratio'] = month
                    self.response.write_text(json.dumps(response))
                    text = self.run_hook('query').strip()
                    self.assertTrue(text.startswith('`Kimi` 已用 · '))
                    self.assertIn('5h ' + five_text, text)
                    self.assertIn('月 ' + month_text, text)
                    self.assertNotIn('🟡', text)
                    self.assertNotIn('｜', text)
                    self.assertEqual(len(text.splitlines()), 1)


    def state_file(self, sid='session-one', turn='turn-one'):
        from urllib.parse import quote
        return self.home / '.cache/kimi-quota/turns-v6' / quote(sid, safe='~') / (quote(turn, safe='~') + '.json')

    def patch_state(self, sid='session-one', **fields):
        path = self.state_file(sid)
        state = json.loads(path.read_text())
        state.update(fields)
        path.write_text(json.dumps(state))

    def advance(self, seconds):
        self.now += seconds
        self.env['TEST_NOW'] = str(self.now)

    def begin(self, **fields):
        self.start(**fields)
        return self.run_hook('prompt', **{k: v for k, v in fields.items() if k in ['sid', 'turn']})

    def wait_for_request(self, count=1):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if len(self.requests()) >= count:
                return
            time.sleep(.01)
        self.fail('模拟接口没有在预期时间内被调用')

    def test_syntax_and_hook_contract(self):
        subprocess.run(['/bin/bash', '-n', str(SCRIPT)], check=True)
        hooks = json.loads((SCRIPT.parent / 'hooks.json').read_text())['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure', 'Stop'})
        for event in ['PostToolUse', 'PostToolUseFailure']:
            command = hooks[event][0]['hooks'][0]
            self.assertTrue(command['command'].endswith(' progress'))
            self.assertFalse(command.get('async', False))
        self.assertGreaterEqual(hooks['Stop'][0]['hooks'][0]['timeoutMs'], 6500)

    def test_start_and_prompt_only_initialize_rules_without_requests(self):
        result = self.begin()
        self.assertIn('不要自行生成或复用历史额度', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.requests(), [])
        state = json.loads(self.state_file().read_text())
        self.assertEqual(state['started_at'], self.now)
        self.assertFalse(state['stop_checked'])

    def test_prompt_rules_do_not_require_a_key(self):
        self.env.pop('KIMI_API_KEY')
        self.assertIsNotNone(self.begin())
        self.assertEqual(self.requests(), [])
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertTrue(json.loads(self.state_file().read_text())['stop_checked'])

    def test_same_turn_prompt_is_idempotent(self):
        self.begin()
        started = self.now
        self.advance(500)
        self.run_hook('prompt')
        self.assertEqual(json.loads(self.state_file().read_text())['started_at'], started)
        self.advance(100)
        self.assertIsNotNone(self.run_hook('progress'))
        self.run_hook('stop', last_assistant_message=LINE)
        self.run_hook('prompt')
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 2)

    def test_progress_first_due_at_600_seconds_and_then_every_600(self):
        self.begin()
        for elapsed in [0, 300, 299]:
            self.advance(elapsed)
            self.assertIsNone(self.run_hook('progress'))
        self.assertEqual(self.requests(), [])
        self.advance(1)
        result = self.run_hook('progress')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertIn('继续执行当前任务', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision', result)
        self.advance(599)
        self.assertIsNone(self.run_hook('progress'))
        self.advance(1)
        self.assertIsNotNone(self.run_hook('progress'))
        self.assertEqual(len(self.requests()), 2)

    def test_progress_interval_override_and_invalid_defaults(self):
        for index, setting in enumerate(['300', '00300', '0', '-1', 'garbage', '999999999999']):
            with self.subTest(setting=setting):
                sid = 'interval-' + str(index)
                self.env['KIMI_QUOTA_PROGRESS_INTERVAL'] = setting
                self.begin(sid=sid)
                self.advance(300)
                result = self.run_hook('progress', sid=sid)
                if setting in ['300', '00300']:
                    self.assertIsNotNone(result)
                else:
                    self.assertIsNone(result)
                    self.advance(300)
                    self.assertIsNotNone(self.run_hook('progress', sid=sid))

    def test_failure_event_and_legacy_alias_use_progress_throttle(self):
        self.begin(); self.advance(600)
        result = self.run_hook('progress', hook_event_name='PostToolUseFailure')
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PostToolUseFailure')
        self.assertIsNone(self.run_hook('hook'))
        self.advance(600)
        result = self.run_hook('hook', hookEventName='PostToolUse')
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertEqual(len(self.requests()), 2)

    def test_progress_rejects_wrong_event_and_missing_ids(self):
        self.begin(); self.advance(600)
        self.assertIsNone(self.run_hook('progress', hook_event_name='Stop'))
        self.assertIsNone(self.run_hook('progress', sid=None))
        self.assertIsNone(self.run_hook('progress', turn=None))
        self.assertIsNone(self.run_hook('progress', sid='uninitialized'))
        self.assertEqual(self.requests(), [])

    def test_progress_failure_has_short_retry_and_does_not_block_final(self):
        self.begin(); self.advance(600)
        self.response.write_text('')
        self.assertIsNone(self.run_hook('progress'))
        for _ in range(3):
            self.advance(1)
            self.assertIsNone(self.run_hook('progress'))
        self.assertEqual(len(self.requests()), 1)
        self.advance(57)
        self.response.write_text(json.dumps(NORMAL))
        self.assertIsNotNone(self.run_hook('progress'))
        self.assertEqual(len(self.requests()), 2)
        self.assertIsNotNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 3)

    def test_final_ignores_progress_failure_retry_delay(self):
        self.begin(); self.advance(600)
        self.response.write_text('')
        self.run_hook('progress')
        self.response.write_text(json.dumps(NORMAL))
        result = self.run_hook('stop', last_assistant_message='done')
        self.assertIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 2)

    def test_clock_rollback_restarts_progress_interval(self):
        self.begin(); self.advance(-60)
        self.assertIsNone(self.run_hook('progress'))
        self.advance(599)
        self.assertIsNone(self.run_hook('progress'))
        self.advance(1)
        self.assertIsNotNone(self.run_hook('progress'))

    def test_progress_requires_known_start_time(self):
        self.begin(); self.patch_state(started_at='invalid')
        self.advance(3600)
        self.assertIsNone(self.run_hook('progress'))
        self.assertEqual(self.requests(), [])

    def test_long_task_final_refreshes_8_to_80(self):
        response = copy.deepcopy(NORMAL)
        response['limits'][0]['detail'].update(used=8, remaining=92)
        self.response.write_text(json.dumps(response))
        old_line = self.run_hook('query').strip()
        self.begin(); self.advance(3600)
        response['limits'][0]['detail'].update(used=80, remaining=20)
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', last_assistant_message='done\n\n' + old_line)
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('80%', text)
        self.assertNotIn('**8%**', text)
        self.assertEqual(len(self.requests()), 2)

    def test_final_always_refreshes_after_recent_progress(self):
        self.begin(); self.advance(600)
        self.run_hook('progress')
        response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = 90
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', last_assistant_message=LINE)
        self.assertIn('🔴 **90%**', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 2)

    def test_final_match_still_fetches_once_and_marks_checked(self):
        self.begin()
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done\n\n' + LINE + '\n\n '))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 1)

    def test_final_failure_is_terminal_and_never_reuses_old_files(self):
        self.begin()
        folder = self.home / '.cache/kimi-quota'
        for name in ['usage-v3/old.json', 'query-v2/old.json']:
            path = folder / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'line': LINE, 'ts': self.now, 'format': 3}))
        self.response.write_text('')
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.response.write_text(json.dumps(NORMAL))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.advance(600)
        self.assertIsNone(self.run_hook('progress'))
        self.assertEqual(len(self.requests()), 1)

    def test_final_requests_complete_answer_once_without_repeating_task(self):
        self.begin()
        result = self.run_hook('stop', last_assistant_message='你好，有什么需要我帮你处理的吗？')
        self.assertEqual(result['decision'], 'block')
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('先完整复现本轮已经完成的答案正文', text)
        self.assertIn('不能只回复额度', text)
        self.assertIn('完整保留已完成的答案正文', result['reason'])
        self.assertIn('不要再次调用工具、修改文件或重新执行任务', text)
        self.assertIn(LINE, text)
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message=LINE, stop_hook_active=True))
        self.assertEqual(len(self.requests()), 1)

    def test_initial_rules_require_body_and_quota_in_one_final_answer(self):
        for result in [self.start(), self.run_hook('prompt')]:
            text = result['hookSpecificOutput']['additionalContext']
            self.assertIn('完整正文和最新额度', text)
            self.assertIn('不能只回复额度', text)
            self.assertNotIn('只补充提供的状态行', text)
        self.assertEqual(self.requests(), [])

    def test_final_preserves_rich_answer_without_copying_it_into_bounded_context(self):
        # ZCode caps lifecycle Hook context at 24,000 characters. Refer to the
        # existing answer in history so code/links and long answers aren't cut off.
        body = '# 结果\n\n[文件](https://example.com/result)\n\n```python\nprint("done")\n```\n\n'
        body += '- 已验证结果。\n' * 4000
        self.assertGreater(len(body), 24000)
        for index, field in enumerate(['last_assistant_message', 'responseText', 'responsePreview']):
            turn = 'full-answer-' + str(index)
            self.begin(turn=turn)
            result = self.run_hook('stop', turn=turn, **{field: body})
            text = result['hookSpecificOutput']['additionalContext']
            self.assertLess(len(text), 4000)
            self.assertIn('从对话历史中保留本轮完整答案', text)
            self.assertIn('正文、代码块、列表、链接及原有格式', text)
            self.assertIn('只替换答案末尾已有的旧额度状态行', text)
            self.assertNotIn('只输出下面这一行', text)
            self.assertIn(LINE, text)

    def test_quota_only_previous_response_requires_recovering_body_from_history(self):
        self.begin()
        old_line = LINE.replace('43%', '8%')
        result = self.run_hook('stop', last_assistant_message=old_line)
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('若上一条仅有额度，向前找到本轮已完成的答案正文', text)
        self.assertIn(LINE, text)
        self.assertNotIn(old_line, text)
        self.assertIsNone(self.run_hook('stop', stop_hook_active=True))
        self.assertEqual(len(self.requests()), 1)

    def test_active_aliases_skip_without_any_request(self):
        self.begin()
        for fields in [{'stop_hook_active': True}, {'stopHookActive': True},
                       {'stop_hook_active': False, 'stopHookActive': True}]:
            self.assertIsNone(self.run_hook('stop', **fields))
        self.assertEqual(self.requests(), [])

    def test_manual_query_and_final_use_current_response_instead_of_receipts(self):
        self.begin()
        response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response)); manual = self.run_hook('query').strip()
        response['limits'][0]['detail']['used'] = 82
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', last_assistant_message=manual)
        self.assertIn('82%', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('80%', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 2)

    def test_final_checks_selected_style_and_color(self):
        for index, text in enumerate(['> ' + LINE, LINE.replace('🟢', '🔴', 1), LINE.replace('`Kimi`', 'Kimi')]):
            turn = 'style-' + str(index)
            self.begin(turn=turn)
            self.assertEqual(self.run_hook('stop', turn=turn, last_assistant_message=text)['decision'], 'block')
        self.begin(turn='style-bold')
        self.assertIsNone(self.run_hook('stop', turn='style-bold', last_assistant_message=LINE.replace('**', '')))

    def test_new_turn_and_new_session_have_independent_intervals(self):
        self.begin(); self.advance(600); self.run_hook('progress')
        self.run_hook('stop', last_assistant_message=LINE)
        self.run_hook('prompt', turn='turn-two')
        self.assertIsNone(self.run_hook('progress', turn='turn-two'))
        self.begin(sid='session-two'); self.advance(600)
        self.assertIsNotNone(self.run_hook('progress', turn='turn-two'))
        self.assertIsNotNone(self.run_hook('progress', sid='session-two'))
        self.assertEqual(len(self.requests()), 4)

    def test_late_turn_events_do_not_overwrite_current_state(self):
        self.begin(); self.advance(600)
        self.run_hook('prompt', turn='turn-two')
        before = self.state_file(turn='turn-two').read_bytes()
        self.assertIsNone(self.run_hook('progress', turn='turn-one'))
        self.assertIsNone(self.run_hook('stop', turn='turn-one', last_assistant_message='done'))
        self.assertEqual(self.state_file(turn='turn-two').read_bytes(), before)
        self.assertEqual(self.requests(), [])

    def test_internal_turn_without_prompt_initializes_its_own_progress_clock(self):
        self.begin()
        self.run_hook('stop', last_assistant_message=LINE)
        self.advance(300)
        self.assertIsNone(self.run_hook('progress', turn='internal'))
        self.assertEqual(json.loads(self.state_file(turn='internal').read_text())['started_at'], self.now)
        self.advance(599)
        self.assertIsNone(self.run_hook('progress', turn='internal'))
        self.advance(1)
        response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response))
        result = self.run_hook('progress', turn='internal')
        self.assertIn('80%', result['hookSpecificOutput']['additionalContext'])
        response['limits'][0]['detail']['used'] = 82
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', turn='internal', last_assistant_message='done')
        self.assertIn('82%', result['hookSpecificOutput']['additionalContext'])
        self.assertIsNone(self.run_hook('stop', turn='internal', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 3)

    def test_internal_turn_without_prompt_or_tools_still_refreshes_at_stop(self):
        self.begin()
        self.run_hook('stop', last_assistant_message=LINE)
        response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = 80
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', turn='internal-stop', last_assistant_message=LINE)
        self.assertIn('80%', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(self.requests()), 2)

    def test_internal_turn_does_not_replace_parent_model_authority_or_clock(self):
        self.db_model('other/model')
        self.begin()
        before = self.state_file().read_bytes()
        self.advance(600)
        self.assertIsNone(self.run_hook('progress', turn='internal-other', model='other/model'))
        self.assertEqual(self.state_file().read_bytes(), before)
        self.assertIsNotNone(self.run_hook('progress'))
        self.assertIsNotNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 2)

    def test_new_user_prompt_invalidates_known_parent_and_internal_turns(self):
        self.begin()
        self.run_hook('progress', turn='internal')
        self.run_hook('prompt', turn='new-user')
        before = self.state_file(turn='new-user').read_bytes()
        self.advance(3600)
        for turn in ['turn-one', 'internal']:
            self.assertIsNone(self.run_hook('progress', turn=turn))
            self.assertIsNone(self.run_hook('stop', turn=turn, last_assistant_message='done'))
        self.assertEqual(self.state_file(turn='new-user').read_bytes(), before)
        self.assertEqual(self.requests(), [])
        self.assertIsNotNone(self.run_hook('stop', turn='new-user', last_assistant_message='done'))

    def test_host_model_wins_first_turn_and_database_rechecks_later(self):
        self.db_model('other/model'); self.begin()
        self.assertIsNotNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('prompt', turn='turn-two'))
        self.db_model('moonshot-kimi/k3')
        self.assertIsNotNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 2)

    def test_non_kimi_host_model_and_unknown_never_query(self):
        self.db_model('moonshot-kimi/k3'); self.begin(model='other/model')
        self.advance(600)
        self.assertIsNone(self.run_hook('progress'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('prompt', sid='unknown'))
        self.assertIsNone(self.run_hook('stop', sid='unknown', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])

    def test_database_switch_to_non_kimi_skips_progress_and_stop(self):
        self.begin(); self.run_hook('prompt', turn='turn-two')
        self.db_model('other/model'); self.advance(600)
        self.assertIsNone(self.run_hook('progress', turn='turn-two'))
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])

    def test_alias_ids_and_empty_turn_do_not_poison_future_turns(self):
        self.run_hook('sessionstart', sid=None, turn=None, sessionId='aliases', turn_id='first', model='moonshot-kimi/k3')
        self.run_hook('prompt', sid=None, turn=None, sessionId='aliases', turn_id='first')
        self.assertIsNotNone(self.run_hook('stop', sid=None, turn=None, sessionId='aliases', turn_id='first'))
        self.env['ZCODE_SESSION_ID'] = 'env-session'
        self.start(sid=None, turn=None)
        self.run_hook('prompt', sid=None, turn=None)
        self.run_hook('stop', sid=None, turn=None)
        self.run_hook('prompt', sid=None, turn='later')
        self.assertIsNotNone(self.run_hook('stop', sid=None, turn='later'))

    def test_concurrent_progress_emits_and_queries_only_once(self):
        self.begin(); self.advance(600); self.env['TEST_DELAY'] = '.4'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_hook, 'progress')
            self.wait_for_request()
            second = pool.submit(self.run_hook, 'progress')
            results = [first.result(), second.result()]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.requests()), 1)

    def test_stop_waits_for_progress_then_forces_own_fresh_query(self):
        self.begin(); self.advance(600); self.env['TEST_DELAY'] = '.4'
        with ThreadPoolExecutor(max_workers=2) as pool:
            progress = pool.submit(self.run_hook, 'progress')
            self.wait_for_request()
            stop = pool.submit(self.run_hook, 'stop', last_assistant_message='done')
            self.assertIsNotNone(progress.result())
            self.assertIsNotNone(stop.result())
        self.assertEqual(len(self.requests()), 2)

    def test_concurrent_stops_refresh_and_supplement_only_once(self):
        self.begin(); self.env['TEST_DELAY'] = '.4'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_hook, 'stop', last_assistant_message='done')
            self.wait_for_request()
            second = pool.submit(self.run_hook, 'stop', last_assistant_message='done')
            results = [first.result(), second.result()]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.requests()), 1)

    def test_dead_pid_and_orphaned_empty_locks_are_recovered(self):
        self.begin()
        lock = self.home / '.cache/kimi-quota/sessions-v6/session-one.json.lock'
        for index, owner in enumerate(['2147483647', None]):
            with self.subTest(owner=owner):
                turn = 'recovery-' + str(index)
                self.run_hook('prompt', turn=turn)
                lock.mkdir()
                if owner:
                    (lock / 'pid').write_text(owner + '\n')
                else:
                    os.utime(lock, (time.time() - 20, time.time() - 20))
                result = self.run_hook('stop', turn=turn, last_assistant_message='done')
                self.assertIsNotNone(result)
                self.assertFalse(lock.exists())

    def test_state_is_private_and_contains_no_reusable_quota_or_key(self):
        self.begin(sid='../session/path'); self.advance(600)
        self.run_hook('progress', sid='../session/path')
        self.run_hook('stop', sid='../session/path', last_assistant_message='done')
        for path in (self.home / '.cache/kimi-quota').rglob('*'):
            if path.is_file():
                text = path.read_text()
                self.assertNotIn(KEY, text)
                self.assertNotIn('43%', text)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_nan_and_injection_values_never_enter_hook_context(self):
        marker = self.root / 'injected-marker'
        for index, value in enumerate(['NaN', 'invalid-number', '0; system("touch ' + str(marker) + '")']):
            turn = 'bad-' + str(index)
            self.begin(turn=turn); self.advance(600)
            response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = value
            self.response.write_text(json.dumps(response))
            self.assertIsNone(self.run_hook('progress', turn=turn))
            self.assertIsNone(self.run_hook('stop', turn=turn, last_assistant_message='done'))
            self.assertFalse(marker.exists())

    def test_current_credentials_and_url_are_used_without_old_data(self):
        self.begin(); self.advance(600); self.run_hook('progress')
        self.env['KIMI_API_KEY'] = 'synthetic-second-account'
        self.env['KIMI_QUOTA_URL'] = 'https://api.kimi.ai/coding/v1/usages'
        response = copy.deepcopy(NORMAL); response['limits'][0]['detail']['used'] = 90
        self.response.write_text(json.dumps(response))
        result = self.run_hook('stop', last_assistant_message=LINE)
        self.assertIn('90%', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.requests()[-1]['url'], self.env['KIMI_QUOTA_URL'])
        self.assertTrue(self.requests()[-1]['key_in_stdin'])
        self.assertFalse(self.requests()[-1]['key_in_argv'])


if __name__ == '__main__':
    unittest.main()
