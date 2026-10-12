import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workshop.profile import Profile
from workshop.service import Service
from workshop.server import Server


class ProfileTests(unittest.TestCase):
    def test_professions_and_options_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Profile(folder,True)
            data=p.get()
            data['professions']['Blacksmith']['level']=42
            data['view_options']={'route':{'level':12,'target':50}}
            p.save(data)
            self.assertEqual(Profile(folder,True).get()['professions']['Blacksmith']['level'],42)
            self.assertEqual(Profile(folder,True).get()['view_options']['route']['target'],50)

    def test_character_import_keeps_manual_hq_and_books(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Profile(folder,True)
            data=p.get()
            data['professions']['Carpenter'].update(hq=True,books=True)
            p.save(data)
            result=p.character(dict(schema=1,source='WorkshopBridge',updated_at=time.time(),home_world='Shiva',
                                    jobs={'Carpenter':dict(level=45,xp=132),'Fisher':dict(level=0,xp=0)}))
            self.assertEqual(result['professions']['Carpenter']['level'],45)
            self.assertTrue(result['professions']['Carpenter']['hq'])
            self.assertTrue(result['professions']['Carpenter']['books'])
            self.assertFalse(result['professions']['Fisher']['enabled'])

    def test_bad_or_stale_snapshot_does_not_change_saved_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Profile(folder,True)
            old=p.get()
            for stamp,level in [(time.time()-700,50),(time.time(),101)]:
                with self.assertRaises(ValueError):
                    p.character(dict(schema=1,source='WorkshopBridge',updated_at=stamp,jobs={'Carpenter':dict(level=level,xp=0)}))
                self.assertEqual(p.get(),old)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.service=Service(Path(cls.temp.name),True)
        cls.server=Server(('127.0.0.1',0),cls.service)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://'+cls.server.host

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self,path,data=None,**headers):
        req=Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
                    headers={'Authorization':'Bearer '+self.server.token,'Content-Type':'application/json',**headers})
        return json.loads(urlopen(req,timeout=10).read())

    def test_auth_origin_and_static_path_guard(self):
        for headers in [{'Authorization':'wrong'},{'Origin':'https://evil.invalid'},{'Host':'evil.invalid'}]:
            with self.assertRaises(HTTPError) as err:self.request('/api/meta',**headers)
            self.assertEqual(err.exception.code,403)
        with self.assertRaises(HTTPError) as err:urlopen(self.base+'/../profile.json')
        self.assertEqual(err.exception.code,404)

    def test_metadata_has_no_token_and_catalogs_load(self):
        data=self.request('/api/meta')
        self.assertTrue(data['demo'])
        self.assertGreater(data['recipes'],9000)
        self.assertGreater(data['leves'],1000)
        self.assertNotIn('token',data)

    def test_demo_leves_result_and_budget_persist(self):
        started=self.request('/api/start',{'kind':'leve','options':{'job':'Carpenter','level':1,'budget':None}})
        until=time.time()+15
        while time.time()<until:
            task=self.request('/api/tasks/'+started['id'])
            if task['status']!='running':break
            time.sleep(.02)
        self.assertEqual(task['status'],'done',task.get('error'))
        self.assertTrue(task['result']['rows'])
        self.assertEqual(task['result']['summary']['target'],5)

    def test_telegram_secret_not_returned_by_metadata(self):
        self.assertNotIn('token',self.request('/api/meta')['profile']['telegram'])

    def test_periodic_monitor_can_be_cancelled_without_another_cycle(self):
        from unittest.mock import patch
        calls=[]
        def scan(options,profile,cancel,progress,task):
            calls.append(1)
            cancel.set()
            return dict(rows=[],summary={'checked':1},warnings=[])
        with patch.object(self.service,'reseller',side_effect=scan):
            task=self.service.start('monitor',{})
            until=time.time()+2
            while task['status']=='running' and time.time()<until:
                task=self.service.snapshot(task['id'])
                time.sleep(.01)
            self.assertEqual(task['status'],'cancelled')
            self.assertEqual(task['result']['summary']['checked'],1)
            self.assertEqual(len(calls),1)
