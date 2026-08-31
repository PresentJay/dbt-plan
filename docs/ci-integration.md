# CI Integration Guide

dbt 프로젝트에서 dbt-plan을 CI에 붙이는 방법입니다.

## GitHub Actions

### 기본 설정

```bash
dbt-plan ci-setup      # .github/workflows/dbt-plan.yml 생성
```

자격증명 설정과 보안 주석이 포함된 워크플로가 생성됩니다.
아래 [자격증명](#자격증명) 절을 참고해 secret을 채우고 push하면 끝입니다.

이 문서는 생성된 워크플로를 그대로 옮겨 적지 않습니다 — 복붙본은 반드시 원본과 어긋납니다.
아래는 그 위에 얹는 **차이분**만 다룹니다.

### PR 코멘트 추가 (선택)

생성된 워크플로는 결과를 step summary로 냅니다. PR 코멘트로도 남기려면 job 권한에
`pull-requests: write`를 더하고, `Check current`와 `Gate` 사이에 아래 두 스텝을 넣으세요.

```yaml
    permissions:
      contents: read
      pull-requests: write   # PR 코멘트를 남길 때만 필요

    # ... steps: 안, Check current 다음 / Gate 앞
      - name: Run DDL check
        id: plan
        continue-on-error: true
        run: |
          dbt-plan check --format github > /tmp/dbt-plan-output.md || true
          echo "result<<EOF" >> $GITHUB_OUTPUT
          cat /tmp/dbt-plan-output.md >> $GITHUB_OUTPUT
          echo "EOF" >> $GITHUB_OUTPUT

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
```

`continue-on-error: true`라서 코멘트는 항상 올라가고, 뒤따르는 `Gate` 스텝이 exit code로 막습니다.

### 의도적인 파괴적 변경 허용 (선택)

라벨이 붙은 PR을 통째로 건너뛰려면 job에 조건을 답니다.

```yaml
    if: "!contains(github.event.pull_request.labels.*.name, 'ddl-reviewed')"
```

## 환경 설정

### 필수

| 항목 | 설명 |
|------|------|
| `dbt compile` 가능 | CI에서 dbt compile이 동작해야 함 (profiles.yml, credentials) |
| `fetch-depth: 0` | base branch checkout을 위해 전체 히스토리 필요 |

## 자격증명

**dbt-plan은 warehouse에 접속하지 않습니다. `dbt compile`이 접속합니다.**
따라서 CI에 넣을 자격증명은 dbt-plan의 요구사항이 아니라, 평소 `dbt compile`에 쓰던 것 그대로입니다.

profiles.yml이 `env_var()`로 읽는 값을 job 레벨 `env:` 블록에 선언하세요:

```yaml
    env:
      SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
      SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
      SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
```

`run:` 블록 안에서 `${{ secrets.* }}`를 직접 쓰지 마세요 — 평문이 커맨드라인에 올라갑니다.

### 최소 권한

`dbt compile`은 **테이블을 읽지 않습니다.** 로그인 + warehouse USAGE면 컴파일됩니다.
매크로가 introspection을 하면 그때만 read 권한이 필요합니다. 확인:

```bash
grep -rn "run_query\|get_column_values\|get_columns_in_relation\|adapter.get_relation" macros/ models/
```

0건이면 아무 grant도 없는 계정 하나로 충분합니다. 있으면 그 대상 테이블만 read를 주세요.

### `pull_request_target` 금지

`dbt compile`은 **PR에 담겨온 Jinja와 매크로를 실행합니다.** 트리거를 `pull_request_target`으로
바꾸면 그 코드가 warehouse 자격증명을 쥔 채로 돌아갑니다. `pull_request`를 유지하세요.

Fork PR은 설계상 secret을 받지 못하므로 compile이 실패합니다. 생성된 워크플로의 Preflight
스텝이 드라이버 에러 대신 그 사실을 명시적으로 알려줍니다.

### 그 밖의 warehouse

| Warehouse | 권장 방식 |
|-----------|----------|
| Snowflake | key-pair (`SNOWFLAKE_PRIVATE_KEY`) — password보다 우선 |
| BigQuery | `google-github-actions/auth` OIDC — secret 자체가 불필요 |
| Postgres / Redshift | `PGPASSWORD` secret |

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
