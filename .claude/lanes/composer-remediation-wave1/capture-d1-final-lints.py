from pathlib import Path
import hashlib,json,os,subprocess
root=Path('/workspace/elspeth/.claude/worktrees/composer-remediation-wave1')
lane=Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1')
expected='14fef5e5342ccbd79b4fdd69b276c20f77d66dc1'
def snapshot():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    assert head==expected
    status=subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True)
    assert status==''
    names=subprocess.check_output(['git','ls-files','-z'],cwd=root).split(b'\0')
    hashes={}
    for name in names:
        if not name: continue
        name=os.fsdecode(name)
        path=root/name
        if path.is_symlink():
            hashes[name]={'kind':'symlink','target':os.readlink(path)}
        else:
            with path.open('rb') as f:
                hashes[name]={'kind':'file','sha256':hashlib.file_digest(f,'sha256').hexdigest()}
    return {'head':head,'status':status,'files':hashes}
assert not os.environ.get('ELSPETH_JUDGE_METADATA_HMAC_KEY')
before=snapshot()
(lane/'d1-final-lints.before.json').write_text(json.dumps(before,sort_keys=True)+'\n')
env=dict(os.environ)
env['PYTHONPATH']=str(root/'src')+':'+str(root/'elspeth-lints/src')
env['ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE']='shape-only-when-key-missing'
cmd=[str(root/'.venv/bin/python'),'-m','elspeth_lints.core.cli','check','--rules','all','--root','src/elspeth','--format','json']
(lane/'d1-final-lints.command.json').write_text(json.dumps({'cwd':str(root),'command':cmd,'head':expected},indent=2)+'\n')
with (lane/'d1-final-lints.json').open('w') as out, (lane/'d1-final-lints.stderr').open('w') as err:
    result=subprocess.run(cmd,cwd=root,env=env,stdout=out,stderr=err)
(lane/'d1-final-lints.exit').write_text(str(result.returncode)+'\n')
after=snapshot()
(lane/'d1-final-lints.after.json').write_text(json.dumps(after,sort_keys=True)+'\n')
frozen=before==after
(lane/'d1-final-lints.frozen-exit').write_text('0\n' if frozen else '1\n')
assert frozen
assert result.returncode in (0,1),result.returncode
rows=json.loads((lane/'d1-final-lints.json').read_text())
assert isinstance(rows,list)
print(json.dumps({'lint_exit':result.returncode,'frozen':frozen,'finding_count':len(rows),'tracked_files_frozen':len(before['files'])}))
