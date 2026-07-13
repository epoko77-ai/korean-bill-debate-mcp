# MCP 클라이언트 연결

## Claude.ai·ChatGPT 웹 — 설치 없음

연결 페이지: [Korean Bill & Debate MCP](https://korean-bill-debate-mcp.vercel.app)

1. 연결 페이지에 본인의 열린국회 API 키를 입력합니다.
2. 발급된 개인 MCP 링크 전체를 복사합니다.
3. Claude는 **설정 → 커넥터 → 커스텀 커넥터 추가**에 붙여 넣습니다.
4. ChatGPT는 **설정 → 앱 → 고급 설정 → 개발자 모드 → 앱 만들기**의 서버 URL에 붙여
   넣습니다.
5. 채팅의 `+` 메뉴에서 추가한 커넥터 또는 앱을 활성화합니다.

키 없는 `/mcp` 주소는 동작하지 않습니다. 연결 페이지가 발급한 `?token=...`까지 포함된
주소를 사용하세요. 서버는 API 키 원문을 DB나 파일에 저장하지 않습니다. 개인 링크는 API
할당량을 사용할 수 있으므로 외부에 공유하지 마세요.

## 로컬 설치 공통 준비

1. 열린국회에서 본인 API 키를 발급받습니다.
2. `uv`를 설치합니다. Poppler(`pdftotext`)는 선택사항이지만 설치하면 PDF 처리가 더 빠릅니다.
3. 아래 자동 설정 또는 수동 설정 중 하나를 사용합니다.

키를 명령행 인자로 쓰면 셸 기록에 남을 수 있으므로 설정 마법사의 가려진 입력이나 MCP
설정의 환경변수를 사용하세요.

## 설치

PyPI 배포 전에는 검증된 GitHub 릴리스를 직접 설치합니다.

```bash
uv tool install git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.8.0
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

## Claude Code

자동 설정을 사용하지 않는 경우:

```bash
claude mcp add --scope user korean-bill-debate \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.8.0 kbd mcp
claude mcp get korean-bill-debate
```

Claude Code에서 `/mcp`를 실행해 8개 도구를 확인합니다.

## Codex CLI

```bash
codex mcp add korean-bill-debate \
  --env ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.8.0 kbd mcp
codex mcp get korean-bill-debate
codex mcp list
```

Codex 안에서는 `/mcp`로 연결 상태를 확인합니다.

## Gemini CLI

```bash
gemini mcp add korean-bill-debate \
  -e ASSEMBLY_OPEN_API_KEY=본인의_키 -- \
  uvx --from git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.8.0 kbd mcp
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
        "git+https://github.com/epoko77-ai/korean-bill-debate-mcp.git@v0.8.0",
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
- **PDF 처리가 느림**: 내장 Python 추출기로도 동작하지만 macOS는 `brew install poppler`,
  Ubuntu는 `sudo apt-get install poppler-utils`를 실행하면 더 빠릅니다.
- **첫 질문이 느림**: 관련 회의록 PDF를 처음 내려받아 파싱하는 과정입니다.
- **검색 범위 부족**: 질문에 법안명·위원회·연도나 월을 추가하면 공식 회의 후보를 더 정확히
  좁힐 수 있습니다.
- **키 노출 우려**: 로그와 API 출처 URL에서는 `KEY=***`으로 마스킹됩니다. 키가 포함된 설정
  파일은 공유하거나 커밋하지 마세요.
