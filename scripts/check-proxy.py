"""Opt-in TLS/auth check with a temporary unprivileged nginx process.

Requires nginx and openssl. Binds loopback only; never changes installed services.
Pass --nginx PATH to use an extracted binary without system installation.
"""
from contextlib import ExitStack
import argparse
import base64
import http.client
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.http import HubServer
from hub.runtime import Fleet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx', default='nginx')
    args = parser.parse_args()
    fleet = Fleet([])
    server = HubServer(('127.0.0.1', 0), fleet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='platmon-proxy-')))
            root.chmod(0o755)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
                '-keyout', str(root/'server.key'), '-out', str(root/'server.crt')], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            hashed = subprocess.check_output(['openssl', 'passwd', '-6', '-stdin'], input='temporary-test-password\n', text=True).strip()
            (root/'viewers.htpasswd').write_text('viewer:'+hashed+'\n', encoding='utf-8')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            template = (Path(__file__).resolve().parents[1]/'deploy/nginx.conf.example').read_text(encoding='utf-8')
            template = template.replace('127.0.0.1:8443', '127.0.0.1:'+str(port)).replace('127.0.0.1:9787', '127.0.0.1:'+str(server.server_port))
            template = template.replace('/etc/platmon-proxy/', str(root)+'/').replace('/var/log/nginx/platmon-access.log', str(root/'access.log'))
            config = root/'nginx.conf'
            config.write_text('pid '+str(root/'nginx.pid')+';\nerror_log stderr crit;\nevents {}\nhttp {\n'
                'client_body_temp_path '+str(root/'body')+';\nproxy_temp_path '+str(root/'proxy')+';\n'
                'fastcgi_temp_path '+str(root/'fastcgi')+';\nuwsgi_temp_path '+str(root/'uwsgi')+';\n'
                'scgi_temp_path '+str(root/'scgi')+';\n'+template+'\n}\n', encoding='utf-8')
            command = [args.nginx, '-p', str(root), '-c', str(config), '-e', 'stderr']
            subprocess.run(command+['-t'], check=True)
            process = subprocess.Popen(command+['-g', 'daemon off;'])
            def stop_proxy():
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            cleanup.callback(stop_proxy)
            context = ssl.create_default_context(cafile=str(root/'server.crt'))
            def request(method='GET', auth=None):
                client = http.client.HTTPSConnection('127.0.0.1', port, context=context, timeout=2)
                try:
                    headers = {'Host':'hub.example.com'}
                    if auth is not None: headers['Authorization'] = 'Basic '+base64.b64encode(auth.encode()).decode()
                    client.request(method, '/api/nodes', headers=headers)
                    response = client.getresponse()
                    body = response.read()
                    return response.status, body
                finally: client.close()
            deadline = time.monotonic()+15
            while True:
                try:
                    assert request()[0] == 401
                    break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline: raise
                    time.sleep(.1)
            assert request(auth='viewer:wrong')[0] == 401
            status, body = request(auth='viewer:temporary-test-password')
            assert status == 200 and json.loads(body)['nodes'] == []
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                assert request(method, 'viewer:temporary-test-password')[0] == 405
            audit = (root/'access.log').read_text(encoding='utf-8')
            assert '401' in audit and '405' in audit and '200' in audit
            assert 'temporary-test-password' not in audit and 'Authorization' not in audit
            print('TLS verified; anonymous/invalid credentials 401; viewer read 200; writes 405; status-only audit verified.')
    finally:
        server.shutdown(); server.server_close(); thread.join(); fleet.close()


if __name__ == '__main__': main()
