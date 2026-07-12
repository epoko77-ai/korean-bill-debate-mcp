# MCP 클라이언트 연결

## 공통 준비

1. 열린국회에서 본인 API 키를 발급받습니다.
2. `uv`와 Poppler(`pdftotext`)를 설치합니다.
3. 아래 자동 설정 또는 수동 설정 중 하나를 사용합니다.

키를 명령행 인자로 쓰면 셸 기록에 남을 수 있으므로 설정 마법사의 가려진 입력이나 MCP
설정의 환경변수를 사용하세요.

## 자동 설정

```bash
uvx korean-bill-debate-mcp setup --client claude-code
uvx korean-bill-debate-mcp setup --client codex
uvx korean-bill-debate-mcp setup --client gemini
uvx korean-bill-debate-mcp setup --client claude-desktop
```

마법사는 API 키를 실제 열린국회 요청으로 검증하고 다음 로컬 파일에 `0600` 권한으로
저장합니다.

```text
~/.config/korean-bill-debate-mcp/credentials.env
```

## Claude Code

자동 설정을 사용하지 않는 경우:

```bash
claude mcp add --scope user korean-bill-debate \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx korean-bill-debate-mcp mcp
claude mcp get korean-bill-debate
```

Claude Code에서 `/mcp`를 실행해 8개 도구를 확인합니다.

## Codex CLI

```bash
codex mcp add korean-bill-debate \
  --env ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx korean-bill-debate-mcp mcp
codex mcp get korean-bill-debate
codex mcp list
```

Codex 안에서는 `/mcp`로 연결 상태를 확인합니다.

## Gemini CLI

```bash
gemini mcp add korean-bill-debate \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx korean-bill-debate-mcp mcp
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
      "args": ["korean-bill-debate-mcp", "mcp"],
      "env": {
        "ASSEMBLY_OPEN_API_KEY": "본인의_열린국회_키"
      }
    }
  }
}
```

저장한 뒤 Claude Desktop을 완전히 재시작합니다.

## Claude.ai, ChatGPT, Gemini 웹

웹 앱은 사용자의 컴퓨터에서 로컬 stdio 명령을 직접 실행할 수 없습니다. 별도의 HTTPS
Streamable HTTP MCP가 필요합니다. 이 프로젝트는 사용자 키를 로컬에서 사용하는 방식을
기본으로 하며, 현재 공용 원격 서버를 제공하지 않습니다.

직접 원격 서버를 운영한다면 각 사용자의 키를 안전하게 전달하는 인증 계층을 추가해야
합니다. 하나의 운영자 키를 모든 공개 사용자와 공유하는 배포는 열린국회 할당량과 키 보안
문제 때문에 권장하지 않습니다.

## 연결 확인

도구 목록에 다음 8개가 표시되는지 확인합니다.

```text
explore_issue, search_bills, get_bill_status, search_speeches,
get_speech, get_speech_context, list_committees, list_meetings
```

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

## 문제 해결

- **키 누락**: `ASSEMBLY_OPEN_API_KEY is required`가 나오면 setup을 다시 실행합니다.
- **키 오류**: setup의 검증 단계에서 열린국회 오류 코드를 그대로 확인합니다.
- **`pdftotext` 없음**: macOS는 `brew install poppler`, Ubuntu는
  `sudo apt-get install poppler-utils`를 실행합니다.
- **첫 질문이 느림**: 관련 회의록 PDF를 처음 내려받아 파싱하는 과정입니다.
- **검색 범위 부족**: 질문에 법안명·위원회·연도나 월을 추가하면 공식 회의 후보를 더 정확히
  좁힐 수 있습니다.
- **키 노출 우려**: 로그와 API 출처 URL에서는 `KEY=***`으로 마스킹됩니다. 키가 포함된 설정
  파일은 공유하거나 커밋하지 마세요.
