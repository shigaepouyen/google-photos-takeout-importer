import json
import argparse
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import migrate as m

TEST_EMAIL = "destination@example.com"

class MigrationTests(unittest.TestCase):
    def test_cli_requires_explicit_source_and_import_account(self):
        with patch('sys.stderr',new_callable=io.StringIO):
            for args in [['scan'],['import','--source','/example/Takeout','--client','oauth.json'],
                         ['import','--source','/example/Takeout','--account',TEST_EMAIL]]:
                with self.assertRaises(SystemExit) as e: m.parse_args(args)
                self.assertEqual(e.exception.code,2)
        args=m.parse_args(['import','--source','/example/Takeout','--account',TEST_EMAIL,'--client','oauth.json'])
        self.assertEqual(args.account,TEST_EMAIL)
        self.assertIsNone(m.parse_args(['scan','--source','/example/Takeout']).account)

    def test_missing_metadata_only(self):
        existing={'ExifIFD:DateTimeOriginal':'2020:01:02 03:04:05','GPS:GPSLatitude':'50 deg',
                  'IFD0:ImageDescription':'Déjà présent'}
        wanted={'taken':'2023:01:01 00:00:00','gps':[51,3],'description':'Autre'}
        self.assertEqual(m.additions(existing,wanted,'.jpg'),{})

    def test_creation_time_is_not_capture_time(self):
        self.assertNotIn('taken',m.metadata({'creationTime':{'timestamp':'1234567890'}}))
        self.assertNotIn('gps',m.metadata({'geoData':{'latitude':0,'longitude':0}}))

    def test_video_and_utc(self):
        add=m.additions({}, {'taken':'2023:01:01 00:00:00','gps':[50,3]},'.mp4')
        self.assertEqual(add['QuickTime:CreateDate'],'2023:01:01 00:00:00')
        self.assertNotIn('QuickTime:ModifyDate',add)
        self.assertIn('Keys:GPSCoordinates',add)

    def test_scan_year_album_edited_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for folder in ['Photos de\xa02025','Mon album']:
                p=root/folder; p.mkdir()
                (p/'image.jpg').write_bytes(b'image')
                (p/'image.jpg.supplemental-metadata.json').write_text(json.dumps({
                    'title':'image.jpg','photoTakenTime':{'timestamp':'1234567890'}}))
            (root/'Mon album'/'image-modifié.jpg').write_bytes(b'edited')
            result=m.scan(root)
            self.assertEqual(result['issues'],[])
            self.assertEqual(result['summary'],{'media_matched':3,'unique_media':2,'albums':1})

    def test_ambiguous_json_blocks_matching(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'image.jpg').write_bytes(b'image')
            for name in ['one','two']:
                (p/(name+'.json')).write_text(json.dumps({'title':'image.jpg','photoTakenTime':{'timestamp':'123'}}))
            r=m.scan(p)
            self.assertEqual(r['records'],[])
            self.assertTrue(r['issues'])

    def test_resume_and_wrong_account(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); response=Mock()
            response.json.return_value={'id':'album-1'}; session.post.return_value=response
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            imp=m.Importer(session,path,user,'client')
            self.assertEqual(imp.album('a','Album'),'album-1')
            self.assertEqual(imp.album('a','Album'),'album-1')
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(m.Importer(session,path,user,'client').album('a','Album'),'album-1')
            with self.assertRaises(RuntimeError): m.Importer(session,path,{'sub':'456','email':'wrong@example.com'},'client')

    def test_upload_and_album_membership_resume(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d); media=path/'photo.jpg'; media.write_bytes(b'photo')
            session=Mock()
            binary=Mock(); binary.text='upload-token'
            created=Mock(); created.json.return_value={'newMediaItemResults':[{'status':{'code':0},'mediaItem':{'id':'media-1'}}]}
            added=Mock(); added.json.return_value={}
            session.post.side_effect=[binary,created,added]
            user={'sub':'123','email':TEST_EMAIL}
            imp=m.Importer(session,path/'state.json',user,'client')
            record={'id':'sha','meta':{'description':'Description'}}
            self.assertEqual(imp.upload(record,media),'media-1')
            imp.member('album-1','media-1')
            again=m.Importer(session,path/'state.json',user,'client')
            self.assertEqual(again.upload(record,media),'media-1')
            again.member('album-1','media-1')
            self.assertEqual(session.post.call_count,3)
            self.assertEqual(session.post.call_args_list[1].kwargs['json']['newMediaItems'][0]['description'],'Description')

    def test_progress_and_full_run_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source'; root.mkdir()
            media=root/'photo.jpg'; media.write_bytes(b'photo')
            records=[{'id':'sha','source':str(media),'relative':'photo.jpg','meta':{},
                      'album':key,'album_title':key} for key in ['Album A','Album B']]
            plan={'records':records,'issues':[],
                  'summary':{'media_matched':2,'unique_media':1,'albums':2}}
            args=argparse.Namespace(source=str(root),work=str(Path(d)/'work'),
                account=TEST_EMAIL,command='import',allow_unmatched=False,
                album=None,limit=None,client='unused.json')
            user={'sub':'123','email':TEST_EMAIL}
            session=Mock()
            responses=[]
            for payload in [{'id':'album-a'},{'id':'album-b'},None,
                {'newMediaItemResults':[{'mediaItem':{'id':'media-1'}}]}, {},{}]:
                response=Mock(); response.json.return_value=payload; response.text='upload-token'
                responses.append(response)
            session.post.side_effect=responses
            output=io.StringIO()
            with patch.object(m,'scan',return_value=plan), patch.object(m,'prepare_one',return_value=media), \
                 patch.object(m,'auth',return_value=(session,user,'client')), patch.object(m.shutil,'which',return_value='/exiftool'):
                m.run(args,m.Console(color=False,stream=output))
                self.assertEqual(session.post.call_count,6)
                m.run(args,m.Console(color=False,stream=output))
                self.assertEqual(session.post.call_count,6)
            text=output.getvalue()
            self.assertIn('Album créé : Album A',text)
            self.assertIn('Album réutilisé : Album A',text)
            self.assertIn('1/1 médias déjà importés',text)
            self.assertIn('reste à faire : 0',text)
            self.assertNotIn('\x1b',text)

    def test_429_retry_then_success(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); rejected=Mock(status_code=429); accepted=Mock(status_code=200)
            accepted.json.return_value={'id':'album-1'}
            session.post.side_effect=[rejected,accepted]
            imp=m.Importer(session,Path(d)/'state.json',{'sub':'123','email':TEST_EMAIL},'client')
            with patch.object(imp,'wait_retry') as wait:
                self.assertEqual(imp.album('a','Album'),'album-1')
                wait.assert_called_once_with(rejected,0)
            self.assertIsNone(imp.state['pending'])
            self.assertEqual(session.post.call_count,2)

    def test_429_exhausted_is_safe_to_resume(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); session.post.return_value=Mock(status_code=429)
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            imp=m.Importer(session,path,user,'client')
            with patch.object(imp,'wait_retry'):
                with self.assertRaisesRegex(RuntimeError,'429 persistante'): imp.album('a','Album')
            self.assertEqual(session.post.call_count,6)
            self.assertIsNone(m.read_json(path)['pending'])
            m.Importer(session,path,user,'client')

    def test_interrupt_during_429_wait_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); session.post.return_value=Mock(status_code=429)
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            imp=m.Importer(session,path,user,'client')
            with patch.object(imp,'wait_retry',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): imp.album('a','Album')
            self.assertIsNone(m.read_json(path)['pending'])

    def test_rejected_media_is_recorded_then_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d); media=path/'video.mp4'; media.write_bytes(b'media')
            session=Mock()
            raw=Mock(status_code=200); raw.text='token'
            bad=Mock(status_code=200); bad.json.return_value={'newMediaItemResults':[{'status':{'code':3,'message':'Rejected'}}]}
            good=Mock(status_code=200); good.json.return_value={'newMediaItemResults':[{'mediaItem':{'id':'media-1'}}]}
            session.post.side_effect=[raw,bad,raw,good]
            imp=m.Importer(session,path/'state.json',{'sub':'123','email':TEST_EMAIL},'client')
            record={'id':'sha','meta':{},'source':str(media),'relative':'video.mp4'}
            with self.assertRaises(m.MediaRejected): imp.upload(record,media)
            self.assertIn('sha',m.read_json(path/'failed-media.json'))
            self.assertIsNone(imp.state['pending'])
            self.assertNotIn('sha',imp.state['media'])
            self.assertEqual(imp.upload(record,media),'media-1')
            self.assertEqual(m.read_json(path/'failed-media.json'),{})

    def test_run_continues_after_media_rejection_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source'; root.mkdir()
            records=[]
            for ident in ['bad','good']:
                media=root/(ident+'.jpg'); media.write_bytes(b'photo')
                records.append({'id':ident,'source':str(media),'relative':media.name,'meta':{},'album':'Album','album_title':'Album'})
            plan={'records':records,'issues':[],'summary':{'media_matched':2,'unique_media':2,'albums':1}}
            args=argparse.Namespace(source=str(root),work=str(Path(d)/'work'),account=TEST_EMAIL,
                command='import',allow_unmatched=False,album=None,limit=None,client='unused.json')
            session=Mock(); responses=[]
            for payload in [{'id':'album'},None,{'newMediaItemResults':[{'status':{'code':3,'message':'Rejected'}}]},
                            None,{'newMediaItemResults':[{'mediaItem':{'id':'good-id'}}]},{}]:
                response=Mock(status_code=200); response.json.return_value=payload; response.text='token'; responses.append(response)
            session.post.side_effect=responses
            output=io.StringIO()
            with patch.object(m,'scan',return_value=plan), patch.object(m,'prepare_one',side_effect=lambda r,w:Path(r['source'])), \
                 patch.object(m,'auth',return_value=(session,{'sub':'123','email':TEST_EMAIL},'client')), patch.object(m.shutil,'which',return_value='/exiftool'):
                self.assertEqual(m.run(args,m.Console(color=False,stream=output)),2)
            state=m.read_json(Path(d)/'work'/'state.json')
            self.assertIn('bad',state['failed']); self.assertEqual(state['media'],{'good':'good-id'})
            self.assertEqual(state['members']['album'],['good-id'])
            self.assertIn('reste à faire : 1 médias, 1 associations',output.getvalue())
            self.assertIn('Album incomplet',output.getvalue())

    def test_pending_membership_recovers_known_ids_only(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            state={'account':{**user,'client_id':'client'},'albums':{'folder':'album-1'},
                'media':{'sha':'media-1'},'members':{},'pending':{'operation':'add_to_album',
                'path':'/albums/album-1:batchAddMediaItems','payload':{'mediaItemIds':['media-1']}}}
            m.save(path,state)
            session=Mock(); session.post.return_value=Mock(status_code=200)
            session.post.return_value.json.return_value={}
            imp=m.Importer(session,path,user,'client')
            self.assertIsNone(imp.state['pending'])
            self.assertEqual(imp.state['members']['album-1'],['media-1'])
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(session.post.call_args.args[0],m.BASE+'/albums/album-1:batchAddMediaItems')
            self.assertTrue(list(path.parent.glob('state.before-membership-resume-*.json')))
            self.assertEqual(imp.state['media'],{'sha':'media-1'})

    def test_pending_unknown_membership_stays_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            state={'albums':{'folder':'album-1'},'media':{'sha':'media-1'},'members':{},
                'pending':{'operation':'add_to_album','path':'/albums/album-1:batchAddMediaItems',
                           'payload':{'mediaItemIds':['unknown-media']}}}
            m.save(path,state); session=Mock()
            with self.assertRaises(RuntimeError): m.Importer(session,path,user,'client')
            session.post.assert_not_called()
            self.assertEqual(m.read_json(path)['pending'],state['pending'])

    def test_uncertain_create_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); session.post.side_effect=TimeoutError('network lost')
            path=Path(d)/'state.json'; user={'sub':'123','email':TEST_EMAIL}
            imp=m.Importer(session,path,user,'client')
            with self.assertRaises(TimeoutError): imp.album('a','Album')
            with self.assertRaises(RuntimeError): m.Importer(session,path,user,'client')
            self.assertEqual(session.post.call_count,1)

if __name__=='__main__': unittest.main()
