# Security policy

Do not open a public issue for a credential leak, code-execution issue, path traversal, unsafe
URL fetch, or remote MCP denial-of-service vulnerability. Use GitHub's private vulnerability
reporting for this repository.

Supported security fixes target the latest release. Reports should include affected version,
impact, reproduction, and suggested mitigation. Never include a live Open Assembly key.

The public search process must not receive `ASSEMBLY_OPEN_API_KEY`. It accepts only prepared
read-only indexes. Synchronization permits only official Open Assembly metadata and
`record.assembly.go.kr` PDF hosts; deployments should terminate TLS and rate-limit at ingress.
