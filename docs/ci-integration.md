# CI Integration Guide

dbt 프로젝트에서 dbt-plan을 CI에 붙이는 방법입니다.

## GitHub Actions

### 기본 설정

```bash
dbt-plan ci-setup      # .github/workflows/dbt-plan.yml 생성
```

새 생성기는 다음 마이너 릴리스에 포함됩니다. 기존 워크플로는 패키지를 업그레이드해도
바뀌지 않습니다. 적용할 때는 기존 파일을 백업하고 새로 생성한 파일과 비교해 자격증명과
프로젝트별 설정을 옮기세요. `ci-setup`은 기존 파일을 덮어쓰지 않습니다.
[체크인된 예제](../examples/ci-workflow/dbt-plan.yml)는 생성 결과와 동일하며 테스트로 확인합니다.

### 의존성 설치

저장소 루트의 dbt 프로젝트를 기준으로 다음 순서로 설치합니다.

1. `pyproject.toml`이 있으면 `uv sync`를 사용합니다. `uv.lock`도 있으면 `--locked`로
   잠금 파일과 설정이 일치하는지 확인하며, 잠금 파일을 자동으로 갱신하지 않습니다.
   원래 잠금 파일이 없었다면 설치 중 생긴 임시 `uv.lock`은 제거해 base 리비전의 파일과
   충돌하지 않게 합니다.
2. `pyproject.toml`이 없고 `requirements.txt`가 있으면 해당 파일의 의존성을 설치합니다.
3. 둘 다 없거나 설치 결과에 dbt가 없으면 설명을 출력하고 실패합니다.

프로젝트 의존성에 dbt와 사용하는 어댑터가 있어야 합니다. dbt가 선택적 의존성이나 별도
그룹에 있다면 Install의 `uv sync` 옵션을 조정하세요. `packages.yml` 등으로 dbt 패키지를
사용한다면 각 리비전의 `dbt compile` 전에 필요한 `dbt deps`도 추가하세요.

프로젝트 의존성과 dbt-plan은 `$RUNNER_TEMP/dbt-plan-venv`에 함께 설치합니다.
`GITHUB_PATH`로 이후 단계가 이 환경의 `dbt`, `dbt-plan`, `python`을 사용하게 합니다.
리비전을 바꿔도 환경이 유지되며, `uv run`의 재동기화로 dbt-plan이 제거되는 일이 없습니다.
자격증명은 아래 [자격증명](#자격증명) 절을 참고해 설정하세요.

### 보고와 차단 정책

Check current는 현재 리비전을 컴파일한 뒤 JSON 보고서를 검사하고 종료 코드를 저장합니다.
Report는 GitHub step summary에 결과를 출력합니다. Markdown 출력이 실패하면 원래 JSON을
표시하며, Report 자체가 실패해도 Gate는 저장된 결과로 판정합니다.

Gate 단계의 `env.FAIL_ON`을 바꾸면 정책을 선택할 수 있습니다.

| 값 | 동작 |
|---|---|
| `destructive` (기본값) | 파괴적 변경만 차단하고 경고는 보고 |
| `warning` | 파괴적 변경과 경고 모두 차단 |
| `never` | 완료된 검사 결과는 보고만 함 |

컴파일·snapshot·검사 실행 오류는 어느 정책에서도 통과시키지 않습니다. 구버전이 오류에
1이나 2를 반환하더라도 JSON 보고서가 없거나 잘못됐다면 실행 오류로 처리합니다.
`warning_exit_code: 0`은 기존처럼 경고를 허용하며, 0/1/2 외 사용자 지정 경고 코드는
이 워크플로에서 실행 오류로 차단합니다. CLI의 기본 경고 코드는 여전히 2입니다.

PR 코멘트는 기본 생성 구성에 포함되지 않습니다. 결과는 Actions의 step summary에서
확인할 수 있으며, JSON 원본은 해당 작업의 `$RUNNER_TEMP/dbt-plan-report.json`에 있습니다.
별도로 코멘트를 게시하려면 토큰 권한과 fork PR 처리도 해당 워크플로에서 구성해야 합니다.

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
| 0 | 설정된 정책상 통과 | 통과 |
| 1 | 파괴적 (DESTRUCTIVE) | merge 차단 |
| 2 | 검토 필요 (WARNING) | 새 생성 워크플로와 Action의 기본 정책에서는 허용; 기본 CLI에서는 실패 |
| 3 | 실행 오류 (ERROR), 완료된 판정 없음 | 차단; `fail-on: never`도 허용하지 않음 |

실행 오류 3은 다음 마이너 릴리스부터 적용합니다. 기존 0.15.x의 오류 2와
구분해야 합니다. [종료 코드 전환 안내](exit-codes.md)를 참고하세요.

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

Slack 알림은 생성 구성에 포함되지 않습니다. 필요하면 저장된 검사 결과를 읽어
별도 알림 단계에서 처리하세요.

## 컴파일 명령과 dbt 패키지

`dbt-plan run`의 `--compile-command`, `DBT_PLAN_COMPILE_COMMAND`,
`.dbt-plan.yml`의 `compile_command`는 **명령 하나와 인자**를 받습니다.
문자열을 `shlex.split`으로 나눈 뒤 셸 없이 실행하므로 `&&`, 파이프, `$VAR`
확장을 처리하지 않습니다. `dbt deps && dbt compile`을 그대로 넣으면 `&&`도
`dbt`의 인자로 전달됩니다.

GitHub Action의 `compile-command` 입력은 현재 Bash의 `eval`로 실행하는
**셸 명령문**입니다. CLI 설정 파일이나 환경 변수를 자동으로 읽는 입력이 아니며,
CLI와 실행 방식도 다릅니다. 생성 워크플로의 `run:` 단계 역시 Bash 명령문입니다.

두 환경에서 같은 동작을 원하면 실행 파일을 사용하세요. 예를 들어 프로젝트의
`scripts/dbt-compile`을 다음과 같이 만들고 실행 권한을 줍니다.

```sh
#!/bin/sh
set -eu
if [ "${1:-}" = "--version" ]; then
    exec dbt --version
fi
if [ -f packages.yml ] || [ -f dependencies.yml ]; then
    dbt deps
fi
exec dbt compile "$@"
```

```bash
chmod +x scripts/dbt-compile
dbt-plan run --compile-command ./scripts/dbt-compile
```

CLI는 작업 트리를 바꾸기 전에 명령의 실행 파일에 `--version`을 전달해 확인하므로
위 분기가 필요합니다. 스크립트는 비교할 두 리비전 모두에 있어야 합니다.
Action에는 `compile-command: ./scripts/dbt-compile`을 지정하고, 생성 워크플로는
기준선과 현재 리비전 양쪽의 `dbt compile`을 이 스크립트 호출로 바꾸세요.
`packages.yml` 또는 `dependencies.yml`로 패키지를 쓰는 프로젝트의 `dbt deps`는
현재 자동 실행되지 않습니다. 리비전마다 패키지 구성이 달라질 수 있으므로 한 번만
설치하지 말고 각각의 컴파일 전에 실행해야 합니다.

[분석 범위와 한계](analysis-limits.md)도 확인하세요.
