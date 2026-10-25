import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Optional

import paths
import render
import rereview
import reviews
import store

OUT = support.OUT
# Page names nobody else uses: a creation stamp, then a slug. Pages 1 to 8 of the migration tests, K, and J.
A, B, C, D, E, G, H, I = (f'20261020-0900{n:02d}-flat-page-{n:02d}' for n in range(1, 9))
K = '20261020-090009-flat-page-09'
J = '20261020-120001-stage-page-01'
A_FILES = {
    'decisions.json': '{"findings": {"C1": {"status": "post"}}}',
    'chat.json': '{"updated": 1, "messages": []}',
    'freshness.json': '{"ref": "abc123", "status": "current"}',
    'drafts.json': '{"state": "ready"}',
    'rereview.json': '{"status": "done"}',
}
COMMITTING = [{'put': f'{D}.decisions.json.staged-ab12', 'to': f'{D}.decisions.json'},
              {'drop': f'{D}.freshness.json'},
              {'put': f'{D}.html.staged-ab12', 'to': f'{D}.html'}]


def flat_html(data: Optional[dict]) -> str:
    """A page as older versions wrote it: the review data in a script block, with '<' escaped; None gives a very old
    page without any data block."""
    if data is None:  # a very old page, without a data block
        return '<!doctype html><title>old</title><p>no data</p>\n'
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    return f'<!doctype html><title>t</title>\n<script type="application/json" id="data">{payload}</script>\n'


def owned(name: str, pages) -> bool:
    """Whether a file name in out/ is one of these pages' flat files or its folder."""
    return any(name == page or name.startswith(page + '.') for page in pages)


def forget(*pages: str) -> None:
    """Remove what a test left in out/ for these pages: their folders and their flat files."""
    for name in os.listdir(OUT):
        if owned(name, pages):
            if (OUT / name).is_dir():
                shutil.rmtree(OUT / name, True)
            else:
                (OUT / name).unlink(missing_ok=True)


def snapshot(*pages: str) -> dict:
    """What out/ holds for these pages, and latest.html: {path under out/: bytes, 'dir', or 'link->target'}."""
    shot = {}
    for name in sorted(os.listdir(OUT)):
        if name != 'latest.html' and not owned(name, pages):
            continue
        top = OUT / name
        if top.is_symlink():
            shot[name] = 'link->' + os.readlink(top)
        elif top.is_dir():
            for root, dirs, files in os.walk(top):
                for d in dirs:
                    shot[os.path.relpath(os.path.join(root, d), OUT)] = 'dir'
                for f in files:
                    path = os.path.join(root, f)
                    shot[os.path.relpath(path, OUT)] = Path(path).read_bytes()
        else:
            shot[name] = top.read_bytes()
    return shot


def keep_latest(test: unittest.TestCase) -> None:
    """Put out/latest.html back as it was when the test ends (a link, a file, or nothing)."""
    latest = OUT / 'latest.html'
    if latest.is_symlink():
        saved = ('link', os.readlink(latest))
    elif latest.exists():
        saved = ('file', latest.read_bytes())
    else:
        saved = None

    def restore() -> None:
        latest.unlink(missing_ok=True)
        if saved and saved[0] == 'link':
            latest.symlink_to(saved[1])
        elif saved:
            latest.write_bytes(saved[1])

    test.addCleanup(restore)


def put(name: str, text: str) -> Path:
    path = OUT / name
    path.write_text(text, encoding='utf-8')
    return path


class MigrateLayoutTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(forget, A, B, C, D, E, G, H, I, K)
        keep_latest(self)

    def test_three_flat_pages_get_their_own_folder(self):
        data_a = support.sample_page(A, title='Code review <b>A</b> · PR #4242')
        html_a = flat_html(data_a)
        put(f'{A}.html', html_a)
        for name, text in A_FILES.items():
            put(f'{A}.{name}', text)
        put(f'{A}.chat.lock', '')
        put(f'{A}.drafts.lock', '')
        put(f'{A}.rereview.lock', '')
        put(f'{A}.chat.tmp', '{"half": ')
        put(f'{B}.html', flat_html(support.sample_page(B)))
        put(f'{C}.html', flat_html(None))
        put(f'{C}.decisions.json', '{"findings": {}}')
        latest = OUT / 'latest.html'
        latest.unlink(missing_ok=True)
        latest.symlink_to(OUT / f'{A}.html')

        line = paths.migrate_layout()

        self.assertTrue(line.startswith('gave 3 review pages'), line)
        folder = OUT / A
        self.assertEqual(json.loads((folder / 'review.json').read_text(encoding='utf-8')), data_a)
        self.assertEqual((folder / 'page.html').read_bytes(), html_a.encode('utf-8'))
        for name, text in A_FILES.items():
            self.assertEqual((folder / name).read_bytes(), text.encode('utf-8'), name)
        for lock in ('chat.lock', 'drafts.lock', 'rereview.lock'):
            self.assertTrue((folder / 'locks' / lock).is_file(), lock)
        self.assertFalse((folder / 'chat.tmp').exists())
        self.assertFalse((OUT / f'{A}.chat.tmp').exists())
        self.assertTrue((OUT / B / 'review.json').is_file())
        self.assertTrue((OUT / B / 'page.html').is_file())
        self.assertTrue((OUT / C / 'page.html').is_file())
        self.assertFalse((OUT / C / 'review.json').exists())
        self.assertTrue((OUT / C / 'decisions.json').is_file())
        self.assertEqual([n for n in os.listdir(OUT) if any(n.startswith(p + '.') for p in (A, B, C))], [])
        self.assertEqual(os.readlink(latest), str(OUT / A / 'page.html'))
        self.assertEqual(store.page_data(A)['title'], data_a['title'])
        self.assertTrue(store.exists(B))
        self.assertTrue({A, B, C} <= set(store.pages()))

        before = snapshot(A, B, C)
        self.assertIsNone(paths.migrate_layout())
        self.assertEqual(snapshot(A, B, C), before)

    def test_a_rereview_that_died_while_committing_is_finished_first(self):
        put(f'{D}.html', flat_html(support.sample_page(D, ref='old')))
        put(f'{D}.decisions.json', '{"findings": {}}')
        put(f'{D}.freshness.json', '{"ref": "old"}')
        put(f'{D}.decisions.json.staged-ab12', '{"findings": {"C1": {"status": "skip"}}}')
        put(f'{D}.html.staged-ab12', flat_html(support.sample_page(D, ref='new')))
        put(f'{D}.chat.json.staged-ff00', '{"stray": true}')
        put(f'{D}.rereview.json', json.dumps({'status': 'reviewing', 'job': 'ab12', 'committing': COMMITTING}))

        paths.migrate_layout()

        folder = OUT / D
        self.assertEqual(json.loads((folder / 'decisions.json').read_text(encoding='utf-8')),
                         {'findings': {'C1': {'status': 'skip'}}})
        self.assertEqual(json.loads((folder / 'review.json').read_text(encoding='utf-8'))['ref'], 'new')
        self.assertFalse((folder / 'freshness.json').exists())
        staged = [os.path.join(root, n) for root, _, names in os.walk(OUT) for n in names if '.staged-' in n]
        self.assertEqual(staged, [])
        self.assertEqual(json.loads((folder / 'rereview.json').read_text(encoding='utf-8'))['committing'], COMMITTING)

    def test_nothing_to_do_when_every_page_has_a_folder(self):
        support.write_page(E)
        before = snapshot(E)
        self.assertIsNone(paths.migrate_layout())
        self.assertEqual(snapshot(E), before)

    def test_a_page_whose_data_block_does_not_parse_still_gets_its_folder(self):
        put(f'{G}.html', '<!doctype html><title>t</title>\n'
                         '<script type="application/json" id="data">{bad json</script>\n')
        put(f'{H}.html', flat_html(support.sample_page(H)))

        line = paths.migrate_layout()

        self.assertTrue(line.startswith('gave 2 review pages'), line)
        self.assertNotIn('could not move', line)
        self.assertTrue((OUT / G / 'page.html').is_file())
        self.assertFalse((OUT / G / 'review.json').exists())
        self.assertEqual(json.loads((OUT / H / 'review.json').read_text(encoding='utf-8')), support.sample_page(H))
        self.assertIsNone(paths.migrate_layout())

    def test_a_page_whose_re_review_commit_cannot_finish_keeps_its_files_flat(self):
        put(f'{K}.html', flat_html(support.sample_page(K)))
        put(f'{K}.decisions.json.staged-ab12', '{"findings": {"C1": {"status": "skip"}}}')
        put(f'{K}.rereview.json', json.dumps({'status': 'reviewing', 'committing': [
            {'put': f'{K}.decisions.json.staged-ab12', 'to': 'no-such-folder/x.json'}]}))
        latest = OUT / 'latest.html'
        latest.unlink(missing_ok=True)
        latest.symlink_to(OUT / f'{K}.html')

        line = paths.migrate_layout()

        self.assertIn('could not move', line)
        self.assertIn(K, line)
        self.assertTrue((OUT / f'{K}.html').is_file())
        self.assertTrue((OUT / f'{K}.rereview.json').is_file())
        self.assertTrue((OUT / f'{K}.decisions.json.staged-ab12').is_file())
        self.assertFalse((OUT / K).exists())
        self.assertEqual(os.readlink(latest), str(OUT / f'{K}.html'))
        self.assertIn('could not move', paths.migrate_layout())  # the next run tries again

    def test_leftover_files_of_a_deleted_page_are_folded_and_not_counted(self):
        put(f'{I}.freshness.json', '{"ref": "abc123"}')

        line = paths.migrate_layout()

        self.assertTrue(line.startswith('gave 0 review pages'), line)
        self.assertIn('leftover files of 1 deleted page', line)
        self.assertEqual((OUT / I / 'freshness.json').read_text(encoding='utf-8'), '{"ref": "abc123"}')
        self.assertFalse(store.exists(I))
        self.assertNotIn(I, store.pages())
        self.assertIsNone(paths.migrate_layout())


class DeleteReviewTest(unittest.TestCase):
    def setUp(self):
        self.older = '20261020-100001-del-page-01'
        self.newer = '20261020-100002-del-page-02'
        self.addCleanup(forget, self.older, self.newer)
        keep_latest(self)
        support.write_page(self.older)
        support.write_page(self.newer)
        latest = OUT / 'latest.html'
        latest.unlink(missing_ok=True)
        latest.symlink_to(OUT / self.newer / 'page.html')

    def test_delete_removes_the_folder_and_repoints_latest(self):
        self.assertTrue(reviews.delete_review(self.newer))
        self.assertFalse((OUT / self.newer).exists())
        # Other tests may have pages newer than this one, so the newest is whatever is greatest now.
        newest = max(store.pages(), default=None)
        self.assertIsNotNone(newest)
        self.assertEqual(os.readlink(OUT / 'latest.html'), str(OUT / newest / 'page.html'))
        self.assertFalse(reviews.delete_review(self.newer))


class RereviewCommitTest(unittest.TestCase):
    def setUp(self):
        self.page = '20261020-110001-commit-page-01'
        self.folder = OUT / self.page
        self.addCleanup(forget, self.page)
        support.write_page(self.page)

    def test_commit_puts_staged_files_in_place_and_removes_the_staging_folder(self):
        folder = self.folder
        staging = folder / '.staging-cd34'
        staging.mkdir()
        (staging / 'decisions.json').write_text(json.dumps({'findings': {'W1': {'status': 'post'}}}), encoding='utf-8')
        (staging / 'review.json').write_text(json.dumps(support.sample_page(self.page, ref='new'), ensure_ascii=False),
                                             encoding='utf-8')
        (folder / 'freshness.json').write_text('{"ref": "old"}', encoding='utf-8')
        st = {'status': 'reviewing', 'to': 'new',
              'committing': [{'put': '.staging-cd34/decisions.json', 'to': 'decisions.json'},
                             {'drop': 'freshness.json'},
                             {'put': '.staging-cd34/review.json', 'to': 'review.json'}],
              'result': {'to': 'new', 'counts': {}, 'message': 'Reviewed new.'}}

        rereview._commit(self.page, st)

        self.assertEqual(json.loads((folder / 'decisions.json').read_text(encoding='utf-8')),
                         {'findings': {'W1': {'status': 'post'}}})
        self.assertEqual(store.page_data(self.page)['ref'], 'new')
        self.assertFalse((folder / 'freshness.json').exists())
        self.assertFalse(staging.exists())
        self.assertEqual(st['status'], 'done')

        # A job that died before it committed: its staging folder goes.
        stage = folder / '.staging-ee56'
        stage.mkdir()
        (stage / 'x.json').write_text('{}', encoding='utf-8')
        rereview._drop_staged(self.page, 'ee56')
        self.assertFalse(stage.exists())

    def test_stage_with_the_real_write_page_then_commit(self):
        self.addCleanup(forget, J)
        support.write_page(J)
        old = support.sample_page(J)
        new = support.sample_page(J, ref='new')
        st = {'status': 'reviewing', 'from': 'old', 'to': 'new',
              'result': {'to': 'new', 'counts': {}, 'message': 'Reviewed new.'}}

        steps = rereview._stage(J, 'ab99', st, old, new, {}, render.write_page)

        folder = OUT / J
        stage = folder / '.staging-ab99'
        self.assertEqual(sorted(os.listdir(stage)), ['decisions.json', 'page.html', 'review.json'])
        self.assertEqual(steps[-2:], [{'put': '.staging-ab99/page.html', 'to': 'page.html'},
                                      {'put': '.staging-ab99/review.json', 'to': 'review.json'}])
        # A worker that died halfway: the steps before the page's are in place, the page's two are still staged.
        for step in steps[:-2]:
            if step.get('put'):
                os.replace(folder / step['put'], folder / step['to'])
            elif step.get('drop'):
                (folder / step['drop']).unlink(missing_ok=True)

        rereview._commit(J, {**st, 'committing': steps}, late=True)

        self.assertEqual(store.page_data(J)['ref'], 'new')
        self.assertIn('"ref": "new"', (folder / 'page.html').read_text(encoding='utf-8'))
        self.assertFalse(stage.exists())
        before = snapshot(J)
        rereview._commit(J, {**st, 'committing': steps}, late=True)
        self.assertEqual(snapshot(J), before)


if __name__ == '__main__':
    unittest.main()
