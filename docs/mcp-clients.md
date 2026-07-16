# MCP 클라이언트 연결

## Claude.ai·ChatGPT 웹 — 설치 없음

먼저 [열린국회](https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do)에서 본인의 API
키를 발급받습니다.

### Claude.ai — OAuth 연결

원격 커스텀 커넥터는 Claude Free·Pro·Max·Team·Enterprise에서 제공되며, Free는 커스텀
커넥터 1개까지 사용할 수 있습니다. 최신 화면은
[Anthropic 공식 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)를
확인하세요.

1. 개인 계정은 **Customize → Connectors → + → Add custom connector**로 이동합니다.
   Team·Enterprise에서는 소유자가 **Organization settings → Connectors**에서 먼저 추가하고,
   사용자는 **Customize → Connectors**에서 연결합니다.
2. 이름은 `Korean Bill & Debate`, URL은 아래 주소를 입력합니다.

   ```text
   https://korean-bill-debate-mcp.vercel.app/mcp
   ```

3. 연결 승인 화면이 열리면 본인의 열린국회 API 키를 입력하고 승인합니다.
4. 새 채팅의 **검색 및 도구(Search and tools)**에서 커넥터와 필요한 도구를 켭니다.

Claude에는 개인 `/mcp/t/...` 링크를 붙이지 않습니다. 공용 `/mcp` 주소에서 시작해야 Claude가
OAuth 메타데이터를 발견하고 승인 절차를 완료할 수 있습니다.

### ChatGPT — OAuth 연결

현재 OpenAI 공식 안내상 전체 MCP 앱 기능은 ChatGPT Business·Enterprise·Edu 웹에서
제공됩니다. Pro도 개발자 모드에서 read/fetch MCP를 연결할 수 있으며, 이 서버의 모든 도구는
MCP 스키마에 읽기 전용으로 선언됩니다. 관리자 정책과 역할에 따라 메뉴가 보이지 않을 수
있습니다. 최신 화면은
[OpenAI 공식 안내](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)를
확인하세요.

1. Pro 사용자와 Business 관리자·소유자는 **Settings → Apps → Advanced settings**에서 개발자
   모드를 켜거나 **Workspace settings → Apps → Create**에서 시작합니다.
2. Enterprise·Edu는 관리자가 **Workspace Settings → Permissions & Roles → Connected Data**에서
   권한을 부여한 뒤, 사용자가 **Settings → Apps → Advanced Settings**에서 개발자 모드를 켭니다.
   한국어 개인 UI가 **설정 → 보안 및 로그인 → 개발자 모드**, **플러그인 → +**로 보이면
   같은 기능이므로 그 경로를 사용합니다.
3. **Settings → Apps → Create**, **Workspace settings → Apps → Create** 또는
   **플러그인 → +**에서 이름은 `Korean Bill & Debate`, MCP 서버 URL은 Claude와 같은 공용
   `/mcp` 주소를 입력합니다.
4. **Scan Tools**를 누르고 연결 승인 화면에 본인의 열린국회 API 키를 입력합니다. OAuth가
   끝난 뒤 13개 도구 스캔이 완료될 때까지 기다린 다음 **Create**를 누릅니다.
5. 새 채팅에서 `+ → More`로 앱을 선택하거나 `@`로 호출합니다.

서버에 도구가 추가된 뒤 기존 앱에서 보이지 않으면 앱의 **Refresh**로 actions를 다시 읽고
새 도구의 사용 설정을 검토하세요. ChatGPT는 이전에 승인된 도구 스냅샷을 유지할 수 있습니다.

`연결됨`은 계정에 등록됐다는 뜻이고, 현재 채팅에서 켜졌다는 뜻은 아닙니다. 새 채팅의 입력창
아래 `+` 또는 **도구** 메뉴에서 `Korean Bill & Debate`를 선택하세요. “이 대화에서는 MCP가
호출 가능한 도구로 잡히지 않는다”는 안내가 나오면 현재 채팅에 앱이 활성화되지 않은 상태입니다.

기존 `?token=...` 또는 `/mcp/t/...` 주소를 Claude.ai나 ChatGPT에 등록했다면 연결을 삭제하고
공용 `/mcp` 주소로 다시 등록하세요. 서버는 API 키 원문을 DB나 파일에 저장하지 않습니다.
호환용 개인 링크는 API 할당량을 사용할 수 있으므로 외부에 공유하지 마세요.

공용 `/mcp`는 OAuth discovery·동적 등록·PKCE 승인·access/refresh token 흐름을 사용합니다.
첫 화면의 `/connect` 폼은 OAuth를 못 쓰는 클라이언트용이며, 키를 열린국회에 실제로 확인한
뒤 암호화된 `/mcp/t/...` 개인 링크를 발급합니다. 두 흐름을 섞지 마세요.

## 로컬 설치 공통 준비

1. 열린국회에서 본인 API 키를 발급받습니다.
2. `uv`를 설치합니다. Poppler(`pdftotext`)는 선택사항이지만 설치하면 PDF 처리가 더 빠릅니다.
3. 아래 자동 설정 또는 수동 설정 중 하나를 사용합니다.

키를 명령행 인자로 쓰면 셸 기록에 남을 수 있으므로 설정 마법사의 가려진 입력이나 MCP
설정의 환경변수를 사용하세요.

## 설치

PyPI 배포 전에는 검증된 GitHub 릴리스를 직접 설치합니다.

```bash
uv tool install git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0
```

## 자동 설정

```bash
kbd setup --client claude-code
kbd setup --client codex
kbd setup --client gemini
kbd setup --client claude-desktop
```

마법사는 API 키를 실제 열린국회 요청으로 검증하고 다음 로컬 파일에 `0600` 권한으로
저장합니다.

```text
~/.config/korean-bill-debate-mcp/credentials.env
```

키는 `--api-key`, `ASSEMBLY_OPEN_API_KEY`, 대화형 가림 입력 순서로 읽습니다. 비대화형
셸에서 키가 없으면 프롬프트를 기다리지 않고 오류로 종료합니다. `--credentials-file`을 쓰면
자동 등록에도 `KBD_CREDENTIALS_FILE`이 전달되어 같은 파일을 읽습니다.

## Claude Code

자동 설정을 사용하지 않는 경우:

```bash
claude mcp add --scope user korean-bill-debate \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0 kbd mcp
claude mcp get korean-bill-debate
```

Claude Code에서 `/mcp`를 실행해 8개 도구를 확인합니다.

## Codex CLI

```bash
codex mcp add korean-bill-debate \
  --env ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0 kbd mcp
codex mcp get korean-bill-debate
codex mcp list
```

Codex 안에서는 `/mcp`로 연결 상태를 확인합니다.

## Gemini CLI

```bash
gemini mcp add --scope user \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 korean-bill-debate -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0 kbd mcp
gemini mcp list
```

Gemini CLI 안에서는 `/mcp list`를 사용합니다.

## Claude Desktop

**Settings → Developer → Edit Config**에서 다음을 추가합니다.

```json
{
  "mcpServers": {
    "korean-bill-debate": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v1.1.0",
        "kbd",
        "mcp"
      ],
      "env": {
        "ASSEMBLY_OPEN_API_KEY": "본인의_열린국회_키"
      }
    }
  }
}
```

저장한 뒤 Claude Desktop을 완전히 재시작합니다.

## 연결 확인

로컬 `kbd setup`의 live-cache 호환 서버에는 다음 8개가 표시됩니다.

```text
explore_issue, search_bills, get_bill_status, search_speeches,
get_speech, get_speech_context, list_committees, list_meetings
```

완전한 durable hosted 설정에는 아래 5개가 더해져 총 13개가 표시됩니다.

```text
start_research, get_research_status, get_research_overview,
get_research_page, get_evidence_document
```

공용 웹 연결에서 8개만 보인다면 연결 실패가 아니라 서버가 durable worker·artifact 설정 없는
호환 모드로 배포된 것입니다. 이때는 durable 조사 완료·coverage 경로를 사용할 수 없습니다.
ChatGPT에서 서버 업데이트 뒤에도 8개만 보이면 먼저 앱 actions를 Refresh하세요.

그 다음 실행합니다.

```text
2219564번 의안의 최신 처리상태를 열린국회에서 확인하고 공식 링크를 보여줘.
```

이어서 동적 회의록 조사를 확인합니다.

```text
검찰 보완수사권 폐지 관련 법안과 의원들의 의견을 찾아서
앞뒤 질의·답변과 공식 회의록 링크까지 정리해줘.
```

응답의 `data_mode`는 `live_open_assembly_with_local_cache`, `live_checked_at`은 이번 조회
시각이어야 합니다.

## 역대 국회·발의자 조사 확인

완전한 13개 도구 hosted 연결의 `start_research`는 제1대부터 제22대까지 공식 임기 경계를
사용합니다. 대수·날짜·정확한 의안번호를 쓰지 않으면 현재 제22대를 기본값으로 사용합니다.
다음처럼 대수, 연속 범위, 서로 떨어진 비교 대수, 날짜 범위를 자연어로 명시할 수 있습니다.

```text
제헌국회부터 제5대까지 경제 관련 법안과 본회의 논의를 대수별로 비교해줘.
제18대와 제22대 인공지능 입법을 비교해줘.
1961년 5월부터 1964년 1월까지 경제 법안과 회의록을 조사해줘.
```

발의자 이름은 역할 표현과 함께 써야 하며, 이름 전체를 정확히 일치시킵니다.

```text
제18대 강명순 의원이 대표발의한 법안과 연결 회의를 찾아줘.
김윤 의원이 공동발의한 인공지능 법안을 찾아줘.
박정 의원이 발의한 법안을 찾아줘.
```

`대표발의`는 공식 `RST_PROPOSER`, `공동발의`는 `PUBL_PROPOSER`, 역할 없는 `발의`는 두
필드의 합집합에서 확인합니다. 이름과 주제를 함께 쓰면 둘 다 맞는 법안만 남습니다. 관련
회의는 비슷한 주제가 아니라 선택된 법안의 정확한 7자리 의안번호가 공식 안건에 있는 경우만
연결합니다. 의원 이름만 언급하고 발의 역할을 쓰지 않은 질문은 발의자 필터로 추측하지 않습니다.

결과의 `source_availability`도 확인하세요. `records_found`는 해당 공식 데이터셋 원자료 확인,
`no_records`는 모든 계획 페이지가 정상 완료된 해당 데이터셋의 0건, `incomplete`는 수집이
끝나지 않아 자료 유무를 단정할 수 없다는 뜻입니다. “해당 열린국회 데이터셋에서 확인된 자료
없음”은 국회 전체에 자료가 없다는 뜻이 아닙니다. 데이터셋별 실측 시작 대수와 소위원회 예외는
[공식 데이터 출처 문서](data-sources.md)를 참고하세요.

## 영어 질문 확인

`v0.8.0`부터 영어 질문을 그대로 사용할 수 있습니다.

```text
In July 2026, compare bills and opposing views on abolishing prosecutors'
supplementary investigation authority. Include surrounding Q&A and official sources.
```

응답 메타데이터에서 `query_language: en`, 한국어 검색어 `search_query_ko`,
`source_language: ko`를 확인할 수 있습니다. 연결된 AI는 답변을 영어로 작성하고 번역한
인용임을 표시하며, 한국어 공식 원문 URL을 함께 제시하도록 안내됩니다. 낯선 고유명사는
MCP의 선택 인자 `korean_query`로 한국어 검색어를 함께 전달할 수 있습니다.

## 문제 해결

- **키 누락**: `ASSEMBLY_OPEN_API_KEY is required`가 나오면 setup을 다시 실행합니다.
- **키 오류**: setup의 검증 단계에서 열린국회 오류 코드를 그대로 확인합니다.
- **이미 등록됨**: Claude Code·Codex·Gemini의 기존 등록이 현재 `v1.1.0` 명령과 정확히
  같으면 성공으로 처리합니다. 다른 명령이면 자동으로 덮어쓰지 않으므로 기존 등록을 확인한
  뒤 직접 삭제하거나 수정하세요.
- **PDF 처리가 느림**: 내장 Python 추출기로도 동작하지만 macOS는 `brew install poppler`,
  Ubuntu는 `sudo apt-get install poppler-utils`를 실행하면 더 빠릅니다.
- **첫 질문이 느림**: 관련 회의록 PDF를 처음 내려받아 파싱하는 과정입니다.
- **검색 범위 부족**: 질문에 법안명·위원회·연도나 월을 추가하면 공식 회의 후보를 더 정확히
  좁힐 수 있습니다.
- **키 노출 우려**: 로그와 API 출처 URL에서는 `KEY=***`으로 마스킹됩니다. 키가 포함된 설정
  파일은 공유하거나 커밋하지 마세요.
