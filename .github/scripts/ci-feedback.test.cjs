const test = require('node:test');
const assert = require('node:assert/strict');
const { resolve, report } = require('./ci-feedback.cjs');

const SHA = 'a'.repeat(40);
// Temporary live verification only. This branch will never be merged.
test('live feedback smoke: deliberate failure for retry verification', () => {
  assert.fail('Expected temporary failure: verifying CI feedback and retry, not a product defect.');
});
const NOW = Date.parse('2026-09-07T12:00:00Z');
const BOT = { id: 41898282, login: 'github-actions[bot]', type: 'Bot' };

function fixture(options = {}) {
  const pr = {
    number: 7, state: 'open', user: { id: 10, login: 'contributor' },
    head: { sha: SHA, ref: 'fix', repo: { id: 20 } },
    base: { ref: 'main', repo: { full_name: 'owner/repo' } },
  };
  const run = {
    id: 50, workflow_id: 3, path: '.github/workflows/ci.yml', event: 'pull_request',
    head_sha: SHA, head_branch: 'fix', head_repository: { id: 20 },
    pull_requests: [], status: 'completed', conclusion: 'failure', run_attempt: 1,
    created_at: '2026-09-07T11:00:00Z',
  };
  const jobs = [{ id: 60, name: 'lint', status: 'completed', conclusion: 'failure',
    started_at: '2026-09-07T11:00:10Z',
    steps: [{ name: 'Run ruff format --check src/ tests/', conclusion: 'failure' }] }];
  const comments = [];
  const calls = [];
  const context = { repo: { owner: 'owner', repo: 'repo' }, eventName: 'pull_request_target',
    payload: { repository: { default_branch: 'main' }, pull_request: pr } };
  const f = { pr, run, jobs, comments, calls, context, runs: [run], permissions: {}, reads: 0 };
  Object.assign(f, options);
  f.github = {
    async request(route, params) {
      calls.push({ route, params });
      if (route.endsWith('/pulls/{pull_number}')) {
        f.reads++;
        if (f.changeHead && f.reads > 1) f.pr.head.sha = 'b'.repeat(40);
        return { data: structuredClone(f.pr) };
      }
      if (route.endsWith('/actions/workflows/{workflow_id}')) return { data: { id: 3 } };
      if (route.endsWith('/permission')) {
        return { data: { permission: f.permissions[params.username] || 'read' } };
      }
      if (route.startsWith('GET') && route.endsWith('/actions/runs/{run_id}')) {
        return { data: structuredClone(f.run) };
      }
      if (route.startsWith('POST') && route.endsWith('/issues/{issue_number}/comments')) {
        const comment = { id: 100, user: BOT, created_at: new Date(NOW).toISOString(), body: params.body };
        comments.push(comment);
        return { data: comment };
      }
      if (route.startsWith('PATCH') && route.endsWith('/issues/comments/{comment_id}')) {
        const comment = comments.find(c => c.id === params.comment_id);
        comment.body = params.body;
        return { data: comment };
      }
      if (route.startsWith('POST') && /\/(approve|rerun-failed-jobs)$/.test(route)) {
        if (f.actionError) throw Object.assign(new Error('untrusted error detail'), { status: 403 });
        return { data: {} };
      }
      throw new Error(`Unexpected request: ${route}`);
    },
    async paginate(route, params) {
      calls.push({ route, params });
      if (route.endsWith('/pulls')) return [structuredClone(f.pr)];
      if (route.endsWith('/actions/workflows/{workflow_id}/runs')) {
        assert.equal(params.head_sha, f.pr.head.sha);
        assert.equal(params.event, 'pull_request');
        assert.equal(params.workflow_id, 3);
        return structuredClone(f.runs);
      }
      if (route.endsWith('/jobs')) return structuredClone(f.jobs);
      if (route.endsWith('/comments')) return structuredClone(comments);
      throw new Error(`Unexpected pagination: ${route}`);
    },
  };
  f.command = (body, user = { id: 10, login: 'contributor', type: 'User' }, extra = {}) => {
    const comment = { id: 200 + comments.length, body, user,
      created_at: new Date(NOW).toISOString(), ...extra };
    comments.push(comment);
    context.eventName = 'issue_comment';
    context.payload.issue = { number: 7, pull_request: {} };
    context.payload.comment = comment;
    return comment;
  };
  f.update = () => report({ github: f.github, context, number: 7, now: NOW });
  f.body = () => comments.find(c => c.user.login === BOT.login)?.body || '';
  f.actions = () => calls.filter(c => c.route.startsWith('POST') && /\/(approve|rerun-failed-jobs)$/.test(c.route));
  return f;
}

test('fork workflow events with empty PR arrays resolve by exact repo, branch and SHA', async () => {
  const f = fixture();
  f.context.eventName = 'workflow_run';
  f.context.payload.workflow_run = f.run;
  assert.deepEqual(await resolve(f), [7]);
  f.run.head_sha = 'b'.repeat(40);
  assert.deepEqual(await resolve(f), []);
});

test('a similarly named workflow or a push event cannot control CI', async () => {
  for (const override of [{ workflow_id: 99 }, { event: 'push' }, { head_repository: { id: 99 } }]) {
    const f = fixture();
    f.context.eventName = 'workflow_run';
    f.context.payload.workflow_run = { ...f.run, ...override };
    assert.deepEqual(await resolve(f), []);
  }
});

test('issue comments, bots and unrelated commands do not start feedback jobs', async () => {
  const f = fixture();
  f.command('/ci');
  assert.deepEqual(await resolve(f), [7]);
  delete f.context.payload.issue.pull_request;
  assert.deepEqual(await resolve(f), []);
  f.context.payload.issue.pull_request = {};
  f.context.payload.comment.user = BOT;
  assert.deepEqual(await resolve(f), []);
});

test('automatically reports approval waiting with a commit-bound maintainer command', async () => {
  const f = fixture();
  f.run.conclusion = 'action_required';
  f.jobs.length = 0;
  await f.update();
  assert.match(f.body(), /Waiting for maintainer approval/);
  assert.ok(f.body().includes(`/ci approve ${SHA}`));
  assert.equal(f.actions().length, 0);
});

test('failure summary links the failing job and step without pinging injected names', async () => {
  const f = fixture();
  f.jobs[0].name = 'lint @everyone [click](https://evil.invalid)';
  await f.update();
  assert.match(f.body(), /actions\/runs\/50\/job\/60/);
  assert.match(f.body(), /ruff format/);
  assert.ok(!f.body().includes('@everyone'));
  assert.ok(!f.body().includes('[click](https://evil.invalid)'));
});

test('status updates reuse one bot comment and identical events do not rewrite it', async () => {
  const f = fixture();
  await f.update();
  const count = f.calls.length;
  await f.update();
  assert.equal(f.comments.length, 1);
  assert.equal(f.calls.slice(count).filter(c => c.route.startsWith('PATCH')).length, 0);
  f.run.conclusion = 'success';
  await f.update();
  assert.equal(f.comments.length, 1);
  assert.match(f.body(), /Passed/);
});

test('a contributor cannot impersonate the bot marker or approval authority', async () => {
  const f = fixture();
  f.comments.push({ id: 99, user: f.pr.user, body: '<!-- dbt-plan-ci-feedback:v1 -->\nforged' });
  f.run.conclusion = 'action_required';
  f.command(`/ci approve ${SHA}`);
  await f.update();
  assert.equal(f.actions().length, 0);
  assert.match(f.body(), /write access/);
  assert.equal(f.comments[0].body, '<!-- dbt-plan-ci-feedback:v1 -->\nforged');
});

test('only current write permission permits approval of the exact current SHA', async () => {
  for (const permission of ['read', 'triage', 'write', 'maintain', 'admin']) {
    const f = fixture();
    f.run.conclusion = 'action_required';
    f.permissions.maintainer = permission;
    f.command(`/ci approve ${SHA}`, { id: 11, login: 'maintainer', type: 'User' },
      { author_association: 'COLLABORATOR' });
    await f.update();
    assert.equal(f.actions().length, ['write', 'maintain', 'admin'].includes(permission) ? 1 : 0);
  }
});

test('old or missing approval SHA never approves the new commit', async () => {
  for (const body of ['/ci approve', `/ci approve ${'b'.repeat(40)}`]) {
    const f = fixture();
    f.run.conclusion = 'action_required';
    f.permissions.contributor = 'write';
    f.command(body);
    await f.update();
    assert.equal(f.actions().length, 0);
  }
});

test('PR author can rerun failed jobs once; duplicate events do not rerun again', async () => {
  const f = fixture();
  f.command('/ci retry');
  await f.update();
  await f.update();
  assert.equal(f.actions().length, 1);
  assert.match(f.actions()[0].route, /rerun-failed-jobs$/);
});

test('outsiders and multi-line command injection cannot rerun jobs', async () => {
  for (const [body, user] of [
    ['/ci retry', { id: 90, login: 'stranger', type: 'User' }],
    ['/ci retry\n/ci approve', undefined],
    ['/ci retry; echo secret', undefined],
  ]) {
    const f = fixture();
    f.command(body, user);
    await f.update();
    assert.equal(f.actions().length, 0);
  }
});

test('retry never approves a run or restarts running, successful or never-executed jobs', async () => {
  for (const override of [
    { conclusion: 'action_required' }, { status: 'in_progress' },
    { conclusion: 'success' }, { conclusion: 'cancelled' }, { run_attempt: 3 },
  ]) {
    const f = fixture();
    Object.assign(f.run, override);
    f.command('/ci retry');
    await f.update();
    assert.equal(f.actions().length, 0);
  }
  const f = fixture();
  f.jobs.length = 0;
  f.command('/ci retry');
  await f.update();
  assert.equal(f.actions().length, 0);
});

test('rapid repeat commands respect cooldown even before GitHub updates run state', async () => {
  const f = fixture();
  f.command('/ci retry');
  await f.update();
  f.command('/ci retry');
  await f.update();
  assert.equal(f.actions().length, 1);
  assert.match(f.body(), /five minutes/);
});

test('commands predating the current run do not retry it', async () => {
  const f = fixture();
  f.command('/ci retry', undefined, { created_at: '2026-09-06T00:00:00Z' });
  await f.update();
  assert.equal(f.actions().length, 0);
});

test('edited comments are not executed when a later status event scans the thread', async () => {
  const f = fixture();
  await f.update();
  f.command('/ci retry', undefined, { updated_at: '2026-09-07T12:00:05Z' });
  f.context.eventName = 'workflow_run';
  await f.update();
  assert.equal(f.actions().length, 0);
});

test('a head change during the request prevents the API mutation', async () => {
  const f = fixture({ changeHead: true });
  f.command('/ci retry');
  await f.update();
  assert.equal(f.actions().length, 0);
});

test('the latest matching CI run is selected, never another workflow, fork, branch or SHA', async () => {
  const f = fixture();
  f.runs.push(...[
    { id: 1000, workflow_id: 99 }, { id: 1001, head_repository: { id: 99 } },
    { id: 1002, head_sha: 'b'.repeat(40) }, { id: 1003, head_branch: 'other' },
  ].map(x => ({ ...f.run, ...x })));
  f.command('/ci retry');
  await f.update();
  assert.equal(f.actions()[0].params.run_id, 50);
});

test('missing runs remain unknown rather than reporting a passing check', async () => {
  const f = fixture({ runs: [] });
  await f.update();
  assert.match(f.body(), /No CI run found/);
  assert.ok(!f.body().includes('**Passed**'));
});

test('API errors are visible without echoing untrusted response text', async () => {
  const f = fixture({ actionError: true });
  f.command('/ci retry');
  await f.update();
  assert.match(f.body(), /HTTP 403/);
  assert.ok(!f.body().includes('untrusted error detail'));
});

test('closed PRs are ignored', async () => {
  const f = fixture();
  f.pr.state = 'closed';
  f.command('/ci retry');
  await f.update();
  assert.equal(f.actions().length, 0);
  assert.equal(f.body(), '');
});

test('an automatic event processes a queued command without replaying historical comments', async () => {
  const f = fixture();
  f.command('/ci retry', undefined, { created_at: '2026-09-06T00:00:00Z' });
  f.context.eventName = 'pull_request_target';
  await f.update();
  assert.equal(f.actions().length, 0);
  f.command('/ci retry');
  f.context.eventName = 'workflow_run';
  await f.update();
  assert.equal(f.actions().length, 1);
});
