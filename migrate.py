#!/usr/bin/env python3
"""Google Photos Takeout migration. Python >=3.10; see README.md."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import random
from email.utils import parsedate_to_datetime
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

MEDIA = {'.jpg','.jpeg','.png','.heic','.heif','.webp','.gif','.avif','.tif','.tiff','.bmp','.mp4','.mov','.m4v','.avi','.mkv','.mts','.3gp','.webm'}
VIDEO = {'.mp4','.mov','.m4v','.avi','.mkv','.mts','.3gp','.webm'}
BASE = 'https://photoslibrary.googleapis.com/v1'
SCOPES = ['openid','https://www.googleapis.com/auth/userinfo.email',
          'https://www.googleapis.com/auth/photoslibrary.appendonly']

class Console:
    """Small dependency-free terminal UI; readable logs when redirected."""
    def __init__(self, color=None, stream=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self.color = self.tty and 'NO_COLOR' not in os.environ if color is None else color
        self.active = False
        self.started = time.monotonic()
        self.last_log = 0

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def clear(self):
        if self.active:
            self.stream.write('\r\033[2K'); self.stream.flush(); self.active = False

    def message(self, text, kind='info'):
        self.clear()
        code = {'info':'36','ok':'32','warn':'33','error':'31','title':'1;36'}[kind]
        print(self.paint(text, code), file=self.stream, flush=True)

    @staticmethod
    def duration(seconds):
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours}h{minutes:02}m' if hours else f'{minutes}m{seconds:02}s'

    def progress(self, label, done, total, started, detail='', force=False):
        now = time.monotonic()
        if not self.tty and not force and done != total and now-self.last_log < 5:
            return
        self.last_log = now
        elapsed = now-started
        remaining = max(0, total-done)
        ratio = done/total if total else 1
        eta = self.duration(elapsed/done*remaining) if done else '…'
        width = 18
        bar = '#' * int(width*ratio) + '-' * (width-int(width*ratio))
        text = f'{label} [{bar}] {ratio:5.1%}  {done}/{total} | reste {remaining} | écoulé {self.duration(elapsed)} | estimé {eta}'
        if self.tty:
            columns = shutil.get_terminal_size((110,24)).columns
            # One line avoids terminal wrapping and stale progress fragments.
            text = (text + (' | '+detail if detail else ''))[:max(20,columns-1)]
            self.stream.write('\r\033[2K'+self.paint(text,'36')); self.stream.flush()
            self.active = True
            if done == total: self.clear(); self.message(text, 'ok')
        else:
            print(text + (' | '+detail if detail else ''), file=self.stream, flush=True)


def norm(s):
    return unicodedata.normalize('NFC', s)

def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def read_json(p):
    return json.loads(p.read_text(encoding='utf-8-sig'))

def metadata(d):
    # photoTakenTime is capture time; creationTime is NOT a safe fallback.
    result = {'description': d.get('description','') or ''}
    try:
        stamp = int(d['photoTakenTime']['timestamp'])
        if stamp > 0:
            result['taken'] = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime('%Y:%m:%d %H:%M:%S')
    except (KeyError, ValueError, TypeError, OverflowError, OSError): pass
    for key in ('geoDataExif','geoData'):
        g = d.get(key) or {}
        try:
            lat, lon = float(g['latitude']), float(g['longitude'])
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 or lon != 0):
                result['gps'] = [lat, lon]
                if g.get('altitude') is not None and math.isfinite(float(g['altitude'])):
                    result['altitude'] = float(g['altitude'])
                break
        except (KeyError, TypeError, ValueError): pass
    return result

def scan(root, console=None):
    root = root.resolve()
    records, issues, album_titles = [], [], {}
    files = sorted(f for f in root.rglob('*') if f.is_file() and not f.is_symlink())
    # Never follow a directory symlink outside Takeout.
    files = [f for f in files if f.resolve().is_relative_to(root)]
    by_dir = collections.defaultdict(list)
    sidecars = collections.defaultdict(list)
    for f in files:
        if f.suffix.lower() in MEDIA: by_dir[f.parent].append(f)
        elif f.suffix.lower() == '.json':
            try:
                d = read_json(f)
                if not isinstance(d, dict): continue
                if isinstance(d.get('title'), str) and ('photoTakenTime' in d or 'googlePhotosOrigin' in d or 'imageViews' in d):
                    sidecars[f.parent].append((f,d))
                elif isinstance(d.get('title'), str) and ('access' in d or norm(f.stem).lower() in {'métadonnées','metadata'}):
                    album_titles[f.parent] = d['title']
            except (ValueError, OSError) as e: issues.append({'file':str(f),'issue':f'JSON illisible: {e}'})
    used = set()
    scan_total = sum(map(len, by_dir.values()))
    scan_done = 0
    scan_started = time.monotonic()
    for directory, media in by_dir.items():
        for f in media:
            scan_done += 1
            if console: console.progress('Analyse',scan_done-1,scan_total,scan_started,f.name)
            derivative = False
            candidates = []
            # Exact filename first, including Google's newer sidecar suffix.
            exact = {norm(f.name+'.json'), norm(f.name+'.supplemental-metadata.json')}
            candidates = [(j,d) for j,d in sidecars[directory] if norm(j.name) in exact]
            if not candidates:
                candidates = [(j,d) for j,d in sidecars[directory] if norm(d.get('title','')) == norm(f.name)]
                # A title can correspond to multiple colliding files. Never guess.
                if len([x for x in media if norm(x.name)==norm(f.name)]) != 1: candidates=[]
            if not candidates:
                base = re.sub(r'-(modifié|edited)$', '', norm(f.stem), flags=re.I) + norm(f.suffix)
                if base != norm(f.name):
                    candidates = [(j,d) for j,d in sidecars[directory] if norm(d.get('title','')) == base]
                    derivative = len(candidates) == 1
            if len(candidates) != 1:
                issues.append({'file':str(f.relative_to(root)), 'issue':'JSON absent ou ambigu', 'candidates':[str(j.name) for j,d in candidates]})
                continue
            j, d = candidates[0]
            used.add(j)
            meta = metadata(d)
            content = digest(f)
            ident = hashlib.sha256((content+json.dumps(meta,sort_keys=True)).encode()).hexdigest()
            album = album_titles.get(directory)
            if album is None and not re.fullmatch(r'(Photos from|Photos de|Photos datant de)\s+\d{4}',directory.name,re.I):
                # Explicit album metadata preferred; ordinary subfolders are albums.
                if directory != root and directory.name.replace('\xa0',' ') != 'Google Photos': album=directory.name
            records.append({'source':str(f),'relative':str(f.relative_to(root)), 'sidecar':str(j),'association':'edited-original' if derivative else 'exact',
                            'sha256':content,'id':ident,'meta':meta,
                            'album':str(directory.relative_to(root)) if album else None,'album_title':album})
    if console: console.progress('Analyse',scan_total,scan_total,scan_started,force=True)
    for items in sidecars.values():
        for j,d in items:
            if j not in used: issues.append({'file':str(j.relative_to(root)),'issue':'JSON sans média associé'})
    return {'root':str(root),'records':records,'issues':issues,
            'summary':{'media_matched':len(records),'unique_media':len({r['id'] for r in records}),
                       'albums':len({r['album'] for r in records if r['album']})},'version':1}

def tags(path):
    r = subprocess.run(['exiftool','-j','-G1','-s','-api','QuickTimeUTC=1',str(path)],
                       check=True,capture_output=True,text=True,env={**os.environ,'TZ':'UTC'})
    d=json.loads(r.stdout)[0]
    if 'ExifTool:Error' in d: raise RuntimeError(d['ExifTool:Error'])
    return d

def has(t, names):
    return any(k.split(':')[-1] in names and v not in ('',None,'0000:00:00 00:00:00') and not str(v).startswith('0000:') for k,v in t.items())

def additions(t, meta, suffix):
    out={}
    video=suffix in VIDEO
    if meta.get('taken') and not has(t,{'DateTimeOriginal','CreateDate','CreationDate','DateCreated'}):
        if video and suffix in {'.mp4','.mov','.m4v'}:
            out['QuickTime:CreateDate']=meta['taken']
        else:
            out['EXIF:DateTimeOriginal']=meta['taken']
            out['EXIF:OffsetTimeOriginal']='+00:00'
        out['XMP-exif:DateTimeOriginal']=meta['taken']+'+00:00'
    if meta.get('description') and not has(t,{'Description','ImageDescription','Caption-Abstract'}):
        out['XMP-dc:Description']=meta['description']
    # Preserve existing locations, including partial coordinates, rather than mixing pairs.
    if meta.get('gps') and not has(t,{'GPSLatitude','GPSLongitude','GPSCoordinates','GPSPosition'}):
        lat,lon=meta['gps']
        if video:
            if suffix in {'.mp4','.mov','.m4v'}:
                out['Keys:GPSCoordinates']=f'{lat:+.7f}{lon:+.7f}/'
            else: raise RuntimeError(f'Écriture GPS vidéo non prise en charge: {suffix}')
        else:
            out.update({'EXIF:GPSLatitude':str(abs(lat)), 'EXIF:GPSLatitudeRef':'N' if lat>=0 else 'S',
                        'EXIF:GPSLongitude':str(abs(lon)), 'EXIF:GPSLongitudeRef':'E' if lon>=0 else 'W'})
            if 'altitude' in meta:
                out['EXIF:GPSAltitude']=str(abs(meta['altitude']))
                out['EXIF:GPSAltitudeRef']='0' if meta['altitude']>=0 else '1'
    # Keep existing offset tags even if the capture date itself is absent.
    if has(t, {'OffsetTimeOriginal'}): out.pop('EXIF:OffsetTimeOriginal', None)
    return out

def prepare_one(r, work):
    source=Path(r['source'])
    dest=work/'prepared'/r['id']/source.name
    receipt=dest.parent/'receipt.json'
    saved=read_json(receipt) if receipt.exists() else {}
    if saved.get('prepared_name'): dest=dest.parent/saved['prepared_name']
    if receipt.exists() and dest.exists():
        saved=read_json(receipt)
        if saved['source_sha256']==r['sha256'] and saved['prepared_sha256']==digest(dest): return dest
        raise RuntimeError(f'Copie préparée modifiée: {dest}')
    if digest(source)!=r['sha256']: raise RuntimeError(f'Source modifiée: {source}')
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,dest)
    before=tags(dest)
    detected=before.get('File:FileTypeExtension','').lower()
    # Some Takeout files have a misleading extension (JPEG named .png).
    aliases={'jpg':{'.jpg','.jpeg'},'tif':{'.tif','.tiff'}}
    if detected and dest.suffix.lower() not in aliases.get(detected,{'.'+detected}):
        corrected=dest.with_suffix('.'+detected)
        dest.replace(corrected); dest=corrected
    changes=additions(before,r['meta'],dest.suffix.lower())
    if changes:
        args=['exiftool','-overwrite_original','-P','-api','QuickTimeUTC=1']
        args += [f'-{k}={v}' for k,v in changes.items()]
        result=subprocess.run(args+[str(dest)],capture_output=True,text=True,env={**os.environ,'TZ':'UTC'})
        if result.returncode or 'Warning:' in result.stderr or 'Error:' in result.stderr:
            raise RuntimeError(f'ExifTool: {dest.name}: {result.stderr.strip()}')
        after=tags(dest)
        # Read back every requested tag; never silently import a failed metadata write.
        for key in changes:
            found = [(k,v) for k,v in after.items() if k==key or (key.startswith('EXIF:') and k.split(':')[-1]==key.split(':')[-1])]
            if not found or all(v in ('',None) for k,v in found):
                raise RuntimeError(f'Métadonnée non écrite: {key} ({dest.name})')
            if key.endswith(('DateTimeOriginal','CreateDate')) and not any(str(v).startswith(changes[key][:19]) for k,v in found):
                raise RuntimeError(f'Date incorrecte après écriture: {key} ({dest.name})')
        # Existing values must remain intact (File/System/Composite are derived).
        for key,value in before.items():
            group=key.split(':')[0]
            if group not in {'System','File','Composite','ExifTool','SourceFile'} and key not in changes and not key.endswith(('Offset','Offsets','ByteCount','ByteCounts')):
                if key in after and after[key]!=value:
                    raise RuntimeError(f'Métadonnée existante modifiée: {key} ({dest.name})')
    save(receipt,{'source_sha256':r['sha256'],'prepared_sha256':digest(dest),'prepared_name':dest.name,'added':changes})
    return dest

def auth(client, token, expected):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession, Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ModuleNotFoundError as e:
        raise RuntimeError('Dépendances Google absentes. Activer le venv puis lancer : python -m pip install -r requirements.txt') from e
    config=read_json(client)
    if 'installed' not in config: raise RuntimeError('Client OAuth de type Application de bureau requis.')
    client_id=config['installed']['client_id']
    creds=Credentials.from_authorized_user_file(str(token),SCOPES) if token.exists() else None
    if creds and creds.client_id!=client_id: raise RuntimeError('Le jeton appartient à un autre client OAuth.')
    if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
    if not creds or not creds.valid:
        flow=InstalledAppFlow.from_client_secrets_file(str(client),SCOPES,autogenerate_code_verifier=True)
        creds=flow.run_local_server(host='127.0.0.1',port=0,access_type='offline',
                                    prompt='consent select_account',login_hint=expected,timeout_seconds=300)
    session=AuthorizedSession(creds)
    r=session.get('https://openidconnect.googleapis.com/v1/userinfo',timeout=30)
    r.raise_for_status(); user=r.json()
    if user.get('email','').casefold()!=expected.casefold() or user.get('email_verified') is not True:
        raise RuntimeError(f"Compte refusé: {user.get('email')!r}; attendu: {expected}. Aucun import effectué.")
    save(token,json.loads(creds.to_json()))
    return session,user,client_id

class MediaRejected(RuntimeError):
    """Explicit per-item rejection, safe to record and continue past."""
    pass


class Importer:
    def __init__(self,session,state_path,user,client_id,console=None):
        self.session=session; self.path=state_path
        self.console=console
        self.state=read_json(state_path) if state_path.exists() else {'albums':{},'media':{},'members':{},'pending':None}
        binding={'sub':user['sub'],'email':user['email'],'client_id':client_id}
        if self.state.get('account',binding)!=binding: raise RuntimeError('État de migration lié à un autre compte/client OAuth.')
        self.state['account']=binding
        self.state.setdefault('failed',{})
        if self.state.get('pending'): self.recover_membership()
        self.persist()
    def recover_membership(self):
        pending=self.state['pending']
        path=pending.get('path','')
        match=re.fullmatch(r'/albums/([^/]+):batchAddMediaItems',path)
        payload=pending.get('payload',{})
        ids=payload.get('mediaItemIds',[])
        # Only a membership update of known, already created media can be replayed.
        if (pending.get('operation')!='add_to_album' or not match
                or match[1] not in self.state['albums'].values()
                or set(payload)!={'mediaItemIds'} or not isinstance(ids,list)
                or not 1<=len(ids)<=50 or not all(isinstance(i,str) and i in self.state['media'].values() for i in ids)):
            raise RuntimeError('Création au résultat incertain. Voir pending dans state.json et README; arrêt pour éviter les doublons.')
        backup=self.path.with_name('state.before-membership-resume-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        save(backup,self.state)
        if self.console:
            self.console.message('Reprise d’un ajout interrompu à un album : médias déjà importés, aucun nouvel envoi.', 'warn')
        # Re-associate the SAME IDs; this does not upload or create media/albums.
        self.mutate(path,payload,'add_to_album')
        members=self.state['members'].setdefault(match[1],[])
        for ident in ids:
            if ident not in members: members.append(ident)
        self.committed()
        if self.console: self.console.message('Ajout à l’album confirmé ; reprise débloquée.', 'ok')

    def persist(self): save(self.path,self.state)
    def wait_retry(self,response,attempt):
        delay=min(300,30*2**attempt)+random.uniform(0,3)
        header=response.headers.get('Retry-After')
        if isinstance(header,str):
            try: requested=float(header)
            except ValueError:
                try: requested=parsedate_to_datetime(header).timestamp()-time.time()
                except (ValueError,TypeError,OverflowError): requested=0
            delay=max(delay,requested)
        if delay>900:
            raise RuntimeError('Google demande une attente supérieure à 15 minutes. Relancer plus tard et vérifier les quotas du projet Google Cloud.')
        if self.console:
            self.console.message(f'Limite Google 429 : nouvelle tentative {attempt+1}/5 dans {self.console.duration(delay)}. Avancement conservé.', 'warn')
        deadline=time.monotonic()+delay
        started=time.monotonic()
        while time.monotonic()<deadline:
            remaining=deadline-time.monotonic()
            if self.console:
                self.console.progress('Pause Google',int(time.monotonic()-started),int(delay)+1,started,
                                      f'reprise dans {int(remaining)+1}s')
            time.sleep(min(1,remaining))
        if self.console: self.console.clear()

    def mutate(self,path,payload,operation):
        for attempt in range(6):
            self.state['pending']={'operation':operation,'path':path,'payload':payload}
            self.persist()
            # Timeouts/5xx remain uncertain; only explicit 429 rejections are retried.
            r=self.session.post(BASE+path,json=payload,timeout=180)
            if r.status_code==429:
                self.committed()  # Server explicitly rejected this attempt.
                if attempt==5:
                    raise RuntimeError('Limite Google 429 persistante après 5 reprises. Relancer plus tard ; vérifier les quotas Photos Library API dans Google Cloud. Aucun élément confirmé ne sera renvoyé.')
                self.wait_retry(r,attempt)
                continue
            r.raise_for_status()
            return r.json()
    def committed(self): self.state['pending']=None; self.persist()
    def album(self,key,title):
        if key not in self.state['albums']:
            if self.console: self.console.message(f'Création de l’album : {title}')
            d=self.mutate('/albums',{'album':{'title':title[:500]}},'create_album')
            self.state['albums'][key]=d['id']; self.committed()
            if self.console: self.console.message(f'Album créé : {title}', 'ok')
        return self.state['albums'][key]
    def upload(self,r,path):
        if r['id'] in self.state['media']: return self.state['media'][r['id']]
        mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        size=path.stat().st_size
        # Stream raw bytes: constant memory, retries restart only this file.
        for attempt in range(6):
            with path.open('rb') as f:
                response=self.session.post(BASE+'/uploads',data=f,headers={
                    'Content-Type':'application/octet-stream','Content-Length':str(size),
                    'X-Goog-Upload-Protocol':'raw','X-Goog-Upload-Content-Type':mime},timeout=(30,600))
            if response.status_code==429:
                if attempt==5:
                    raise RuntimeError('Limite Google 429 persistante pendant l’envoi. Relancer plus tard ; avancement conservé.')
                self.wait_retry(response,attempt)
                continue
            response.raise_for_status()
            break
        item={'simpleMediaItem':{'uploadToken':response.text.strip(),'fileName':path.name}}
        description=r['meta'].get('description','')
        if len(description)>=1000: raise RuntimeError(f'Description trop longue: {path.name} (limite API <1000 caractères)')
        if description: item['description']=description
        # Include the stable migration key in the journal, never in the public description.
        d=self.mutate('/mediaItems:batchCreate',{'newMediaItems':[item]},'create_media:'+r['id'])
        result=d['newMediaItemResults'][0]
        status=result.get('status',{})
        if status.get('code',0)!=0:
            # INVALID_ARGUMENT / FAILED_PRECONDITION are media-specific rejections.
            # Quota, auth and server failures still stop the run for investigation.
            if status['code'] in (3,9):
                self.state['failed'][r['id']]={'source':r.get('source',str(path)),
                    'relative':r.get('relative',path.name),'prepared':str(path),
                    'status':status,'last_attempt':dt.datetime.now(dt.timezone.utc).isoformat()}
                self.committed()
                save(self.path.parent/'failed-media.json',self.state['failed'])
                raise MediaRejected(f"Média refusé : {path.name} — {status.get('message','')}")
            self.committed()
            raise RuntimeError(f"Import refusé pour {path.name}: {status}")
        ident=result['mediaItem']['id']
        self.state['media'][r['id']]=ident
        self.state['failed'].pop(r['id'],None)
        self.committed()
        if (self.path.parent/'failed-media.json').exists():
            save(self.path.parent/'failed-media.json',self.state['failed'])
        return ident
    def member(self,album,media):
        done=self.state['members'].setdefault(album,[])
        if media not in done:
            self.mutate(f'/albums/{album}:batchAddMediaItems',{'mediaItemIds':[media]},'add_to_album')
            done.append(media); self.committed()

def run(a, console=None):
    console = console or Console()
    root=Path(a.source).expanduser().resolve()
    work=Path(a.work).expanduser().resolve()
    if not root.is_dir(): raise RuntimeError(f'Dossier absent: {root}')
    if work==root or work.is_relative_to(root): raise RuntimeError('Le dossier de travail doit être hors du Takeout.')
    work.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(work,0o700)
    # Lock auto-released by the OS, including after crashes.
    import fcntl
    with (work/'migration.lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Une migration utilise déjà ce dossier de travail.')
        console.message('Google Photos · migration Takeout', 'title')
        console.message(f'Source : {root}')
        if a.account: console.message(f'Compte attendu : {a.account}')
        plan=scan(root,console); save(work/'scan.json',plan)
        summary=plan['summary']
        console.message(f"{summary['media_matched']} fichiers associés · {summary['unique_media']} médias distincts · {summary['albums']} albums", 'ok')
        console.message(f"Anomalies : {len(plan['issues'])} · rapport : {work/'scan.json'}", 'warn' if plan['issues'] else 'info')
        if a.command=='scan': return
        if not shutil.which('exiftool'): raise RuntimeError('ExifTool absent. Installation: brew install exiftool')
        if plan['issues'] and not a.allow_unmatched: raise RuntimeError('Anomalies dans scan.json. Corriger ou utiliser --allow-unmatched pour ignorer les médias non associés.')
        records=plan['records']
        if a.album: records=[r for r in records if a.album in (r['album'],r['album_title'])]
        if not records: raise RuntimeError('Aucun média sélectionné.')
        if a.limit:
            keep=list(dict.fromkeys(r['id'] for r in records))[:a.limit]
            records=[r for r in records if r['id'] in keep]
        # Prepare all selected files BEFORE any remote write.
        unique = {r['id']: r for r in records}
        prepared={}
        console.message(f'Préparation de {len(unique)} médias distincts', 'title')
        started=time.monotonic()
        for r in unique.values():
            console.progress('Préparation',len(prepared),len(unique),started,Path(r['source']).name)
            prepared[r['id']]=prepare_one(r,work)
            console.progress('Préparation',len(prepared),len(unique),started,Path(r['source']).name)
        if a.command=='prepare':
            console.message('Copies prêtes. Originaux conservés.', 'ok')
            return
        if not a.client: raise RuntimeError('--client client_secret.json requis pour OAuth.')
        console.message('Connexion Google et vérification du compte…', 'title')
        session,user,client_id=auth(Path(a.client).expanduser(),work/'token.json',a.account)
        console.message('Compte vérifié : '+user['email'], 'ok')
        importer=Importer(session,work/'state.json',user,client_id,console)
        albums = {r['album']: r['album_title'] for r in records if r['album']}
        previous = len(set(unique) & set(importer.state['media']))
        album_previous = len(set(albums) & set(importer.state['albums']))
        links = {(r['album'],r['id']) for r in records if r['album']}
        def linked(pair):
            album_id=importer.state['albums'].get(pair[0])
            media_id=importer.state['media'].get(pair[1])
            return bool(album_id and media_id and media_id in importer.state['members'].get(album_id,[]))
        existing_links = sum(linked(pair) for pair in links)
        console.message(f'Reprise : {previous}/{len(unique)} médias déjà importés · {len(unique)-previous} à envoyer')
        console.message(f'Albums : {album_previous} existants · {len(albums)-album_previous} à créer')
        console.message(f'Associations aux albums : {existing_links}/{len(links)} terminées · {len(links)-existing_links} à faire')
        # Each album is announced once. IDs and journal format remain unchanged.
        album_ids={}
        for key,title in albums.items():
            if key in importer.state['albums']:
                console.message(f'Album réutilisé : {title}', 'ok')
            album_ids[key]=importer.album(key,title)
        console.message('Import des médias', 'title')
        known_failed=set(unique) & set(importer.state['failed'])
        if known_failed and not getattr(a,'retry_failed',False):
            console.message(f'{len(known_failed)} médias déjà refusés mis de côté. Utiliser --retry-failed pour les réessayer.', 'warn')
        todo=[r for key,r in unique.items() if key not in importer.state['media']
              and (key not in known_failed or getattr(a,'retry_failed',False))]
        sent=0
        started=time.monotonic()
        for i,r in enumerate(todo):
            console.progress('Envoi',i,len(todo),started,Path(r['source']).name,force=i==0)
            try:
                importer.upload(r,prepared[r['id']])
                sent+=1
            except MediaRejected as e:
                console.message(str(e), 'warn')
                console.message('Échec enregistré ; poursuite avec le média suivant.', 'warn')
            console.progress('Envoi',i+1,len(todo),started,Path(r['source']).name)
        if not todo:
            console.message('Aucun nouveau média à envoyer (consulter les éventuels refus ci-dessous).')
        console.message('Classement dans les albums', 'title')
        pending_links=[pair for pair in sorted(links) if pair[1] in importer.state['media'] and not linked(pair)]
        by_album=collections.defaultdict(list)
        for album_key,media_key in pending_links: by_album[album_key].append(media_key)
        started=time.monotonic(); done=0
        for key,media_keys in by_album.items():
            console.message(f'Album « {albums[key]} » : {len(media_keys)} médias à ajouter')
            for media_key in media_keys:
                console.progress('Classement',done,len(pending_links),started,albums[key])
                importer.member(album_ids[key],importer.state['media'][media_key])
                done+=1
                console.progress('Classement',done,len(pending_links),started,albums[key])
            missing=sum(not linked(pair) for pair in links if pair[0]==key)
            console.message(f'Album incomplet : {albums[key]} ({missing} associations manquantes)' if missing
                            else f'Album terminé : {albums[key]}', 'warn' if missing else 'ok')
        if not pending_links: console.message('Tous les médias importés sont déjà classés.', 'ok')
        # Never call an album complete if one of its media was rejected.
        for key in albums.keys()-by_album.keys():
            missing=sum(not linked(pair) for pair in links if pair[0]==key)
            console.message(f'Album incomplet : {albums[key]} ({missing} associations manquantes)' if missing
                            else f'Album déjà complet : {albums[key]}', 'warn' if missing else 'ok')
        missing_media=len(set(unique)-set(importer.state['media']))
        missing_links=sum(not linked(pair) for pair in links)
        tone='warn' if missing_media or missing_links else 'ok'
        console.message(f'Bilan : {len(unique)-missing_media}/{len(unique)} médias importés · {len(albums)} albums · {len(links)-missing_links}/{len(links)} associations', tone)
        console.message(f'Cette exécution : {sent} médias envoyés · {len(albums)-album_previous} albums créés · {len(pending_links)} associations ajoutées')
        console.message(f'Durée totale : {console.duration(time.monotonic()-console.started)} · reste à faire : {missing_media} médias, {missing_links} associations', tone)
        if missing_media:
            console.message(f'Médias refusés : {work/"failed-media.json"}', 'warn')
            console.message('Pour réessayer les refus : relancer la même commande avec --retry-failed.', 'warn')
        console.message(f'Journal de reprise : {work/"state.json"}')
        return 2 if missing_media or missing_links else 0

def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['scan','prepare','import'])
    p.add_argument('--source',required=True,help='Répertoire Takeout à parcourir')
    p.add_argument('--work',default=str(Path.cwd()/'migration-work'))
    p.add_argument('--account',help='Adresse exacte du compte Google de destination (requise pour import)')
    p.add_argument('--client',help='JSON OAuth Google Application de bureau')
    p.add_argument('--album',help='Titre exact ou chemin relatif pour un essai')
    p.add_argument('--limit',type=int,help='Maximum de médias uniques (essai)')
    p.add_argument('--allow-unmatched',action='store_true',help='Ignorer explicitement les fichiers sans JSON certain')
    p.add_argument('--no-color',action='store_true',help='Désactiver les couleurs (aussi NO_COLOR=1)')
    p.add_argument('--retry-failed',action='store_true',help='Réessayer également les médias précédemment refusés')
    a=p.parse_args(argv)
    if a.limit is not None and a.limit<1: p.error('--limit doit être positif')
    if not a.source.strip(): p.error('--source ne peut pas être vide')
    if a.command=='import':
        if not a.account or not a.account.strip(): p.error('--account est requis pour import')
        if not a.client: p.error('--client est requis pour import')
        a.account=a.account.strip()
    return a

def main():
    a=parse_args()
    console=Console(color=False if a.no_color else None)
    try: return run(a,console) or 0
    except (Exception,KeyboardInterrupt) as e:
        console.clear()
        console.message('Interrompu par l’utilisateur.' if isinstance(e,KeyboardInterrupt) else f'ARRÊT : {e}', 'error')
        console.message('Relancer la même commande pour reprendre. Les étapes enregistrées seront réutilisées.', 'warn')
        console.message('Si state.json contient une opération pending, sa vérification est nécessaire avant de continuer.', 'warn')
        return 130 if isinstance(e,KeyboardInterrupt) else 1
    return 0

if __name__=='__main__': sys.exit(main())
