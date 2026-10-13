"""Guarded DEV-only speech rollout with original containers and policy rollback."""
import copy
import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('engine', ROOT / 'deploy-audio-cross-project-reuse-dev.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

TARGETS = ('df-svc-audio', 'df-svc-audio-worker')
# Exact source hashes, allowing only pre-existing terminal-newline packaging variants.
ORIGINAL = {"/app/app/services/tts_routing_preference.py": ["ef874fed5ad81c65ba6db92d83ed59d027b7c23bf288098cb60ec4fbd1329cc6"], "/app/app/repos/tts_catalog_repo.py": ["e80754dd63512ecb0d69a2810fa5c045c65f267b734d8235b5d0bbc9b31d9385"], "/app/app/api/routes/catalog.py": ["2a9f8dd8cbdedc5394d11401103e64ea8512f1b1461cab5c55ce9c0ba61fa644"], "/app/app/services/tts_service.py": ["e215ee010c3b8e95d5a52079b77041501b7b29cf1dee32972d494589e53cbb0c"], "/app/app/services/tts_intent_policy.py": ["e1eb0092e85f012952fdd03a669e899292efd568534fafbb5719ce85d3f43e50"], "/app/app/services/multi_person_pricing_policy.py": ["4ae8a3609f6bcdfbf84be49b2323ff1f67c3594288476a6d3973f0fbcfd5cc22"], "/app/desifaces_shared/pricing/multi_person.py": ["ad2b7a333c4f371a2f44cd6dd6d95b9fb475b7589e22baf1bbf62f556bb910be"], "/app/app/services/tts_orchestrator.py": ["8f13c526533c2f9abf0f1fd1058769fdc0d0d7988d963eeb4389886c14c4ecc5"], "/app/app/services/gender_translation.py": ["e76f48c98feebc79281383e0c935507cfe5212c7c51fb05e3c604186c8895368"], "/app/app/llm.py": ["279d790121162497713c1ddf907337de236c08649e3bf34f26e3b5048016dddb"], "/app/app/audio_execution.py": ["69b3f9f8a3449138992a9cfd5a34586479b30d23a8f6165b68b5139cf5041025"]}


def call(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def protected():
    return sorted(line for line in subprocess.check_output(['docker','ps','--format','{{.Names}} {{.ID}} {{.Image}}'], text=True).splitlines()
        if line.split()[0] not in TARGETS and not any(line.split()[0].startswith(name+'-rollback-') for name in TARGETS))


def wait_ready(name):
    for _ in range(25):
        if not engine.inspect(name)['State']['Running']:
            raise RuntimeError(name+' exited')
        if name.endswith('-worker'):
            time.sleep(2)
            assert engine.inspect(name)['State']['Running']
            return
        port = 8004 if name == 'df-svc-audio' else 8011
        script = "import json,urllib.request; x=json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/health',timeout=3)); assert x.get('status')=='ok' or x.get('ok') is True" % port
        if subprocess.run(['docker','exec',name,'python','-c',script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError(name+' health timeout')


DB_TOOL = r'''
import asyncio,json,os,sys
import asyncpg
async def run():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'))
 try:
  mode=sys.argv[1]
  if mode=='snapshot':
   async with c.transaction(readonly=True):
    active=await c.fetchval("select count(*) from public.studio_jobs where studio_type='audio' and status in ('pending','queued','running','processing','generating')")
    if active: raise RuntimeError('audio generations in flight; rollout deferred')
    rows=await c.fetch("select policy_code,meta_json from public.tts_routing_policies where is_default and is_enabled")
    revision=await c.fetchval("select revision from public.masterdata_revision where domain='tts'")
   print(json.dumps({'rows':[dict(r) for r in rows], 'revision':revision}))
  elif mode=='restore':
   original=json.loads(sys.stdin.read())
   async with c.transaction():
    for row in original['rows']:
     raw=row['meta_json']; raw=raw if isinstance(raw,str) else json.dumps(raw)
     await c.execute('update public.tts_routing_policies set meta_json=$2::jsonb where policy_code=$1',row['policy_code'],raw)
    await c.execute("update public.masterdata_revision set revision=revision+1 where domain='tts'")
  elif mode=='apply':
   await c.execute(sys.stdin.read())
 finally: await c.close()
asyncio.run(run())
'''


def database(mode, data=None):
    return subprocess.check_output(['docker','exec','-i','df-svc-audio','python','-c',DB_TOOL,mode], input=data, text=True)


def main():
    assert socket.gethostname().split('.')[0] == 'desifaces-dev', 'DEV host required'
    sha = sys.argv[1]
    assert len(sha)==40 and all(c in '0123456789abcdef' for c in sha)
    bundle = json.loads((ROOT/'indian-speech-bundle.json').read_text())
    assert bundle['source_sha'] == sha
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    receipt_dir = Path('/home/azureuser/workspace/desifaces-runtime/.desifaces-deploy') / ('indian-speech-'+sha+'-'+stamp)
    receipt_dir.mkdir(parents=True, exist_ok=False)
    receipt_dir.chmod(0o700)
    original = {name: engine.inspect(name) for name in TARGETS}
    before = protected()
    snapshot = database('snapshot')
    (receipt_dir/'policy-backup.json').write_text(snapshot)
    (receipt_dir/'policy-backup.json').chmod(0o600)
    assert json.loads((receipt_dir/'policy-backup.json').read_text()) == json.loads(snapshot)
    images, backups = {}, []
    applied = False
    with tempfile.TemporaryDirectory(prefix='desifaces-indian-speech-') as folder:
        work = Path(folder)
        for name, obj in original.items():
            is_audio = name.startswith('df-svc-audio')
            files = bundle['audio'] if is_audio else bundle['director']
            files = [item for item in files if not item.get('only_containers') or name in item['only_containers']]
            replacements = {item['destination'] for item in files}
            for change in subprocess.check_output(['docker','diff',name], text=True).splitlines():
                if '.py' in change and not '.pyc' in change:
                    changed_path = change[2:]
                    assert changed_path in replacements, name+' has an unpreserved runtime source overlay: '+changed_path
            root = work/name
            root.mkdir()
            baseline = 'desifaces-speech-baseline-'+name+':'+stamp
            call('docker','tag',obj['Image'],baseline)
            dockerfile = 'FROM '+baseline+'\n'
            for index, item in enumerate(files):
                destination = item['destination']
                if destination in ORIGINAL:
                    live = subprocess.check_output(['docker','exec',name,'cat',destination])
                    assert hashlib.sha256(live).hexdigest() in ORIGINAL[destination], name+' source drift: '+destination
                local = 'source-'+str(index)+'.py'
                (root/local).write_text(item['content'])
                dockerfile += 'COPY '+local+' '+destination+'\n'
            if is_audio:
                for label, text in bundle['tests'].items():
                    (root/label).write_text(text)
                    dockerfile += 'COPY '+label+' /opt/indian-speech/'+label+'\n'
            image = 'desifaces-indian-speech-'+name+':'+sha
            (root/'Dockerfile').write_text(dockerfile+'LABEL ai.desifaces.indian-speech-candidate="'+sha+'"\n')
            call('docker','build','-q','-t',image,str(root))
            images[name] = image
        audio_obj = original['df-svc-audio']
        env_file = work/'audio.env'
        env_file.write_text('\n'.join(audio_obj['Config']['Env'])+'\n')
        env_file.chmod(0o600)
        image = images['df-svc-audio']
        for test in ('test_sarvam_style_compatibility.py','test_natural_speech_translation.py','test-audio-cross-project-reuse.py'):
            call('docker','run','--rm','--network','none','--env-file',str(env_file),'-e','DF_TRANSLATION_TEST_MODULE=/app/app/services/gender_translation.py','-e','AUDIO_REUSE_ROUTE=/app/app/api/routes/v3_audio_output.py','--entrypoint','python',image,'/opt/indian-speech/'+test)
        network = next(iter(engine.endpoints(audio_obj)))
        call('docker','run','--rm','--network',network,'--env-file',str(env_file),'--entrypoint','python',image,'/opt/indian-speech/certify-indian-speech-dev.py','active',timeout=90)
        assert database('snapshot') == snapshot, 'preflight changed stored policy'
        assert protected() == before, 'unrelated services changed'
        assert database('snapshot') == snapshot, 'routing policy changed'
        try:
            # Stop workers first, preserving their exact runtime configuration.
            for name in ('df-svc-audio-worker','df-svc-audio'):
                obj = original[name]
                cfg = copy.deepcopy(obj['Config'])
                cfg['Image'] = images[name]
                cfg['Labels'] = dict(cfg.get('Labels') or {})
                cfg['Labels']['ai.desifaces.indian-speech-candidate'] = sha
                service = cfg['Labels'].get('com.docker.compose.service')
                assert service, 'compose identity missing'
                override = receipt_dir/(name+'-image.yml')
                override.write_text('services:\n  '+service+':\n    image: '+images[name]+'\n')
                prior = cfg['Labels'].get('com.docker.compose.project.config_files','')
                assert prior
                cfg['Labels']['com.docker.compose.project.config_files'] = prior+','+str(override)
                body = {**cfg,'HostConfig':obj['HostConfig'],'NetworkingConfig':{'EndpointsConfig':engine.endpoints(obj)}}
                backup = name+'-rollback-'+stamp
                call('docker','stop',name,stdout=subprocess.DEVNULL)
                call('docker','rename',name,backup)
                backups.append((name,backup))
                call('docker','update','--restart=no',backup,stdout=subprocess.DEVNULL)
                engine.engine('POST','/containers/create?name='+name,body)
            database_before = json.loads(snapshot)
            # Start API so policy application uses the candidate's unchanged DB configuration.
            for name in ('df-svc-audio',):
                engine.engine('POST','/containers/'+name+'/start')
                wait_ready(name)
            assert database('snapshot') == snapshot, 'context-only rollout must not change routing policy'
            call('docker','exec','df-svc-audio','python','/opt/indian-speech/certify-indian-speech-dev.py','active',timeout=90)
            call('docker','exec','df-svc-audio','python','/opt/indian-speech/certify-audio-cross-project-reuse.py',timeout=90)
            for name in ('df-svc-audio-worker',):
                engine.engine('POST','/containers/'+name+'/start')
                wait_ready(name)
            for name,obj in original.items():
                live = engine.inspect(name)
                assert engine.comparison(live) == engine.comparison(obj), name+' configuration drift'
                assert engine.endpoints(live) == engine.endpoints(obj), name+' network drift'
            assert protected() == before
            (receipt_dir/'receipt.json').write_text(json.dumps({'source_branch':'codex/indian-speech-dev-20261013','source_sha':sha,'images':images,'rollback_containers':dict(backups),'policy_backup_sha256':hashlib.sha256(snapshot.encode()).hexdigest()},indent=2))
            for name in ('df-svc-audio','df-svc-audio-worker'):
                call('docker','exec',name,'python','-c',"from app.services.tts_service import translate_with_gender; import inspect; assert inspect.getfile(translate_with_gender)=='/app/app/services/gender_translation.py'; print('IMAGE_OWNED_TRANSLATION_IMPORT=PASS')")
            assert database('snapshot') == snapshot, 'context rollout changed routing policy'
            print('DEV_RUNTIME_CONFIG_AND_UNRELATED_SERVICES_PRESERVED=PASS')
            print('INDIAN_SPEECH_DEVELOPMENT_DEPLOYMENT=PASS')
        except BaseException:
            if applied:
                database('restore',snapshot)
            for name,backup in reversed(backups):
                subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                call('docker','rename',backup,name)
                policy = original[name]['HostConfig']['RestartPolicy']
                restart = policy['Name']+(':'+str(policy['MaximumRetryCount']) if policy['Name']=='on-failure' and policy['MaximumRetryCount'] else '')
                call('docker','update','--restart='+restart,name,stdout=subprocess.DEVNULL)
                call('docker','start',name,stdout=subprocess.DEVNULL)
            print('INDIAN_SPEECH_DEVELOPMENT_ROLLBACK=COMPLETE')
            raise


if __name__ == '__main__':
    main()
