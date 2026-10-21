"""Offline regression suite. UI doubles do NOT verify the real Telegram library/API."""
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import zipfile
from hosting import (Settings, Manager, HostingError, extract_zip, parse_env, filename,
                     redact, health_server, TOKEN_RE)

A = '111111111:' + 'A' * 35
B = '222222222:' + 'B' * 35
C = '333333333:' + 'C' * 35
ROOT = Path(__file__).resolve().parent

def archive(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        for name, content in files.items():
            z.writestr(name, content)
    return b.getvalue()

def wait_until(fn, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(.03)
    return False

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(A, 1, Path(self.tmp.name) / 'data', 10000, True)
        self.m = Manager(self.settings)
        self.m.ensure_user(1)
        self.m.ensure_user(2)
        self.m.authorize(1, 2, True)
        self.m.ensure_user(3)
        self.m.authorize(1, 3, True)
    def tearDown(self):
        self.m.close()
        self.tmp.cleanup()
    def project(self, code='import time\nprint("ready",flush=True)\ntime.sleep(60)\n', uid=2, token=B):
        pid = self.m.upload(uid, 'bot.py', code.encode())
        self.m.configure(uid, pid, '.env', ('BOT_TOKEN=' + token + '\nCUSTOM_SECRET=my_private_value\n').encode())
        return pid
    def prepare_python(self, pid):
        # Real isolated interpreter; no third-party downloads required for process tests.
        path = self.m.python(pid)
        subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(path.parent.parent)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return path
    def test_settings_valid_dummy(self):
        with patch.dict(os.environ, {'HOST_BOT_TOKEN': A, 'OWNER_ID': '1', 'PORT': '9090'}, clear=True):
            s = Settings.from_env()
            self.assertEqual(s.token, A)
            self.assertEqual(s.port, 9090)
            self.assertFalse(s.execution)
    def test_settings_missing_and_invalid(self):
        for env in ({}, {'HOST_BOT_TOKEN':'bad','OWNER_ID':'1'}, {'BOT_TOKEN':A,'OWNER_ID':'1'},
                    {'HOST_BOT_TOKEN':A,'OWNER_ID':'bad'}, {'HOST_BOT_TOKEN':A,'OWNER_ID':'0'},
                    {'HOST_BOT_TOKEN':A,'OWNER_ID':'1','PORT':'0'}, {'HOST_BOT_TOKEN':A,'OWNER_ID':'1','PORT':'x'},
                    {'HOST_BOT_TOKEN':A,'OWNER_ID':'1','ALLOW_UNSANDBOXED_EXECUTION':'yes'}):
            with self.subTest(env=list(env)), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(HostingError): Settings.from_env()
    def test_token_isolation_real_subprocess(self):
        pid = self.project('import os,json\nprint(json.dumps({k:os.getenv(k) for k in ["BOT_TOKEN","HOST_BOT_TOKEN","DATABASE_URL","OWNER_ID","MARKER"]}),flush=True)\n')
        folder = self.m.folder(self.m.project(2,pid))
        with patch.dict(os.environ, {'HOST_BOT_TOKEN': A, 'BOT_TOKEN': A, 'DATABASE_URL':'host-db-secret', 'MARKER':'host'}):
            env = self.m.child_env(folder)
            result = subprocess.run([sys.executable, str(folder/'bot.py')], cwd=folder, env=env, capture_output=True, text=True, check=True)
            resolved = json.loads(result.stdout)
            self.assertEqual(resolved['BOT_TOKEN'], B)
            self.assertNotIn(A, result.stdout)
            for key in ['HOST_BOT_TOKEN','DATABASE_URL','OWNER_ID','MARKER']: self.assertIsNone(resolved[key])
        self.assertEqual(self.settings.token, A)
    def test_host_account_rejected(self):
        pid = self.project()
        for token in [A, A.split(':')[0] + ':' + 'Z'*35]:
            with self.assertRaises(HostingError):
                self.m.configure(2,pid,'.env',('BOT_TOKEN='+token).encode())
    def test_missing_token_never_falls_back(self):
        pid = self.m.upload(2, 'bot.py', b'print("x")')
        with patch.dict(os.environ, {'BOT_TOKEN':A}):
            with self.assertRaises(HostingError): self.m.start(2,pid)
    def test_env_quotes_comments_and_invalid(self):
        p = Path(self.tmp.name)/'.env'
        p.write_text('# comment\nBOT_TOKEN="'+B+'"\nX=literal${VALUE}\n')
        self.assertEqual(parse_env(p)['BOT_TOKEN'], B)
        self.assertEqual(parse_env(p)['X'], 'literal${VALUE}')
        for bad in ['X=1\nX=2', 'PORT=10000', 'HOST_BOT_TOKEN=x', 'PIP_INDEX_URL=x', 'PYTHONPATH=x', 'LD_PRELOAD=x', 'a b=c', 'X="bad', 'X=\x00']:
            p.write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(HostingError): parse_env(p)
    def test_invalid_filenames_and_uploads(self):
        for name in ['../x.py','/x.py','x\\y.py','.', '..', None, 'a'*101]:
            with self.subTest(name=name), self.assertRaises(HostingError): filename(name)
        for name, data in [('x.js',b'code'),('requirements.txt',b'x'),('x.py',b''),('x.zip',b'bad')]:
            with self.subTest(name=name), self.assertRaises(HostingError): self.m.upload(2,name,data)
        self.assertEqual(self.m.ensure_user(2)['files'], [])
    def test_upload_limit_and_owner_exemption(self):
        for i in range(2): self.m.upload(2, str(i)+'.py', b'pass')
        with self.assertRaises(HostingError): self.m.upload(2,'third.py',b'pass')
        for i in range(3): self.m.upload(1,str(i)+'.py',b'pass')
    def test_zip_normal_wrapper_and_cwd(self):
        pid = self.m.upload(2,'project.zip',archive({'package/main.py':'print("ok")','package/.env':'BOT_TOKEN='+B}))
        p=self.m.project(2,pid)
        self.assertEqual(p['entry'],'main.py')
        env=self.m.child_env(self.m.folder(p))
        self.assertEqual(Path(env['VIRTUAL_ENV']),self.m.python(pid).parent.parent)
    def test_zip_traversal_absolute_backslash_reserved(self):
        for name in ['../escape.py','/escape.py','a/../../escape.py','a\\b.py','C:/x.py','.venv/bin/python','.git/config']:
            with self.subTest(name=name), self.assertRaises(HostingError):
                self.m.upload(1,'bad.zip',archive({name:'bad','main.py':'pass'}))
        self.assertFalse((Path(self.tmp.name)/'escape.py').exists())
    def test_zip_symlink(self):
        info=zipfile.ZipInfo('link')
        info.create_system=3
        info.external_attr=(0o120777 << 16)
        with self.assertRaises(HostingError): self.m.upload(2,'bad.zip',archive({info:'../outside','bot.py':'pass'}))
    def test_zip_duplicate(self):
        with self.assertRaises(HostingError): self.m.upload(2,'bad.zip',archive({'BOT.py':'pass','bot.py':'pass'}))
    def test_zip_expansion_limit(self):
        with patch('hosting.MAX_EXPANDED',10), self.assertRaises(HostingError):
            self.m.upload(2,'bad.zip',archive({'main.py':'x'*11}))
    def test_zip_missing_ambiguous_entry(self):
        for files in [{'README.txt':'x'}, {'bot.py':'pass','main.py':'pass'}, {'a.py':'pass','b.py':'pass'}]:
            with self.subTest(files=list(files)), self.assertRaises(HostingError): self.m.upload(1,'bad.zip',archive(files))
    def test_persistence_and_no_silent_reset(self):
        pid = self.project()
        other = Manager(self.settings)
        self.assertEqual(other.project(2,pid)['name'],'bot.py')
        other.close()
        self.m.db.write_text('broken')
        with self.assertRaises(HostingError): Manager(self.settings)
        self.assertEqual(self.m.db.read_text(),'broken')
        self.m.save()
    def test_unwritable_storage_injected(self):
        with patch('hosting.atomic_json',side_effect=PermissionError('denied')):
            with self.assertRaises(HostingError): Manager(self.settings)
    def test_actual_file_directory_permissions(self):
        pid=self.project()
        self.assertEqual(self.m.root.stat().st_mode & 0o777,0o700)
        self.assertEqual(self.m.db.stat().st_mode & 0o777,0o600)
        self.assertEqual((self.m.folder(self.m.project(2,pid))/'.env').stat().st_mode & 0o777,0o600)
    def test_user_separation_and_approval(self):
        pid=self.project()
        for fn in [lambda:self.m.project(3,pid), lambda:self.m.stop(3,pid), lambda:self.m.log_text(3,pid),
                   lambda:self.m.configure(3,pid,'.env',b'BOT_TOKEN=x'),lambda:self.m.authorize(2,3,False)]:
            with self.assertRaises(HostingError): fn()
        self.m.ensure_user(4)
        with self.assertRaises(HostingError): self.m.upload(4,'bot.py',b'pass')
    def test_real_venv_install_empty_requirements(self):
        pid=self.project()
        self.m.configure(2,pid,'requirements.txt',b'# offline empty dependency set\n')
        self.assertIn('pip check passed',self.m.install(2,pid))
        result=subprocess.run([str(self.m.python(pid)),'-I','-m','pip','check'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        cfg=(self.m.python(pid).parent.parent/'pyvenv.cfg').read_text()
        self.assertIn('include-system-site-packages = false',cfg)
        result=subprocess.run([str(self.m.python(pid)),'-I','-c','import sys;print(sys.prefix != sys.base_prefix)'],capture_output=True,text=True)
        self.assertEqual(result.stdout.strip(),'True')
    def test_missing_dependency_runtime(self):
        pid=self.project('import nonexistent_offline_test_package\n')
        self.prepare_python(pid)
        self.m.start(2,pid)
        self.assertTrue(wait_until(lambda:'ModuleNotFoundError' in self.m.log_text(2,pid)))
        self.assertTrue(wait_until(lambda:self.m.status(2,pid)=='Stopped'))
    def test_pip_failure_and_log_capture(self):
        pid=self.project()
        self.m.configure(2,pid,'requirements.txt',b'bad @ file:///definitely_missing_package.whl\n')
        with self.assertRaises(HostingError): self.m.install(2,pid)
        self.assertNotIn(pid,self.m.jobs)
        self.assertTrue(self.m.log_text(2,pid))
    def test_start_stop_restart_and_redaction(self):
        pid=self.project('import os,time\nprint(os.getcwd(),flush=True)\nprint(os.getenv("BOT_TOKEN"),flush=True)\nprint(os.getenv("CUSTOM_SECRET"),flush=True)\ntime.sleep(60)\n')
        self.prepare_python(pid)
        self.m.start(2,pid)
        self.assertTrue(wait_until(lambda:'REDACTED' in self.m.log_text(2,pid)))
        logs=self.m.log_text(2,pid)
        self.assertNotIn(B,logs)
        self.assertNotIn('my_private_value',logs)
        self.assertIn(str(self.m.folder(self.m.project(2,pid))),logs)
        old=self.m.running[pid][0]
        self.m.restart(2,pid)
        self.assertIsNotNone(old.poll())
        self.assertNotEqual(old.pid,self.m.running[pid][0].pid)
        self.m.stop(2,pid)
        self.assertEqual(self.m.status(2,pid),'Stopped')
        self.assertFalse(self.m.projects[pid]['desired'])
    def test_atomic_duplicate_start_race(self):
        pid=self.project();self.prepare_python(pid)
        outcomes=[]
        def work():
            try: self.m.start(2,pid);outcomes.append('started')
            except HostingError: outcomes.append('blocked')
        threads=[threading.Thread(target=work) for _ in range(6)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(outcomes.count('started'),1)
        self.assertEqual(outcomes.count('blocked'),5)
    def test_duplicate_account_across_projects(self):
        a=self.project();b=self.project(uid=3)
        self.prepare_python(a);self.prepare_python(b)
        self.m.start(2,a)
        with self.assertRaises(HostingError): self.m.start(3,b)
    def test_ordinary_child_process_cleanup(self):
        pid=self.project('import subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\nopen("child.pid","w").write(str(p.pid))\ntime.sleep(60)\n')
        self.prepare_python(pid);self.m.start(2,pid)
        f=self.m.folder(self.m.project(2,pid))/'child.pid'
        self.assertTrue(wait_until(f.exists))
        child=int(f.read_text());self.m.stop(2,pid)
        def gone_or_zombie():
            p=Path(f'/proc/{child}/stat')
            try:
                return p.read_text().split()[2]=='Z'
            except (FileNotFoundError, ProcessLookupError):
                return True
        self.assertTrue(wait_until(gone_or_zombie))
    def test_reject_stops_and_blocks_user(self):
        pid=self.project();self.prepare_python(pid);self.m.start(2,pid)
        proc=self.m.running[pid][0]
        self.m.authorize(1,2,False)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(self.m.projects[pid]['desired'])
        with self.assertRaises(HostingError): self.m.start(2,pid)
    def test_saved_running_state_restore_and_stopped_stays_stopped(self):
        pid=self.project();self.prepare_python(pid);self.m.start(2,pid)
        self.m.close()
        self.m=Manager(self.settings)
        self.assertEqual(self.m.restore(),[(pid,'launched')])
        self.m.stop(2,pid);self.m.close();self.m=Manager(self.settings)
        self.assertEqual(self.m.restore(),[])
    def test_execution_opt_in(self):
        pid=self.project()
        self.m.settings=Settings(A,1,self.settings.data_dir,10000,False)
        with self.assertRaises(HostingError):self.m.install(2,pid)
        with self.assertRaises(HostingError):self.m.start(2,pid)
    def test_config_change_running_blocked(self):
        pid=self.project();self.prepare_python(pid);self.m.start(2,pid)
        with self.assertRaises(HostingError):self.m.configure(2,pid,'.env',('BOT_TOKEN='+C).encode())
    def test_single_instance_lock(self):
        self.m.acquire_instance_lock();other=Manager(self.settings)
        with self.assertRaises(HostingError):other.acquire_instance_lock()
        other.close()
    def test_logs_bounded(self):
        pid=self.project()
        for n in range(500):self.m.safe_log(pid,str(n)*1000)
        self.assertEqual(len(self.m.logs[pid]),100)
        self.assertLessEqual(len(self.m.log_text(2,pid)),3000)
    def test_health_bind_response_and_conflict(self):
        import http.client
        event=threading.Event();server=health_server(0,event)
        try:
            port=server.server_address[1]
            self.assertEqual(server.server_address[0],'0.0.0.0')
            with self.assertRaises(OSError):health_server(port,event)
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
            c.request('GET','/healthz');r=c.getresponse();self.assertEqual(r.status,200);r.read()
            event.set();c.request('GET','/healthz');r=c.getresponse();self.assertEqual(r.status,503);r.read();c.close()
        finally:server.shutdown();server.server_close()
    def test_cli_config_missing_invalid_valid(self):
        for token,expected in [('',2),('invalid',2),(A,0)]:
            env={'PATH':os.environ['PATH'],'HOST_BOT_TOKEN':token,'OWNER_ID':'1','DATA_DIR':str(Path(self.tmp.name)/'cli')}
            result=subprocess.run([sys.executable,str(ROOT/'bot.py'),'--check-config'],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,expected,result.stderr)
            self.assertNotIn(A,result.stdout+result.stderr)

class Markup:
    def __init__(self, keyboard=None, **kwargs):self.keyboard=list(keyboard or [])
    def row(self,*items):self.keyboard.append(list(items))
class Button:
    def __init__(self,text,callback_data=None):self.text=text;self.callback_data=callback_data
class FakeBot:
    """Only the small Telegram surface used by this source, not an API emulator."""
    def __init__(self,token,**kwargs):self.token=token;self.sent=[];self.handlers=[];self.callbacks=[]
    def message_handler(self,**kwargs):
        def deco(fn):self.handlers.append((kwargs,fn));return fn
        return deco
    def callback_query_handler(self,**kwargs):
        def deco(fn):self.callbacks.append(fn);return fn
        return deco
    def send_message(self,chat,text,**kwargs):self.sent.append((chat,text,kwargs));return types.SimpleNamespace(message_id=1)
    def answer_callback_query(self,*args):pass
    def delete_message(self,*args):pass
    def get_file(self,*args):return types.SimpleNamespace(file_path='test')
    def download_file(self,*args):return self.document_bytes
    def get_me(self):return types.SimpleNamespace(id=111111111)
    def stop_polling(self):pass
    def infinity_polling(self,**kwargs):return

class UIContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        tele=types.ModuleType('telebot');tele.TeleBot=FakeBot;tele.logger=logging.getLogger('test.TeleBot')
        ty=types.ModuleType('telebot.types')
        ty.InlineKeyboardMarkup=ty.ReplyKeyboardMarkup=Markup
        ty.InlineKeyboardButton=ty.KeyboardButton=Button
        ps=types.ModuleType('psutil');ps.virtual_memory=lambda:types.SimpleNamespace(available=1000000);ps.cpu_percent=lambda:0
        self.p1=patch.dict(sys.modules,{'telebot':tele,'telebot.types':ty,'psutil':ps});self.p1.start()
        self.p2=patch.dict(os.environ,{'HOST_BOT_TOKEN':A,'OWNER_ID':'1','DATA_DIR':self.tmp.name,'ALLOW_UNSANDBOXED_EXECUTION':'true'});self.p2.start()
        spec=importlib.util.spec_from_file_location('host_under_test',ROOT/'bot.py')
        self.ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.ui)
    def tearDown(self):
        self.ui.manager.close();self.p2.stop();self.p1.stop();self.tmp.cleanup()
    def message(self,uid=1,text='/start',chat_type='private'):
        return types.SimpleNamespace(from_user=types.SimpleNamespace(id=uid,first_name='Test',username=None),
                                     chat=types.SimpleNamespace(id=uid,type=chat_type),text=text,message_id=7)
    def call(self,data,uid=1):return types.SimpleNamespace(from_user=types.SimpleNamespace(id=uid),message=self.message(uid),data=data,id='test-callback')
    def test_start_approval_and_menu(self):
        self.ui.start_command(self.message(2));self.assertFalse(self.ui.manager.approved(2))
        self.ui.callback_query(self.call('approve_2'));self.assertTrue(self.ui.manager.approved(2))
        self.ui.start_command(self.message(2));self.assertIn('DK HOSTING',self.ui.bot.sent[-2][1])
        self.assertEqual(len(self.ui.control_buttons().keyboard),4)
    def test_group_ignored_and_owner_enforced(self):
        self.ui.start_command(self.message(2,chat_type='group'));self.assertEqual(self.ui.bot.sent,[])
        self.ui.callback_query(self.call('approve_3',2));self.assertIn('Owner only',self.ui.bot.sent[-1][1])
    def test_all_top_level_menus(self):
        for text in ['🌏 Upload','📁 files','🔄 restart','⏹ stop','⚡ speed','🚀 status','⚙ Recommended Install','/cancel','/help']:
            self.ui.control_handler(self.message(text=text))
        self.assertEqual(len(self.ui.bot.sent),9)
    def test_document_project_config_and_callback_ownership(self):
        msg=self.message();msg.document=types.SimpleNamespace(file_name='bot.py',file_size=4,file_id='dummy')
        self.ui.bot.document_bytes=b'pass';self.ui.document_handler(msg)
        pid=self.ui.manager.ensure_user(1)['files'][0]
        self.ui.callback_query(self.call('env:'+pid))
        msg.document.file_name='.env';data=('BOT_TOKEN='+B).encode();msg.document.file_size=len(data);self.ui.bot.document_bytes=data
        self.ui.document_handler(msg)
        self.assertEqual(self.ui.manager.child_env(self.ui.manager.folder(self.ui.manager.project(1,pid)))['BOT_TOKEN'],B)
        self.ui.manager.ensure_user(2);self.ui.manager.authorize(1,2,True)
        self.ui.callback_query(self.call('stop:'+pid,2));self.assertIn('not found',self.ui.bot.sent[-1][1])
        for action in ['file','status','logs','stop','req']:
            self.ui.callback_query(self.call(action+':'+pid))
    def test_callback_payload_lengths(self):
        self.ui.manager.ensure_user(1)
        pid=self.ui.manager.upload(1,'a'*97+'.py',b'pass')
        self.ui.project_menu(1,pid)
        for row in self.ui.bot.sent[-1][2]['reply_markup'].keyboard:
            for button in row:self.assertLessEqual(len(button.callback_data.encode()),64)
    def test_main_mock_transport_startup_and_cleanup(self):
        # Real HTTP thread and lock; Telegram credentials/polling are test doubles.
        with patch.object(self.ui,'settings',Settings(A,1,Path(self.tmp.name),0,True)):
            self.assertEqual(self.ui.main(),0)
        self.assertTrue(self.ui.manager.closed)

if __name__=='__main__':
    unittest.main(verbosity=2)
