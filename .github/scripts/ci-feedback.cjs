// Runs only from the default branch in the privileged feedback workflow.
// Never check out PR code, download artifacts, or execute text from GitHub data.
const ROOT = '/repos/{owner}/{repo}';
const MARKER = '<!-- dbt-plan-ci-feedback:v1 -->';
const WRITERS = new Set(['write', 'maintain', 'admin']);
const TRUSTED_ASSOCIATIONS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);

function api(github, context) {
  return {
    get: async (path, params = {}) => (await github.request(`GET ${ROOT}${path}`, { ...context.repo, ...params })).data,
    list: (path, params = {}) => github.paginate(`GET ${ROOT}${path}`, { ...context.repo, per_page: 100, ...params }),
    write: (method, path, params) => github.request(`${method} ${ROOT}${path}`, { ...context.repo, ...params }),
  };
}

function command(body) {
  const match = /^\/ci(?: +(status|retry|approve)(?: +([a-f0-9]{40}))?)?$/.exec((body || '').trim());
  if (!match || (match[2] && match[1] !== 'approve')) return null;
  return { action: match[1] || 'status', sha: match[2] };
}

function matches(run, pr, workflowId) {
  return run.workflow_id === workflowId && run.event === 'pull_request'
    && run.head_sha === pr.head.sha && run.head_branch === pr.head.ref
    && run.head_repository?.id === pr.head.repo?.id
    && (!run.pull_requests?.length || run.pull_requests.some(p => p.number === pr.number));
}

async function resolve({ github, context }) {
  const { payload, eventName } = context;
  if (eventName === 'issue_comment') {
    return payload.issue.pull_request && payload.comment.user.type !== 'Bot'
      && command(payload.comment.body) ? [payload.issue.number] : [];
  }
  if (eventName === 'pull_request_target') return [payload.pull_request.number];
  if (eventName === 'workflow_dispatch') {
    const number = Number(payload.inputs?.pr);
    return Number.isSafeInteger(number) && number > 0 ? [number] : [];
  }
  if (eventName !== 'workflow_run' || payload.workflow_run.event !== 'pull_request') return [];
  const client = api(github, context);
  const workflow = await client.get('/actions/workflows/{workflow_id}', { workflow_id: 'ci.yml' });
  if (payload.workflow_run.workflow_id !== workflow.id) return [];
  // GitHub leaves workflow_run.pull_requests empty for many fork runs.
  const prs = await client.list('/pulls', { state: 'open', base: payload.repository.default_branch });
  return prs.filter(pr => matches(payload.workflow_run, pr, workflow.id)).map(pr => pr.number);
}

function text(value) {
  return String(value).replace(/@/g, '＠').replace(/[\r\n]/g, ' ')
    .replace(/[\\`*_{}\[\]()<>|#!]/g, '\\$&').slice(0, 200);
}

function readState(comment) {
  try {
    return JSON.parse(comment?.body.match(/<!-- ci-state: (.+) -->/)?.[1] || '{}');
  } catch {
    return {};
  }
}

function render(context, pr, run, jobs, state, notice = '', submitted = '') {
  const root = `https://github.com/${context.repo.owner}/${context.repo.repo}`;
  const runLink = run ? `[Open CI run](${root}/actions/runs/${run.id})` : `[Open checks](${root}/pull/${pr.number}/checks)`;
  let status = 'No CI run found for this commit yet';
  if (run) {
    status = run.status !== 'completed' ? 'Queued or running'
      : ({ success: 'Passed', action_required: 'Waiting for maintainer approval',
        failure: 'Failed', timed_out: 'Timed out', cancelled: 'Cancelled',
        skipped: 'Skipped — not a passing check' }[run.conclusion] || 'Needs attention');
  }
  const lines = [MARKER, '### CI status', '',
    `**${submitted || status}** · [${pr.head.sha.slice(0, 7)}](${root}/commit/${pr.head.sha}) · ${runLink}`, ''];
  if (notice) lines.push(notice, '');
  if (!run) lines.push('A new commit normally starts CI automatically. If it was skipped, check the workflow and commit message. Use `/ci` to refresh.', '');
  if (run?.conclusion === 'action_required' && !submitted) {
    lines.push('This is waiting for approval, not a test failure. A maintainer with write access can review the changes and comment:',
      '', `\`/ci approve ${pr.head.sha}\``, '');
  }
  const failed = jobs.filter(job => ['failure', 'timed_out', 'cancelled'].includes(job.conclusion));
  if (failed.length) {
    lines.push('Failed jobs:', '');
    for (const job of failed.slice(0, 15)) {
      const steps = (job.steps || []).filter(s => ['failure', 'timed_out'].includes(s.conclusion));
      lines.push(`- [${text(job.name)}](${root}/actions/runs/${run.id}/job/${job.id})${steps.length ? ` — ${steps.slice(0, 2).map(s => text(s.name)).join('; ')}` : ''}`);
    }
    lines.push('', 'For code/test failures, fix the code and push a commit. For a transient failure, the PR author or a maintainer can comment `/ci retry`.', '');
  }
  lines.push('`/ci` refreshes this comment. `/ci retry` retries failed jobs on this commit only (five-minute cooldown, at most three run attempts). New commits require their own approval when GitHub requests it.',
    '', 'CI execution approval does not approve or merge the pull request.',
    '', `<!-- ci-state: ${JSON.stringify(state)} -->`);
  return lines.join('\n');
}

async function report({ github, context, number, now = Date.now() }) {
  const client = api(github, context);
  const pr = await client.get('/pulls/{pull_number}', { pull_number: number });
  if (pr.state !== 'open' || !pr.head.repo || !/^[a-f0-9]{40}$/.test(pr.head.sha)
    || pr.base.repo.full_name !== `${context.repo.owner}/${context.repo.repo}`
    || pr.base.ref !== context.payload.repository.default_branch) return;
  const workflow = await client.get('/actions/workflows/{workflow_id}', { workflow_id: 'ci.yml' });
  const runs = await client.list('/actions/workflows/{workflow_id}/runs', {
    workflow_id: workflow.id, event: 'pull_request', head_sha: pr.head.sha,
  });
  const run = runs.filter(r => matches(r, pr, workflow.id)).sort((a, b) => b.id - a.id)[0];
  const jobs = run && run.conclusion !== 'action_required'
    ? await client.list('/actions/runs/{run_id}/jobs', { run_id: run.id, filter: 'latest' }) : [];
  const comments = await client.list('/issues/{issue_number}/comments', { issue_number: number });
  let sticky = comments.find(c => c.user.type === 'Bot' && c.user.login === 'github-actions[bot]' && c.body.startsWith(MARKER));
  const previous = readState(sticky);
  const state = { head: pr.head.sha, commandId: previous.commandId || 0,
    retryAt: previous.head === pr.head.sha ? previous.retryAt || 0 : 0 };

  async function publish(notice = '', submitted = '') {
    // A synchronize event will post the new head. Never overwrite it with old results.
    const current = await client.get('/pulls/{pull_number}', { pull_number: number });
    if (current.state !== 'open' || current.head.sha !== pr.head.sha) return false;
    const body = render(context, pr, run, jobs, state, notice, submitted);
    if (sticky?.body === body) return true;
    const response = sticky
      ? await client.write('PATCH', '/issues/comments/{comment_id}', { comment_id: sticky.id, body })
      : await client.write('POST', '/issues/{issue_number}/comments', { issue_number: number, body });
    sticky = response.data;
    return true;
  }

  // Per-PR jobs serialize writes. Read unhandled commands too: a newer queued status
  // event can replace a pending command job in GitHub's concurrency queue.
  // Do not replay comments that existed before this bot first appeared.
  const candidates = comments.filter(c => c.user.type === 'User' && c.id > state.commandId
    && (!c.updated_at || c.updated_at === c.created_at)
    && command(c.body) && (sticky ? Date.parse(c.created_at) >= Date.parse(sticky.created_at)
      : context.eventName === 'issue_comment' && c.id === context.payload.comment.id))
    .sort((a, b) => b.id - a.id);
  let selected;
  for (const comment of candidates) {
    const isAuthor = comment.user.id === pr.user.id;
    if (!isAuthor && !TRUSTED_ASSOCIATIONS.has(comment.author_association)) continue;
    const cmd = command(comment.body);
    let writer = false;
    if (!isAuthor || cmd.action === 'approve') {
      try {
        const access = await client.get('/collaborators/{username}/permission', { username: comment.user.login });
        writer = WRITERS.has(access.permission);
      } catch (error) {
        if (error.status !== 404) throw error;
      }
    }
    if (!isAuthor && !writer) continue;
    selected = { comment, cmd, writer };
    break;
  }
  if (!selected) return publish();
  const { comment, cmd, writer } = selected;
  state.commandId = comment.id;
  if (cmd.action === 'status') return publish();
  if (cmd.action === 'approve' && !writer) return publish('Approval requires current repository write access. A contributor cannot approve their own external CI run.');
  if (cmd.action === 'approve' && cmd.sha !== pr.head.sha) return publish(`Approval must name the current commit: \`/ci approve ${pr.head.sha}\`.`);
  if (!run) return publish('There is no current CI run to act on. Push a commit or inspect the Checks tab.');
  if (run.status !== 'completed') return publish('CI is already queued or running; no duplicate run was started.');
  if (cmd.action === 'approve' && run.conclusion !== 'action_required') return publish('This CI run is not waiting for execution approval.');
  if (cmd.action === 'retry') {
    if (!['failure', 'timed_out'].includes(run.conclusion) || !jobs.some(j => j.started_at)) {
      return publish('Only previously executed failed jobs can be retried. Approval-waiting, cancelled, and successful runs are not restarted by this command.');
    }
    if (!(Date.parse(comment.created_at) >= Date.parse(run.created_at))) return publish('That retry command predates the current CI run. Comment `/ci retry` again if it still applies.');
    if (run.run_attempt >= 3) return publish('This run has reached the three-attempt limit. Fix the failure and push a commit, or ask a maintainer to inspect it.');
    if (state.retryAt && now - state.retryAt < 300_000) return publish('Please wait five minutes between retry requests.');
  }

  const current = await client.get('/pulls/{pull_number}', { pull_number: number });
  if (current.state !== 'open' || current.head.sha !== pr.head.sha) return;
  const freshRun = await client.get('/actions/runs/{run_id}', { run_id: run.id });
  if (!matches(freshRun, pr, workflow.id) || freshRun.status !== run.status
    || freshRun.conclusion !== run.conclusion || freshRun.run_attempt !== run.run_attempt) {
    return publish('CI changed while processing the command. No duplicate action was taken; use `/ci` to refresh.');
  }
  if (cmd.action === 'retry') state.retryAt = now;
  // Record consumption before the API call, so redelivered events are idempotent.
  if (!await publish('Submitting the CI request…')) return;
  try {
    const endpoint = cmd.action === 'approve' ? 'approve' : 'rerun-failed-jobs';
    await client.write('POST', `/actions/runs/{run_id}/${endpoint}`, { run_id: run.id });
    return publish('The run link above will show progress. This comment updates when CI starts and finishes.',
      cmd.action === 'approve' ? 'Approval submitted' : 'Retry submitted');
  } catch (error) {
    return publish(`GitHub did not accept the request (HTTP ${Number(error.status) || 'unknown'}). Open the CI run for details or ask a maintainer.`);
  }
}

module.exports = { resolve, report };
