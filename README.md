# Korean Bill & Debate MCP

> **법안 제목만 보지 마세요. 누가 밀었고, 누가 막았고, 정부는 뭐라고 답했는지까지.**
>
> 질문 한 줄이면 최신 처리상태부터 위원회·소위원회, 의원 발언과 바로 앞뒤 질의·답변을
> 열린국회 공식 원문으로 연결합니다.

한국어 중심 · 사용자 본인의 열린국회 API 키 · 실시간 조회 · 로컬 캐시 · Apache-2.0

[English](README.en.md) · [MCP 연결](docs/mcp-clients.md) ·
[데이터 출처](docs/data-sources.md) · [아키텍처](docs/architecture.md)

![질문 하나로 법안부터 실제 발언과 앞뒤 맥락까지 추적하는 데모](assets/demo.gif)

## 법안 검색이 끝나는 곳에서, 조사가 시작됩니다

```text
“검찰 보완수사권 폐지 관련 법안의 최신 상태와 의원들의 의견을 보여줘”
“이 법안이 법사위 소위에서 어떻게 논의됐고 정부는 뭐라고 답했어?”
“플랫폼 노동자 보호에 관한 서로 다른 의원 의견을 앞뒤 맥락과 비교해줘”
“2219564번 의안의 처리상태와 연결된 회의록 원문을 찾아줘”
```

MCP는 질문할 때 열린국회 공식 API를 조회합니다. 관련 의안과 처리상태에서 소관 위원회와
시점을 찾고, 필요한 공식 회의록만 내려받아 발언자·질의·답변·후속질의를 복원합니다.

```text
자연어 질문
  → 공식 의안·처리상태 실시간 조회
  → 관련 위원회·본회의·소위원회 후보 탐색
  → 필요한 회의록만 다운로드·파싱
  → 법안–회의–사람–발언–답변 연결
  → 공식 의안·회의록 URL과 원문 위치 반환
```

## 준비: 열린국회 API 키

사용자는 [열린국회정보 Open API](https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do)에서
본인의 인증키를 발급받아야 합니다. 키는 사용자의 컴퓨터에만 저장하며 저장소, 로그, 검색
결과에 포함하지 않습니다.

## 빠른 설치

`uv`와 PDF 텍스트 추출 도구가 필요합니다.

```bash
# macOS
brew install uv poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
```

설정 마법사는 키를 가려서 입력받고, 실제 열린국회 요청으로 검증한 뒤 선택한 클라이언트에
MCP를 등록합니다.

```bash
uvx korean-bill-debate-mcp setup --client claude-code
uvx korean-bill-debate-mcp setup --client codex
uvx korean-bill-debate-mcp setup --client gemini
uvx korean-bill-debate-mcp setup --client claude-desktop
```

키는 기본적으로 다음 파일에 사용자 전용 권한(`0600`)으로 저장됩니다.

```text
~/.config/korean-bill-debate-mcp/credentials.env
```

## 수동 MCP 설정

환경변수로 직접 전달할 수도 있습니다.

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

Claude Code와 Codex CLI의 상세 명령, Claude Desktop 및 웹 클라이언트 연결 방식은
[MCP 연결 가이드](docs/mcp-clients.md)를 참고하세요.

## MCP 도구

| 도구 | 실시간 조사 내용 |
|---|---|
| `explore_issue` | 쟁점에서 법안·상태·회의·발언·앞뒤 문맥까지 한 번에 조사 |
| `search_bills` | 열린국회에서 법안·의안 검색 후 로컬 캐시에 정규화 |
| `get_bill_status` | 공식 처리상태 API를 다시 호출해 최신 상태 확인 |
| `search_speeches` | 관련 회의를 탐색하고 필요한 회의록을 수집한 뒤 발언 검색 |
| `get_speech` | 캐시에 저장된 발언 전문과 공식 출처 확인 |
| `get_speech_context` | 발언 전후 질의·답변·후속질의 복원 |
| `list_committees` | 공식 회의 메타데이터에서 위원회 확인 |
| `list_meetings` | 날짜·위원회·회의 종류로 공식 회의 탐색 |

## 캐시는 보조 수단입니다

전체 국회 DB나 준비된 검색 인덱스를 배포하지 않습니다. `SQLite` 캐시는 사용자가 이미 조회한
API 응답, 회의록, 구조화 발언을 재사용해 반복 요청을 빠르게 합니다.

- 새 질문은 공식 API에서 후보를 다시 탐색합니다.
- 법안 처리상태는 공식 상태 API를 우선합니다.
- API 캐시는 기본 15분 후 만료됩니다.
- 회의록 원문은 공식 URL과 SHA-256을 함께 보존합니다.
- 기본 캐시 위치는 `~/.local/share/korean-bill-debate-mcp`입니다.
- `KBD_DATA_DIR`로 위치를 바꿀 수 있습니다.

## 개발

```bash
uv sync --extra dev
ASSEMBLY_OPEN_API_KEY=본인키 uv run kbd research "검찰 보완수사권 폐지"
uv run pytest
uv run ruff check .
uv run mypy src
```

fixture 테스트는 API 키 없이 실행됩니다. 실제 API smoke test는 저장소 Secret 또는 개발자의
환경변수를 사용하며, 키를 출력하지 않습니다.

## 범위와 검증 원칙

이 프로젝트는 공식 근거를 검색하고 연결하는 조사 도구입니다. 의원의 찬반 입장을 임의로
추정하지 않으며 실제 발언과 앞뒤 문맥을 제공합니다. 모든 인용은 `citation.official_url`과
`source_locator`로 원문을 확인해야 합니다.

열린국회 API가 검색 조건으로 제공하지 않는 자유어 전체 회의록 검색은 관련 법안·위원회·
시점을 먼저 좁힌 뒤 제한된 수의 회의록을 동적으로 분석합니다. 첫 조회는 PDF 다운로드와
파싱 때문에 느릴 수 있으며, 이후 같은 자료는 로컬 캐시를 재사용합니다.
