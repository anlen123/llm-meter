import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from llm_meter_codex import read_codex_limits


class CodexQuotaTests(unittest.TestCase):
    def test_rpc_handshake_quota_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'codex'
            executable.write_text('''#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='initialize':
  assert r['params']['clientInfo']['name']=='llm_meter'
  print(json.dumps({'id':r['id'],'result':{}}),flush=True)
 elif r['method']=='initialized':pass
 elif r['method']=='account/rateLimits/read':
  print(json.dumps({'method':'account/updated','params':{'email':'private'}}),flush=True)
  print(json.dumps({'id':r['id'],'result':{'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':25,'resetsAt':123},'credits':{'balance':'12'}}},'secret':'private'}}),flush=True)
 else:raise RuntimeError('Unexpected RPC')
''')
            executable.chmod(0o700)
            with patch.dict(os.environ,{'PATH':folder+os.pathsep+os.environ['PATH']}):
                result=read_codex_limits(folder,timeout=2)
            self.assertEqual(result['rate_limits'][0]['primary']['usedPercent'],25)
            self.assertEqual(result['rate_limits'][0]['credits']['balance'],'12')
            self.assertTrue(result['live'])
            self.assertNotIn('private',str(result))

    def test_missing_cli_actionable_error(self):
        with patch('llm_meter_codex.subprocess.Popen',side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError,'Codex CLI'):read_codex_limits('/tmp')
