"""Builds the PR-mode demo for record.sh in the scratch folder $1: a git repo whose branch adds what examples/ holds,
and the review page for examples/review.md as a review of PR 4242. Prints the page's address."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(here))
work = Path(sys.argv[1])
repo = Path(os.environ['DEMO_REPO'])


def git(*a):
    return subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


(repo / 'examples/src').mkdir(parents=True)
git('init', '-q', '-b', 'main')
git('config', 'user.email', 'demo@example.com')
git('config', 'user.name', 'Demo')
git('remote', 'add', 'origin', 'https://github.com/acme/billing.git')
billing = (here / 'examples/src/billing.py').read_text().split('\n')
(repo / 'examples/src/billing.py').write_text('\n'.join(billing[:12] + billing[14:]))  # the base has no coupon
git('add', '-A')
git('commit', '-qm', 'Base')
git('update-ref', 'refs/remotes/origin/main', 'HEAD')
git('checkout', '-qb', 'coupons')
for f in ('src/billing.py', 'src/queue.py', 'review.md'):
    shutil.copy(here / 'examples' / f, repo / 'examples' / f)
git('add', '-A')
git('commit', '-qm', 'Add coupons and retries')

home = Path(os.environ['HOME'])
(home / '.claude/projects/demo').mkdir(parents=True)
(home / '.claude/projects/demo/demo-0001.jsonl').write_text(json.dumps(
    {'type': 'user', 'message': {'role': 'user', 'content': 'Review PR 4242'}, 'cwd': str(repo), 'sessionId': 'demo-0001'}) + '\n')

# The review session counts as open (a live process in ~/.claude/sessions), so posting can tell it what was posted.
(home / '.claude/sessions').mkdir(parents=True, exist_ok=True)
(home / f'.claude/sessions/{os.getppid()}.json').write_text(json.dumps(
    {'sessionId': 'demo-0001', 'pid': os.getppid(), 'cwd': str(repo), 'name': 'PR 4242 review', 'status': 'idle'}))

import hook, render, triage  # noqa: E402 — after sys.path and the environment are set

spec = hook.spec_from_report(repo / 'examples/review.md')
spec.update(pr=4242, session_id='demo-0001', out=str(render.store.html_path('demo')))
out = render.render(spec)
assert triage.ensure_server()
print(triage.page_url(out))
