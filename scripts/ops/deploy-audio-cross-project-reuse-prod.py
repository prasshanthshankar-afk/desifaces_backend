"""Deploy only the main-sourced audio origin-project reuse fix to the live API."""
import copy
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAME = 'df-svc-audio'


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def engine(method, path, body=None):
    conn = UnixConnection('localhost', timeout=60)
    conn.request(method, '/v1.47'+path, body=json.dumps(body).encode() if body is not None else None, headers={'Content-Type':'application/json'})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    if response.status >= 300:
        raise RuntimeError('Docker API HTTP '+str(response.status))
    return json.loads(data) if data else None


def inspect(name):
    return json.loads(subprocess.check_output(['docker','inspect',name]))[0]


def snapshot():
    rows = subprocess.check_output(['docker','ps','--format','{{.Names}} {{.ID}} {{.Image}}'], text=True).splitlines()
    return sorted(x for x in rows if x.split()[0] != NAME and not x.split()[0].startswith(NAME+'-rollback-'))


def comparison(obj):
    config = copy.deepcopy(obj['Config'])
    for key in ['Image','Labels']:
        config.pop(key,None)
    return {'Config':config,'HostConfig':obj['HostConfig'],'Mounts':[{k:v for k,v in m.items() if k != 'Name'} for m in obj['Mounts']]}


def endpoints(obj):
    result = {}
    for name, net in obj['NetworkSettings']['Networks'].items():
        assert not net.get('IPAMConfig'), 'static network needs preservation review'
        result[name] = {'Aliases':[x for x in (net.get('Aliases') or []) if x not in [obj['Id'],obj['Id'][:12]]], 'Links':net.get('Links')}
    return result


def health():
    for _ in range(30):
        assert inspect(NAME)['State']['Running'], 'Audio API exited'
        result = subprocess.run(['docker','exec',NAME,'python','-c',"import urllib.request,json; r=json.load(urllib.request.urlopen('http://127.0.0.1:8004/api/health',timeout=3)); assert r['status']=='ok'"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError('Audio healthcheck timed out')


def main():
    assert socket.gethostname().split('.')[0] == 'desifaces-gpu'
    sha = sys.argv[1]
    assert len(sha) == 40 and all(x in '0123456789abcdef' for x in sha)
    source = Path(__file__).parent
    obj = inspect(NAME)
    assert obj['State']['Running']
    protected = snapshot()
    cfg_before = comparison(obj)
    network = endpoints(obj)
    assert len(network) == 1
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup = NAME+'-rollback-'+stamp
    runtime = Path('/home/azureuser/workspace/desifaces/.desifaces-deploy') / ('audio-cross-project-reuse-'+sha)
    runtime.mkdir(parents=True, exist_ok=True)
    rolled = False
    with tempfile.TemporaryDirectory(prefix='desifaces-audio-reuse-') as folder:
        root = Path(folder)
        subprocess.run(['docker','cp',NAME+':/app/app/api/routes/v3_audio_output.py',str(root/'original.py')],check=True,stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable,str(source/'build-audio-reuse-patch.py'),str(root/'original.py'),str(root/'v3_audio_output.py')],check=True)
        for filename in ['certify-audio-cross-project-reuse.py','test-audio-cross-project-reuse.py']:
            (root/filename).write_bytes((source/filename).read_bytes())
        baseline = 'desifaces-audio-reuse-baseline:'+stamp
        image = 'desifaces-audio-cross-project-reuse:'+sha
        subprocess.run(['docker','tag',obj['Image'],baseline],check=True)
        (root/'Dockerfile').write_text('FROM '+baseline+'\nCOPY v3_audio_output.py /app/app/api/routes/v3_audio_output.py\nCOPY certify-audio-cross-project-reuse.py test-audio-cross-project-reuse.py /opt/audio-reuse/\nLABEL ai.desifaces.audio-reuse-main="'+sha+'"\n')
        subprocess.run(['docker','build','-q','-t',image,str(root)],check=True)
        image_id = subprocess.check_output(['docker','image','inspect','-f','{{.Id}}',image],text=True).strip()
        values = obj['Config'].get('Env') or []
        assert all('\n' not in x and '\r' not in x for x in values)
        env_file = root/'runtime.env'
        env_file.write_text('\n'.join(values)+'\n')
        env_file.chmod(0o600)
        for test in ['test-audio-cross-project-reuse.py','certify-audio-cross-project-reuse.py']:
            subprocess.run(['docker','run','--rm','--network',next(iter(network)),'--env-file',str(env_file),'-e','AUDIO_REUSE_ROUTE=/app/app/api/routes/v3_audio_output.py','--entrypoint','python',image,'/opt/audio-reuse/'+test],check=True,timeout=60)
        assert snapshot() == protected, 'unrelated services changed before cutover'
        cfg = copy.deepcopy(obj['Config'])
        cfg['Image'] = image
        cfg['Labels'] = dict(cfg.get('Labels') or {})
        cfg['Labels']['ai.desifaces.audio-reuse-main'] = sha
        service = cfg['Labels'].get('com.docker.compose.service')
        assert service, 'compose service identity missing'
        override = runtime/'image.yml'
        override.write_text('services:\n  '+service+':\n    image: '+image+'\n')
        prior = cfg['Labels'].get('com.docker.compose.project.config_files','')
        assert prior, 'compose provenance missing'
        cfg['Labels']['com.docker.compose.project.config_files'] = prior+','+str(override)
        body = {**cfg,'HostConfig':obj['HostConfig'],'NetworkingConfig':{'EndpointsConfig':network}}
        try:
            subprocess.run(['docker','stop',NAME],check=True,stdout=subprocess.DEVNULL)
            subprocess.run(['docker','rename',NAME,backup],check=True)
            rolled = True
            subprocess.run(['docker','update','--restart=no',backup],check=True,stdout=subprocess.DEVNULL)
            engine('POST','/containers/create?name='+NAME,body)
            engine('POST','/containers/'+NAME+'/start')
            health()
            active = inspect(NAME)
            assert active['Image'] == image_id
            assert comparison(active) == cfg_before, 'live configuration drift'
            assert endpoints(active) == network, 'network alias drift'
            assert snapshot() == protected, 'unrelated production service changed'
            subprocess.run(['docker','exec',NAME,'python','/opt/audio-reuse/certify-audio-cross-project-reuse.py'],check=True,timeout=60)
            (runtime/'receipt.json').write_text(json.dumps({'source_branch':'main','backend_main':sha,'image':image,'image_id':image_id,'rollback_container':backup,'compose_override':str(override),'original_sha256':hashlib.sha256((root/'original.py').read_bytes()).hexdigest(),'patched_sha256':hashlib.sha256((root/'v3_audio_output.py').read_bytes()).hexdigest()},indent=2))
            print('AUDIO_API_CONFIGURATION_PRESERVED=PASS')
            print('GENERATION_PRICING_AND_OTHER_SERVICES_PRESERVED=PASS')
            print('backend_source_branch=main')
            print('backend_source_commit='+sha)
            print('rollback_container='+backup)
            print('AUDIO_CROSS_PROJECT_REUSE_PRODUCTION_DEPLOYMENT=PASS')
        except BaseException:
            if rolled:
                subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                subprocess.run(['docker','rename',backup,NAME],check=True)
                policy = obj['HostConfig']['RestartPolicy']
                restart = policy['Name']+(':'+str(policy['MaximumRetryCount']) if policy['Name']=='on-failure' and policy['MaximumRetryCount'] else '')
                subprocess.run(['docker','update','--restart='+restart,NAME],check=True,stdout=subprocess.DEVNULL)
            subprocess.run(['docker','start',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            print('AUDIO_API_ROLLBACK=COMPLETE',flush=True)
            raise


if __name__ == '__main__':
    main()

