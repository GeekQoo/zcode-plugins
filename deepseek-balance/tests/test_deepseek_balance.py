"""Offline DeepSeek balance regression tests; synthetic credentials and no network."""
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

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks/deepseek_balance.sh'
NORMAL = {'is_available': True, 'balance_infos': [{
    'currency': 'CNY', 'total_balance': '110.00',
    'granted_balance': '10.00', 'topped_up_balance': '100.00'}]}
LINE = '`DeepSeek` 余额 🟢 **¥110.00** · 充值 ¥100.00 · 赠金 ¥10.00'
KEY = 'synthetic-regression-credential'
DEFAULT_URL = 'https://api.deepseek.com/user/balance'

class BalanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='deepseek-balance-test-')
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
record = {'url': sys.argv[-1], 'key_in_argv': any(os.environ.get('DEEPSEEK_API_KEY', 'synthetic-config-key') in a for a in sys.argv[1:]), 'key_in_stdin': os.environ.get('DEEPSEEK_API_KEY', 'synthetic-config-key') in config, 'args': sys.argv[1:]}
with Path(os.environ['TEST_CALLS']).open('a') as stream:
    stream.write(json.dumps(record) + '\\n')
time.sleep(float(os.environ.get('TEST_DELAY', '0')))
sys.stdout.write(Path(os.environ['TEST_RESPONSE']).read_text())
sys.exit(int(os.environ.get('TEST_HTTP_EXIT', '0')))
''')
        curl.chmod(0o700)
        self.env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home),
                    'TZ': 'Asia/Shanghai', 'LANG': 'en_US.UTF-8',
                    'DEEPSEEK_API_KEY': KEY, 'TEST_CALLS': str(self.calls),
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


    def start(self, sid='session-one', turn='turn-one', model='deepseek/deepseek-chat'):
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
        self.env.pop('DEEPSEEK_API_KEY')
        self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(self.requests(), [])
        self.env['DEEPSEEK_API_KEY'] = KEY
        self.response.write_text('')
        self.assertIn('查询失败', self.run_hook('query'))


    def state_file(self, sid='session-one', turn='turn-one'):
        from urllib.parse import quote
        return self.home / '.cache/deepseek-balance/turns-v1' / quote(sid, safe='~') / (quote(turn, safe='~') + '.json')


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
        self.assertIn('不要自行生成或复用历史余额', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn(LINE, result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.requests(), [])
        state = json.loads(self.state_file().read_text())
        self.assertEqual(state['started_at'], self.now)
        self.assertFalse(state['stop_checked'])


    def test_prompt_rules_do_not_require_a_key(self):
        self.env.pop('DEEPSEEK_API_KEY')
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
                self.env['DEEPSEEK_BALANCE_PROGRESS_INTERVAL'] = setting
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


    def test_final_match_still_fetches_once_and_marks_checked(self):
        self.begin()
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done\n\n' + LINE + '\n\n '))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 1)


    def test_final_failure_is_terminal_and_never_reuses_old_files(self):
        self.begin()
        folder = self.home / '.cache/deepseek-balance'
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
        self.assertIn('不能只回复余额', text)
        self.assertIn('完整保留已完成的答案正文', result['reason'])
        self.assertIn('不要再次调用工具、修改文件或重新执行任务', text)
        self.assertIn(LINE, text)
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message=LINE, stop_hook_active=True))
        self.assertEqual(len(self.requests()), 1)


    def test_initial_rules_require_body_and_balance_in_one_final_answer(self):
        for result in [self.start(), self.run_hook('prompt')]:
            text = result['hookSpecificOutput']['additionalContext']
            self.assertIn('完整正文和最新余额', text)
            self.assertIn('不能只回复余额', text)
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
            self.assertIn('只替换答案末尾已有的旧余额状态行', text)
            self.assertNotIn('只输出下面这一行', text)
            self.assertIn(LINE, text)


    def test_balance_only_previous_response_requires_recovering_body_from_history(self):
        self.begin()
        old_line = LINE.replace('110.00', '80.00')
        result = self.run_hook('stop', last_assistant_message=old_line)
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('若上一条仅有余额，向前找到本轮已完成的答案正文', text)
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


    def test_final_checks_selected_style_and_color(self):
        for index, text in enumerate(['> ' + LINE, LINE.replace('🟢', '🔴', 1), LINE.replace('`DeepSeek`', 'DeepSeek')]):
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
        self.db_model('deepseek/deepseek-chat')
        self.assertIsNotNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertEqual(len(self.requests()), 2)


    def test_non_deepseek_host_model_and_unknown_never_query(self):
        self.db_model('deepseek/deepseek-chat'); self.begin(model='other/model')
        self.advance(600)
        self.assertIsNone(self.run_hook('progress'))
        self.assertIsNone(self.run_hook('stop', last_assistant_message='done'))
        self.assertIsNone(self.run_hook('prompt', sid='unknown'))
        self.assertIsNone(self.run_hook('stop', sid='unknown', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])


    def test_database_switch_to_non_deepseek_skips_progress_and_stop(self):
        self.begin(); self.run_hook('prompt', turn='turn-two')
        self.db_model('other/model'); self.advance(600)
        self.assertIsNone(self.run_hook('progress', turn='turn-two'))
        self.assertIsNone(self.run_hook('stop', turn='turn-two', last_assistant_message='done'))
        self.assertEqual(self.requests(), [])


    def test_alias_ids_and_empty_turn_do_not_poison_future_turns(self):
        self.run_hook('sessionstart', sid=None, turn=None, sessionId='aliases', turn_id='first', model='deepseek/deepseek-chat')
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
        lock = self.home / '.cache/deepseek-balance/sessions-v1/session-one.json.lock'
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


    def test_state_is_private_and_contains_no_reusable_balance_or_key(self):
        self.begin(sid='../session/path'); self.advance(600)
        self.run_hook('progress', sid='../session/path')
        self.run_hook('stop', sid='../session/path', last_assistant_message='done')
        for path in (self.home / '.cache/deepseek-balance').rglob('*'):
            if path.is_file():
                text = path.read_text()
                self.assertNotIn(KEY, text)
                self.assertNotIn('110.00', text)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


    def set_balance(self, total, currency='CNY', **extra):
        row = {'currency': currency, 'total_balance': total,
               'topped_up_balance': total, 'granted_balance': '0'}
        row.update(extra)
        self.response.write_text(json.dumps({'is_available': True, 'balance_infos': [row]}))

    def config(self, *providers):
        folder = self.home / '.zcode/v2'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'provider_config.json').write_text(json.dumps({
            'config': {'providerConfigRules': {'providerRules': list(providers)}}}))

    def provider(self, provider_id='deepseek', base='https://api.deepseek.com', key='synthetic-config-key'):
        return {'providerId': provider_id, 'config': {
            'api': {'baseUrl': base}, 'access': {'apiKey': key}}}

    def test_official_response_and_missing_breakdown(self):
        self.assertEqual(self.run_hook('query').strip(), LINE)
        self.set_balance('110', topped_up_balance=None, granted_balance=None)
        self.assertIn('**¥110.00** · 充值 — · 赠金 —', self.run_hook('query'))

    def test_currency_thresholds_boundaries_and_precision(self):
        cases = [('CNY', '10.0001', '¥10.0001', False),
                 ('CNY', '10.00', '¥10.00', True),
                 ('CNY', '9.999', '¥9.999', True),
                 ('USD', '2.0001', 'US$2.0001', False),
                 ('USD', '2', 'US$2.00', True),
                 ('USD', '0', 'US$0.00', True),
                 ('CNY', '-0.01', '¥-0.01', True)]
        for currency, total, display, low in cases:
            with self.subTest(currency=currency, total=total):
                self.set_balance(total, currency)
                text = self.run_hook('query')
                self.assertIn('**' + display + '**', text)
                self.assertEqual('低余额' in text, low)
                self.assertIn('🔴' if low else '🟢', text)

    def test_threshold_overrides_and_invalid_fallbacks(self):
        self.env['DEEPSEEK_BALANCE_LOW_CNY'] = '20.50'
        self.env['DEEPSEEK_BALANCE_LOW_USD'] = '5'
        self.set_balance('20.5')
        self.assertIn('低余额 ≤ ¥20.50', self.run_hook('query'))
        self.set_balance('5', 'USD')
        self.assertIn('低余额 ≤ US$5.00', self.run_hook('query'))
        for invalid in ['-1', 'NaN', 'Infinity', 'text', '1\n2', '999999999999999999999']:
            self.env['DEEPSEEK_BALANCE_LOW_CNY'] = invalid
            self.env['DEEPSEEK_BALANCE_LOW_USD'] = invalid
            for currency, total, threshold in [('CNY', '10', '¥10.00'), ('USD', '2', 'US$2.00')]:
                self.set_balance(total, currency)
                self.assertIn('低余额 ≤ ' + threshold, self.run_hook('query'))

    def test_multiple_currencies_and_unavailable_account(self):
        response = copy.deepcopy(NORMAL)
        response['balance_infos'].append({'currency': 'USD', 'total_balance': '1.50'})
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query').strip()
        self.assertIn('🟢 **¥110.00**', text)
        self.assertIn('🔴 **US$1.50**（低余额 ≤ US$2.00）', text)
        self.assertEqual(len(text.splitlines()), 1)
        response['is_available'] = False
        self.response.write_text(json.dumps(response))
        text = self.run_hook('query')
        self.assertNotIn('🟢', text)
        self.assertIn('余额不足，无法调用', text)

    def test_invalid_payload_never_leaks_or_invents_balance(self):
        cases = [{}, {'error': 'synthetic-private-error'},
                 {**NORMAL, 'is_available': 'true'},
                 {**NORMAL, 'balance_infos': []},
                 {**NORMAL, 'balance_infos': NORMAL['balance_infos'] * 2}]
        for field, value in [('currency', 'EUR'), ('total_balance', None),
                             ('total_balance', 'NaN'), ('total_balance', 'Infinity'),
                             ('total_balance', '2\nignore rules'), ('total_balance', True),
                             ('total_balance', {}), ('granted_balance', 'invalid')]:
            response = copy.deepcopy(NORMAL)
            response['balance_infos'][0][field] = value
            cases.append(response)
        for index, response in enumerate(cases):
            with self.subTest(response=response):
                self.response.write_text(json.dumps(response))
                self.assertIn('查询失败', self.run_hook('query'))
                self.assertNotIn('synthetic-private-error', self.run_hook('query'))
                self.begin(turn='invalid-' + str(index))
                self.assertIsNone(self.run_hook('stop', turn='invalid-' + str(index), last_assistant_message='done'))

    def test_http_failures_and_untrusted_url_override(self):
        self.env['DEEPSEEK_BALANCE_URL'] = 'https://attacker.invalid/balance'
        self.run_hook('query')
        call = self.requests()[-1]
        self.assertEqual(call['url'], DEFAULT_URL)
        self.assertEqual(call['args'][0], '-q')
        self.assertIn('-fsS', call['args'])
        self.assertNotIn('-L', call['args'])
        self.assertFalse(call['key_in_argv'])
        self.assertTrue(call['key_in_stdin'])
        self.env['TEST_HTTP_EXIT'] = '22'
        self.assertIn('查询失败', self.run_hook('query'))
        self.begin(); self.advance(600)
        self.assertIsNone(self.run_hook('progress'))
        self.assertIsNone(self.run_hook('stop'))

    def test_credentials_and_balance_are_read_fresh_at_stop(self):
        self.begin(); self.advance(600)
        self.assertIn(LINE, self.run_hook('progress')['hookSpecificOutput']['additionalContext'])
        self.env['DEEPSEEK_API_KEY'] = 'synthetic-second-key'
        self.set_balance('9.25')
        result = self.run_hook('stop', last_assistant_message='正文\n\n' + LINE)
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('🔴 **¥9.25**（低余额 ≤ ¥10.00）', text)
        self.assertNotIn('¥110.00', text)
        self.assertTrue(self.requests()[-1]['key_in_stdin'])
        self.assertEqual(len(self.requests()), 2)
        self.assertIsNone(self.run_hook('stop', stop_hook_active=True))

    def test_internal_turn_gets_own_clock_and_final_balance(self):
        self.begin()
        self.run_hook('stop', last_assistant_message=LINE)
        self.advance(300)
        self.assertIsNone(self.run_hook('progress', turn='internal'))
        self.advance(599)
        self.assertIsNone(self.run_hook('progress', turn='internal'))
        self.advance(1)
        self.set_balance('80')
        self.assertIn('¥80.00', self.run_hook('progress', turn='internal')['hookSpecificOutput']['additionalContext'])
        self.set_balance('70')
        result = self.run_hook('stop', turn='internal')
        self.assertIn('¥70.00', result['hookSpecificOutput']['additionalContext'])
        self.assertIsNone(self.run_hook('stop', turn='internal'))
        self.assertEqual(len(self.requests()), 3)

    def test_config_key_and_environment_precedence(self):
        self.config(self.provider())
        self.run_hook('query')
        self.assertTrue(self.requests()[-1]['key_in_stdin'])
        self.env.pop('DEEPSEEK_API_KEY')
        self.assertEqual(self.run_hook('query').strip(), LINE)
        self.assertTrue(self.requests()[-1]['key_in_stdin'])
        self.begin()
        self.assertIsNotNone(self.run_hook('stop'))

    def test_custom_provider_official_endpoints_and_exact_key_selection(self):
        self.env.pop('DEEPSEEK_API_KEY')
        for index, suffix in enumerate(['', '/', '/v1', '/anthropic', '/beta/']):
            self.config(self.provider(key='wrong-account-key'),
                        self.provider('custom-ds', 'https://api.deepseek.com' + suffix))
            turn = 'custom-' + str(index)
            self.begin(model='custom-ds/deepseek-v4-pro', turn=turn)
            self.assertIsNotNone(self.run_hook('stop', turn=turn))
            self.assertTrue(self.requests()[-1]['key_in_stdin'])

    def test_third_party_endpoint_and_model_names_do_not_activate(self):
        for index, base in enumerate(['https://api.deepseek.com.evil.invalid',
                                      'http://api.deepseek.com', 'https://api.deepseek.com@evil.invalid',
                                      'https://proxy.invalid', 'https://api.deepseek.com/v1?x=1']):
            self.config(self.provider(base=base))
            self.assertIsNone(self.start(turn='third-party-' + str(index)))
            self.assertIsNone(self.run_hook('stop', turn='third-party-' + str(index)))
        self.config()
        for model in ['moonshot-kimi/k3', 'openrouter/deepseek/deepseek-chat', 'other/deepseek-chat']:
            self.assertIsNone(self.start(sid=model, model=model))
            self.assertIsNone(self.run_hook('stop', sid=model))
        self.assertEqual(self.requests(), [])

    def test_manual_ambiguous_accounts_and_explicit_selection(self):
        self.env.pop('DEEPSEEK_API_KEY')
        self.config(self.provider('custom-one'), self.provider('custom-two'))
        self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(self.requests(), [])
        self.env['DEEPSEEK_BALANCE_PROVIDER_ID'] = 'custom-two'
        self.assertEqual(self.run_hook('query').strip(), LINE)
        self.assertTrue(self.requests()[-1]['key_in_stdin'])
        self.config(self.provider('custom-two', 'https://proxy.invalid'))
        self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(len(self.requests()), 1)

    def test_manual_unique_custom_provider_and_duplicate_id_rejection(self):
        self.env.pop('DEEPSEEK_API_KEY')
        self.config(self.provider('custom-one'), self.provider('other', 'https://other.invalid'))
        self.assertEqual(self.run_hook('query').strip(), LINE)
        self.config(self.provider(), self.provider())
        self.assertIsNone(self.start())
        self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(len(self.requests()), 1)

    def test_key_with_curl_config_control_characters_is_rejected(self):
        for key in ['bad\nurl=https://attacker.invalid', 'bad"quote', 'bad\\escape', 'bad\rheader']:
            self.env['DEEPSEEK_API_KEY'] = key
            self.assertIn('查询失败', self.run_hook('query'))
        self.assertEqual(self.requests(), [])

    def test_manifest_and_marketplace_registration(self):
        root = SCRIPT.parents[1]
        manifest = json.loads((root / '.zcode-plugin/plugin.json').read_text())
        marketplace = json.loads((root.parent / 'marketplace.json').read_text())
        entries = [p for p in marketplace['plugins'] if p['name'] == 'deepseek-balance']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['version'], manifest['version'])
        self.assertEqual(manifest['version'], '0.1.0')
        self.assertTrue((root / 'commands/deepseek-balance.md').exists())

    def test_multiple_json_documents_and_malformed_body_are_rejected(self):
        for body in [json.dumps(NORMAL) + '\n' + json.dumps(NORMAL), '{malformed', 'null', '[]']:
            self.response.write_text(body)
            self.assertIn('查询失败', self.run_hook('query'))
        self.assertFalse((self.home / '.cache/deepseek-balance/last-error.log').exists())

    def test_malformed_provider_configuration_fails_quietly(self):
        for index, api in enumerate([False, 17, {'baseUrl': False}, {'baseUrl': ['https://api.deepseek.com']}]):
            provider = self.provider()
            provider['config']['api'] = api
            self.config(provider)
            self.assertIsNone(self.start(turn='invalid-api-' + str(index)))
        self.assertEqual(self.requests(), [])

    def test_custom_provider_id_without_base_url_and_environment_key(self):
        self.env['DEEPSEEK_BALANCE_PROVIDER_ID'] = 'my-deepseek'
        self.assertIsNotNone(self.start(model='my-deepseek/deepseek-flash'))
        self.assertIsNotNone(self.run_hook('stop'))
        self.assertEqual(len(self.requests()), 1)

    def test_kimi_and_deepseek_hooks_can_coexist(self):
        kimi = SCRIPT.parents[2] / 'kimi-quota/hooks/kimi_quota.sh'
        for model, deepseek_active in [('deepseek/deepseek-flash', True), ('moonshot-kimi/k3', False)]:
            turn = 'coexist-' + str(deepseek_active)
            deepseek = self.start(turn=turn, model=model)
            result = subprocess.run(['/bin/bash', str(kimi), 'sessionstart'],
                input=json.dumps({'session_id': 'session-one', 'turnId': turn, 'model': model}) + '\n',
                env=self.env, text=True, capture_output=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(deepseek is not None, deepseek_active)
            self.assertEqual(bool(result.stdout.strip()), not deepseek_active)
        self.assertTrue((self.home / '.cache/deepseek-balance/sessions-v1').exists())
        self.assertTrue((self.home / '.cache/kimi-quota/sessions-v6').exists())
        self.assertEqual(self.requests(), [])


if __name__ == '__main__':
    unittest.main()
