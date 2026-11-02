"""使用隔离 Docker 替身验证构建失败、切换失败回滚和成功清理。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['MOCK_STATE']); s=json.loads(p.read_text()); a=sys.argv[1:]; code=0
s['calls'].append(a)
if a[:2]==['image','inspect']:
    code=0 if a[2] in s['images'] else 1
elif a[:2]==['container','inspect']:
    code=0 if a[2] in s['containers'] else 1
elif a[0]=='inspect':
    print('true' if 'Labels' in a[2] else s['containers'][a[-1]])
elif a[:2]==['image','ls']:
    print('')
elif a[0]=='build':
    s['images'].append(a[a.index('-t')+1]); code=1 if os.environ['SCENARIO']=='build_fail' else 0
elif a[0]=='run':
    if '--name' in a:
        name=a[a.index('--name')+1]; s['containers'][name]='apiserver:0.5.1'
        if name=='apiserver' and os.environ['SCENARIO']=='switch_fail': code=1
elif a[0]=='exec':
    code=1 if os.environ['SCENARIO']=='candidate_fail' else 0
elif a[0]=='rm':
    s['containers'].pop(a[-1],None)
elif a[0]=='rename':
    s['containers'][a[2]]=s['containers'].pop(a[1])
elif a[:2]==['image','rm']:
    if a[2] in s['images']: s['images'].remove(a[2])
p.write_text(json.dumps(s)); sys.exit(code)
'''


class DeployTests(unittest.TestCase):
    def run_scenario(self, scenario):
        """在项目临时目录运行部署脚本并收集隔离状态。"""
        with tempfile.TemporaryDirectory(dir=ROOT / 'runtime') as directory:
            base=Path(directory); bin_dir=base/'bin'; bin_dir.mkdir()
            for name, content in [('docker', MOCK), ('flock', '#!/bin/sh\nexit 0\n'), ('sleep', '#!/bin/sh\nexit 0\n')]:
                file=bin_dir/name; file.write_text(content); file.chmod(0o755)
            (base/'resources').mkdir()
            (base/'resources/apiserver-0.5.1.tar.gz').write_text('temporary archive')
            state=base/'state.json'
            state.write_text(json.dumps({'images':['python:3.10-slim-bullseye','apiserver:0.0.1'], 'containers':{'apiserver':'apiserver:0.0.1'},'calls':[]}))
            env_file=base/'.env'; env_file.write_text('DB_PASSWORD=test\n')
            env=os.environ | {'PATH':str(bin_dir)+':'+os.environ['PATH'],'MOCK_STATE':str(state),'SCENARIO':scenario,'RESOURCE_DIR':str(base/'resources'),'ENV_FILE':str(env_file)}
            result=subprocess.run(['bash',str(ROOT/'build.sh')],env=env,capture_output=True,text=True,errors="replace")
            state_data=json.loads(state.read_text())
            state_data['archive_exists']=(base/'resources/apiserver-0.5.1.tar.gz').exists()
            return result,state_data

    def test_failure_preserves_old(self):
        """三种失败均保留旧容器和镜像，并删除新镜像。"""
        for scenario in ('build_fail','candidate_fail','switch_fail'):
            with self.subTest(scenario=scenario):
                result,state=self.run_scenario(scenario)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertEqual(state['containers'],{'apiserver':'apiserver:0.0.1'})
                self.assertIn('apiserver:0.0.1',state['images'])
                self.assertNotIn('apiserver:0.5.1',state['images'])
                self.assertTrue(state['archive_exists'])

    def test_success_cleans_old(self):
        """成功后只保留新版本，并保留 Python 基础镜像。"""
        result,state=self.run_scenario('success')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(state['containers'],{'apiserver':'apiserver:0.5.1'})
        self.assertNotIn('apiserver:0.0.1',state['images'])
        self.assertIn('python:3.10-slim-bullseye',state['images'])
        self.assertFalse(state['archive_exists'])
        runs=[call for call in state['calls'] if call[0]=='run']
        for call in runs:
            self.assertEqual(call[call.index('--network')+1],'host')
            self.assertIn('http_proxy=http://127.0.0.1:1087',call)
            self.assertIn('NO_PROXY=localhost,127.0.0.1,::1,192.168.3.110',call)
            self.assertNotIn('-p',call)
        candidate=next(call for call in runs if '--no-healthcheck' in call)
        self.assertIn('18110',candidate)
        final=next(call for call in runs if '--restart' in call)
        self.assertEqual(final[final.index('--restart')+1],'always')


if __name__=='__main__':
    unittest.main()
