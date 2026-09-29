"""Tests for Phase E: products (named, shippable artifacts) + structured wiki.

Covers the pure sim helpers (create_product / next_product_id /
next_product_revision / product_release_record / set_product_status /
wiki_write_page / wiki_read_pages / inject_wiki_context / release_uses_handle),
the server-owned merge carry, and the endpoint layer (create/release/status,
wiki page/read/category) with real session auth. Runs against a hermetic temp
DB and a temp sandbox directory -- never the live think tank.
"""
import os
import shutil
import tempfile
import unittest
import unittest.mock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim
import serve


def _make_product_state():
    """A minimal state with product + wiki scaffolds present."""
    return {
        'sim': {'owner': 'server'},
        'agentRoster': [
            {'id': 'faye', 'name': 'Faye', 'role': 'admin', 'isAdmin': True, 'isDirector': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'faye'},
            {'id': 'maya', 'name': 'Maya', 'role': 'director', 'isDirector': True, 'director': 'faye'},
        ],
        'agents': {
            'faye': {'id': 'faye', 'name': 'Faye', 'busy': False, 'offDuty': False},
            'ada': {'id': 'ada', 'name': 'Ada', 'busy': False, 'offDuty': False},
            'ben': {'id': 'ben', 'name': 'Ben', 'busy': False, 'offDuty': False},
            'maya': {'id': 'maya', 'name': 'Maya', 'busy': False, 'offDuty': False},
        },
        'teams': [{'id': 'faye', 'name': "Faye's Crew", 'directorId': 'faye'}],
        'products': {},
        'wiki': {'pages': {}, 'categories': {'workroom': {'label': 'Work Room'}, 'research': {'label': 'Research'}}},
        'reports': [],
        'workQueue': [],
        'tasks': {},
    }


class ProductHelpers(unittest.TestCase):
    def test_create_and_release_are_cataloged(self):
        state = _make_product_state()
        rec = sim.create_product(state, sim.next_product_id(state), 'Loom', 'A game',
                                 'Build a loom', 'ada', 'workroom-shared', handles=['hnd_1'])
        self.assertEqual(rec['id'], 'prd-1')
        self.assertEqual(rec['status'], 'draft')
        self.assertTrue(rec['revisions'] == [])
        self.assertEqual(rec['nextRevision'], 1)
        self.assertEqual(sim.release_uses_handle(state, 'prd-1'), 'hnd_1')

    def test_create_idempotent_clash(self):
        state = _make_product_state()
        rec = sim.create_product(state, 'prd-1', 'Loom', 'x', 'y', 'ada', 'workroom-shared')
        self.assertEqual(rec['id'], 'prd-1')
        self.assertIsNone(sim.create_product(state, 'prd-1', 'Dup', 'x', 'y', 'ada', 'workroom-shared'))
        self.assertEqual(len(state['products']), 1)

    def test_release_appends_revision_and_flips_status(self):
        state = _make_product_state()
        sim.create_product(state, 'prd-1', 'Loom', 'x', 'y', 'ada', 'workroom-shared')
        n, rel = sim.next_product_revision(state, 'prd-1')
        self.assertEqual((n, rel), (1, 'v1'))
        rev = sim.product_release_record(state, 'prd-1', n, 'maya', 'ship it', target_path='v1')
        self.assertEqual(rev['n'], 1)
        self.assertEqual(state['products']['prd-1']['status'], 'released')
        self.assertEqual(state['products']['prd-1']['nextRevision'], 2)
        self.assertEqual(rev['path'], 'v1')
        # Re-release bumps to v2.
        n2, _rel2 = sim.next_product_revision(state, 'prd-1')
        self.assertEqual(n2, 2)
        sim.product_release_record(state, 'prd-1', n2, 'maya', '', target_path='v2')
        self.assertEqual(len(state['products']['prd-1']['revisions']), 2)

    def test_release_unknown_product_returns_none(self):
        state = _make_product_state()
        self.assertIsNone(sim.product_release_record(state, 'prd-9', 1, 'maya'))
        self.assertIsNone(sim.next_product_revision(state, 'prd-9')[0])

    def test_set_product_status_gates_released(self):
        state = _make_product_state()
        sim.create_product(state, 'prd-1', 'Loom', 'x', 'y', 'ada', 'workroom-shared')
        rec = sim.set_product_status(state, 'prd-1', 'in_progress')
        self.assertEqual(rec['status'], 'in_progress')
        # 'released' is unreachable through set_product_status.
        self.assertIsNone(sim.set_product_status(state, 'prd-1', 'released'))


class WikiHelpers(unittest.TestCase):
    def test_write_page_bumps_version_and_history(self):
        state = _make_product_state()
        rec, is_new = sim.wiki_write_page(state, 'workroom-policy', 'Policy', 'workroom',
                                          'Plan the work.', 'maya')
        self.assertTrue(is_new)
        self.assertEqual(rec['version'], 1)
        rec2, is_new2 = sim.wiki_write_page(state, 'workroom-policy', 'Policy', 'workroom',
                                            'Plan it twice.', 'maya')
        self.assertFalse(is_new2)
        self.assertEqual(rec2['version'], 2)
        self.assertEqual(len(rec2['history']), 1)
        self.assertEqual(rec2['history'][0]['version'], 1)

    def test_write_page_rejects_unknown_category(self):
        state = _make_product_state()
        rec, _ = sim.wiki_write_page(state, 'x', 'X', 'nope-category', 'body', 'maya')
        self.assertIsNone(rec)

    def test_read_pages_and_inject_affinity(self):
        state = _make_product_state()
        sim.wiki_write_page(state, 'workroom-policy', 'Policy', 'workroom', 'b1', 'maya')
        sim.wiki_write_page(state, 'research-note', 'Note', 'research', 'b2', 'maya')
        # A workroom task pulls the workroom page, not the research one.
        pages = sim.wiki_read_pages(state, 'pressoffice', max_pages=3)
        self.assertTrue(any(p['id'] == 'workroom-policy' for p in pages))
        ctx = sim.inject_wiki_context(state, {'room': 'pressoffice'})
        self.assertIn('Policy', ctx)
        self.assertNotIn('Note', ctx)
        # Empty wiki -> empty context, executor still runs (blank knowledge).
        empty = _make_product_state()
        self.assertEqual(sim.inject_wiki_context(empty, {'room': 'pressoffice'}), '')

    def test_inject_honors_explicit_page_ids(self):
        state = _make_product_state()
        sim.wiki_write_page(state, 'a', 'Alpha', 'workroom', 'x', 'maya')
        sim.wiki_write_page(state, 'b', 'Beta', 'research', 'y', 'maya')
        ctx = sim.inject_wiki_context(state, {'room': 'pressoffice', 'wikiPageIds': ['b']})
        self.assertIn('Beta', ctx)
        self.assertNotIn('Alpha', ctx)


class ProductEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-products-')
        # A fake sandbox repo on disk for the release snapshot path.
        self.sb = os.path.join(self.tmp, 'sandboxes', 'workroom-shared')
        os.makedirs(self.sb, exist_ok=True)
        with open(os.path.join(self.sb, 'index.html'), 'w') as f:
            f.write('<h1>hi</h1>\n')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SANDBOXES_DIR=os.path.join(self.tmp, 'sandboxes'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db(_make_product_state())

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        sid = serve.create_session()
        c.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        return c

    def _auth(self, c, agent_id):
        # Return query params + headers so _resolve_requester (which reads
        # `requesterId` from the QUERY STRING) recognizes the caller as a real
        # agent; the attribution key rides in X-Agent-Key.
        key = serve.get_or_create_agent_key(agent_id)
        return {'params': {'requesterId': agent_id}, 'headers': {'X-Agent-Key': key}}

    def test_create_and_list_product(self):
        c = self._client()
        r = c.post('/api/intent/product', json={
            'name': 'Loom', 'summary': 'A game', 'spec': 'Build it',
            'ownerId': 'ada', 'sandboxId': 'workroom-shared',
        })
        self.assertEqual(r.status_code, 200, r.text)
        product = r.json()['product']
        pid = product['id']
        self.assertEqual(product['status'], 'draft')
        lst = c.get('/api/intent/products')
        self.assertEqual(lst.status_code, 200)
        self.assertIn(pid, lst.json()['products'])

    def test_create_rejects_unknown_sandbox_and_owner(self):
        c = self._client()
        r = c.post('/api/intent/product', json={
            'name': 'Loom', 'ownerId': 'ada', 'sandboxId': 'does-not-exist',
        })
        self.assertEqual(r.status_code, 400)
        r2 = c.post('/api/intent/product', json={
            'name': 'Loom', 'ownerId': 'ghost', 'sandboxId': 'workroom-shared',
        })
        self.assertEqual(r2.status_code, 404)

    def test_release_snapshots_repo_and_returns_revision(self):
        c = self._client()
        pid = c.post('/api/intent/product', json={
            'name': 'Loom', 'ownerId': 'ada', 'sandboxId': 'workroom-shared',
        }).json()['product']['id']
        r = c.post(f'/api/intent/product/{pid}/release', json={'revisionNote': 'v1 out'})
        self.assertEqual(r.status_code, 200, r.text)
        rev = r.json()['revision']
        self.assertEqual(rev['n'], 1)
        # The snapshot landed under library/projects/<id>/v1/.
        snap = os.path.join(self.tmp, 'library', 'projects', pid, 'v1')
        self.assertTrue(os.path.isfile(os.path.join(snap, 'index.html')))
        self.assertTrue(os.path.isfile(os.path.join(snap, 'RELEASE.md')))
        # Re-release bumps to v2 + appends the revision log.
        r2 = c.post(f'/api/intent/product/{pid}/release', json={})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()['revision']['n'], 2)
        # Status is now released.
        self.assertEqual(c.get('/api/intent/products').json()['products'][pid]['status'], 'released')

    def test_release_missing_sandbox_is_clean_error(self):
        c = self._client()
        pid = c.post('/api/intent/product', json={
            'name': 'Loom', 'ownerId': 'ada', 'sandboxId': 'workroom-shared',
        }).json()['product']['id']
        # Point the product at a missing sandbox directly in state.
        st = serve.get_state_from_db()
        st['products'][pid]['sandboxId'] = 'workroom-shared'
        serve.save_state_to_db(st)
        # Sandbox dir still exists; simulate the missing case by removing it.
        shutil.rmtree(os.path.join(self.tmp, 'sandboxes', 'workroom-shared'))
        r = c.post(f'/api/intent/product/{pid}/release', json={})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('revision', r.json())

    def test_product_status_needs_director(self):
        c = self._client()
        pid = c.post('/api/intent/product', json={
            'name': 'Loom', 'ownerId': 'ada', 'sandboxId': 'workroom-shared',
        }).json()['product']['id']
        # A non-director (ada) is denied.
        r = c.post(f'/api/intent/product/{pid}/status', json={'status': 'in_progress'},
                   **self._auth(c, 'ada'))
        self.assertEqual(r.status_code, 403)
        # A director (maya) succeeds.
        r2 = c.post(f'/api/intent/product/{pid}/status', json={'status': 'in_progress'},
                    **self._auth(c, 'maya'))
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()['product']['status'], 'in_progress')


class WikiEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-wiki-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SANDBOXES_DIR=os.path.join(self.tmp, 'sandboxes'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve.save_state_to_db(_make_product_state())

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self):
        from fastapi.testclient import TestClient
        c = TestClient(serve.app)
        sid = serve.create_session()
        c.cookies.set(serve.SESSION_COOKIE_NAME, sid)
        return c

    def _auth(self, c, agent_id):
        key = serve.get_or_create_agent_key(agent_id)
        return {'params': {'requesterId': agent_id}, 'headers': {'X-Agent-Key': key}}

    def test_write_blocked_for_non_director(self):
        c = self._client()
        r = c.post('/api/intent/wiki/page', json={
            'id': 'p', 'title': 'P', 'category': 'workroom', 'body': 'hi',
        }, **self._auth(c, 'ada'))
        self.assertEqual(r.status_code, 403)

    def test_write_and_read_round_trip(self):
        c = self._client()
        r = c.post('/api/intent/wiki/page', json={
            'id': 'policy', 'title': 'Policy', 'category': 'workroom', 'body': 'Plan the work.',
        }, **self._auth(c, 'maya'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['page']['version'], 1)
        # Body landed on disk under library/wiki/workroom/policy.md.
        body_path = os.path.join(self.tmp, 'library', 'wiki', 'workroom', 'policy.md')
        self.assertTrue(os.path.isfile(body_path))
        got = c.get('/api/intent/wiki/page/policy')
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()['page']['body'], 'Plan the work.')
        # Rewrite bumps version + still readable.
        r2 = c.post('/api/intent/wiki/page', json={
            'id': 'policy', 'title': 'Policy', 'category': 'workroom', 'body': 'Plan twice.',
        }, **self._auth(c, 'maya'))
        self.assertEqual(r2.json()['page']['version'], 2)

    def test_unknown_page_404(self):
        c = self._client()
        self.assertEqual(c.get('/api/intent/wiki/page/nope').status_code, 404)

    def test_category_write(self):
        c = self._client()
        r = c.post('/api/intent/wiki/category', json={'id': 'ops', 'label': 'Ops'},
                   **self._auth(c, 'maya'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('ops', r.json()['categories'])


class WriteWikiServerAutoSeed(unittest.TestCase):
    """_write_wiki_server's 'think_tank' auto-seed (2026-09-26): real gap caught
    live -- distillation's server-owned write path required a 'think_tank' wiki
    category to already exist, but nothing ever seeds one; it's only ever
    created via a director manually calling POST /api/intent/wiki/category.
    A think tank where nobody happened to do that had every distillation
    attempt silently fail its write, forever. Real function under test here
    (unlike test_distill.py's DistillExecutor, which mocks _write_wiki_server
    entirely) -- isolated the same way WikiEndpoints above is."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-wiki-autoseed-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            SANDBOXES_DIR=os.path.join(self.tmp, 'sandboxes'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_successfully_when_think_tank_category_is_missing_and_auto_seeds_it(self):
        state = _make_product_state()  # only has 'workroom'/'research' categories
        serve.save_state_to_db(state)
        record = serve._write_wiki_server('state-of-knowledge', 'Think Tank state of knowledge',
                                          'think_tank', 'Merged think tank knowledge.')
        self.assertIsNotNone(record, 'the write must succeed, not silently fail')
        persisted = serve.get_state_from_db()
        self.assertIn('think_tank', persisted['wiki']['categories'])
        body_path = os.path.join(self.tmp, 'library', 'wiki', 'think_tank', 'state-of-knowledge.md')
        self.assertTrue(os.path.isfile(body_path))

    def test_does_not_auto_seed_an_arbitrary_missing_category(self):
        # Only 'think_tank' is special-cased (a hardcoded system constant the
        # server-owned path itself depends on) -- any other missing category
        # still fails closed exactly as before, no silent auto-creation.
        state = _make_product_state()
        serve.save_state_to_db(state)
        record = serve._write_wiki_server('p', 'P', 'nonexistent-category', 'body')
        self.assertIsNone(record)
        persisted = serve.get_state_from_db()
        self.assertNotIn('nonexistent-category', persisted['wiki']['categories'])

    def test_does_not_overwrite_an_already_existing_think_tank_category(self):
        # A director may already have set a custom label/order for 'think_tank'
        # -- the auto-seed must only fire when truly missing, never clobber
        # an existing one back to the generic default.
        state = _make_product_state()
        state['wiki']['categories']['think_tank'] = {'label': 'Custom Think Tank Label', 'order': 7}
        serve.save_state_to_db(state)
        record = serve._write_wiki_server('state-of-knowledge', 'T', 'think_tank', 'body')
        self.assertIsNotNone(record)
        persisted = serve.get_state_from_db()
        self.assertEqual(persisted['wiki']['categories']['think_tank'],
                         {'label': 'Custom Think Tank Label', 'order': 7})


if __name__ == '__main__':
    unittest.main()