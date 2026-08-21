# CI Integration Guide

dbt 프로젝트에서 dbt-plan을 CI에 붙이는 방법입니다.

## GitHub Actions

### 기본 설정

아래 파일을 dbt 프로젝트의 `.github/workflows/dbt-plan.yml`에 복사하세요.

```yaml
name: dbt-plan
on:
  pull_request:
    paths: ['models/**', 'macros/**', 'dbt_project.yml']

concurrency:
  group: dbt-plan-${{ github.ref }}
  cancel-in-progress: true

jobs:
  plan:
    runs-on: ubuntu-latest

    # ddl-reviewed 라벨이 있으면 skip (의도적 파괴적 변경)
    if: "!contains(github.event.pull_request.labels.*.name, 'ddl-reviewed')"

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install uv
          uv sync --group dbt
          pip install git+https://github.com/PresentJay/dbt-plan

      # Base branch: compile + snapshot
      - name: Compile base
        run: |
          git checkout ${{ github.event.pull_request.base.sha }}
          dbt compile
          dbt-plan snapshot

      # Current branch: compile + check
      - name: Compile current
        run: |
          git checkout ${{ github.event.pull_request.head.sha }}
          dbt compile

      - name: Run DDL check
        id: plan
        continue-on-error: true
        run: |
          dbt-plan check --format github > /tmp/dbt-plan-output.md || true
          echo "result<<EOF" >> $GITHUB_OUTPUT
          cat /tmp/dbt-plan-output.md >> $GITHUB_OUTPUT
          echo "EOF" >> $GITHUB_OUTPUT

      # PR comment (update existing or create new)
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            const body = `<!-- dbt-plan -->\n${process.env.RESULT}`;
            const { data: comments } = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
            });
            const existing = comments.find(c => c.body.includes('<!-- dbt-plan -->'));
            if (existing) {
              await github.rest.issues.updateComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                comment_id: existing.id,
                body,
              });
            } else {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body,
              });
            }
        env:
          RESULT: ${{ steps.plan.outputs.result }}

      # Block on destructive (separate step so PR comment always posts)
      - name: Block destructive changes
        run: dbt-plan check
```

### 최소 설정 (PR 코멘트 없이)

```yaml
name: dbt-plan
on:
  pull_request:
    paths: ['models/**']

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - run: pip install git+https://github.com/PresentJay/dbt-plan
      - run: |
          git checkout ${{ github.event.pull_request.base.sha }}
          dbt compile && dbt-plan snapshot
      - run: |
          git checkout ${{ github.event.pull_request.head.sha }}
          dbt compile
      - run: dbt-plan check --format github >> $GITHUB_STEP_SUMMARY
      - run: dbt-plan check
```

## 환경 설정

### 필수

| 항목 | 설명 |
|------|------|
| `dbt compile` 가능 | CI에서 dbt compile이 동작해야 함 (profiles.yml, credentials) |
| `fetch-depth: 0` | base branch checkout을 위해 전체 히스토리 필요 |

### 선택 (Phase 1b)

| 환경변수 | 설명 |
|----------|------|
| `SNOWFLAKE_ACCOUNT` | INFORMATION_SCHEMA 조회용 |
| `SNOWFLAKE_USER` | Snowflake 인증 |
| `SNOWFLAKE_PRIVATE_KEY` | Snowflake 인증 |
| `SNOWFLAKE_DATABASE` | 대상 데이터베이스 |
| `SNOWFLAKE_SCHEMA` | 대상 스키마 (기본: DBT) |

## Exit Codes

| Code | 의미 | CI 동작 |
|------|------|---------|
| 0 | 안전 (SAFE) | 통과 |
| 1 | 파괴적 (DESTRUCTIVE) | merge 차단 |
| 2 | 경고/오류 (WARNING) | 통과 (경고만) |

## Override: `ddl-reviewed` 라벨

의도적으로 컬럼을 삭제하는 PR이라면:

1. PR에 `ddl-reviewed` 라벨 추가
2. 워크플로우가 자동으로 skip됨
3. PR 코멘트에는 여전히 DDL 예측이 표시됨 (정보 제공)

## Self-hosted Runner 참고

- `concurrency` 설정으로 같은 PR에 대한 동시 실행 방지
- Snowflake credentials는 AWS Secrets Manager 또는 GitHub Secrets 사용
- dbt compile 캐시: `.dbt/` 디렉토리를 actions/cache로 캐시하면 빨라짐

## 알림 설정 (선택)

### GitHub Step Summary (기본)

```yaml
- run: dbt-plan check --format github >> $GITHUB_STEP_SUMMARY
```

PR의 Actions 탭에서 결과를 볼 수 있습니다.

### Slack Webhook (Phase 2a)

destructive DDL 발생 시 Slack으로 알림:

```yaml
- name: Notify Slack on destructive
  if: failure()
  run: |
    curl -X POST ${{ secrets.SLACK_WEBHOOK_URL }} \
      -H 'Content-Type: application/json' \
      -d '{"text": "dbt-plan: destructive DDL detected in PR #${{ github.event.number }}"}'
```
