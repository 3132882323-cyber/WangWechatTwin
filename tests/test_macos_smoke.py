"""Real macOS CI core startup, no browser/WeChat access or model requests."""
import json
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import pytest
import yaml


@pytest.mark.skipif(sys.platform != 'darwin',reason='requires actual macOS runner')
def test_macos_backend_starts_and_stops_without_windows_dependencies(tmp_path):
    root=Path(__file__).resolve().parents[1]
    raw=yaml.safe_load((root/'config.macos.example.yaml').read_text(encoding='utf-8'))
    # No sender, input, model request, or public listener is introduced.
    raw['web']['port']=18791
    for key in ['persona','business_rules','reply_samples']:
        raw['paths'][key]=str(root/raw['paths'][key])
    config=tmp_path/'smoke.yaml';config.write_text(yaml.safe_dump(raw,allow_unicode=True),encoding='utf-8')
    with (tmp_path/'backend.log').open('wb') as log:
        child=subprocess.Popen([sys.executable,'-u','-m','app','--config',str(config),'run'],cwd=root,stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+20
            health=None
            while time.monotonic()<deadline and child.poll() is None:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:18791/health',timeout=1) as response:
                        health=json.load(response)
                    if health.get('runtime_ok') and health.get('workers',{}).get('status')=='running':
                        break
                except OSError:
                    pass
                time.sleep(.1)
            assert child.poll() is None,(tmp_path/'backend.log').read_text(errors='replace')
            assert health and health['runtime_ok'] and health['paused'] is False
            assert health['model_bridges_ok'] is False
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=5)
        assert child.returncode==0,(tmp_path/'backend.log').read_text(errors='replace')
