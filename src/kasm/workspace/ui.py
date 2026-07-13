"""Self-contained, script-isolated UI for the Korean legislative research workspace."""

# ruff: noqa: E501 - HTML, CSS, and JavaScript are kept readable as embedded assets.

from __future__ import annotations


def workspace_page() -> str:
    return _PAGE


def workspace_script() -> str:
    return _SCRIPT


_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="description" content="법안·처리상태·소위원회 회의록·전문위원 검토보고서·의원과 정부 발언을 한 번에 조사합니다.">
  <title>국회 입법조사 워크스페이스 · Korean Bill & Debate</title>
  <style>
    :root{--navy:#071a2e;--navy2:#0c2944;--ink:#102234;--paper:#f7f3ea;--paper2:#fffdf8;
      --line:#d9d3c8;--gold:#e0ad4f;--mint:#72c9b6;--muted:#647383;--danger:#b63f35}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);
      font:16px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}
    a{color:inherit}.hero{background:var(--navy);color:#fff;padding:22px 5vw 74px;position:relative;overflow:hidden}
    .hero:after{content:"";position:absolute;width:440px;height:440px;border:1px solid #31516a;border-radius:50%;
      right:-170px;top:-180px;box-shadow:0 0 0 65px #0a2239,0 0 0 66px #31516a}
    nav{max-width:1180px;margin:auto;display:flex;align-items:center;justify-content:space-between;position:relative;z-index:1}
    .brand{font-weight:900;letter-spacing:-.02em;text-decoration:none}.brand small{font-weight:600;color:#a9bdca;margin-left:8px}
    .navlinks{display:flex;gap:18px;color:#c4d2dc;font-size:14px}.navlinks a{text-decoration:none}
    .hero-inner{max-width:1180px;margin:70px auto 0;position:relative;z-index:1;display:grid;grid-template-columns:1.35fr .65fr;gap:50px}
    .eyebrow{color:var(--gold);font-weight:800;letter-spacing:.08em;font-size:13px;text-transform:uppercase}
    h1{font-size:clamp(40px,6.2vw,76px);letter-spacing:-.06em;line-height:1.08;margin:14px 0 24px;max-width:850px}
    .hero p{font-size:clamp(17px,2vw,21px);color:#c8d5de;max-width:760px;margin:0}
    .promise{align-self:end;border-left:1px solid #466076;padding-left:24px;color:#dce5ea}
    .promise b{display:block;color:#fff;font-size:20px;margin-bottom:8px}.promise span{color:#a9bdca;font-size:14px}
    main{max-width:1180px;margin:-34px auto 80px;padding:0 24px;position:relative;z-index:2}
    .steps{display:grid;grid-template-columns:repeat(3,1fr);background:var(--paper2);border:1px solid var(--line);border-radius:18px;
      box-shadow:0 16px 45px #071a2e18;overflow:hidden}
    .step{padding:22px 25px;border-right:1px solid var(--line);display:flex;gap:14px}.step:last-child{border:0}
    .number{flex:0 0 34px;height:34px;border-radius:50%;background:var(--navy);color:#fff;display:grid;place-items:center;font-weight:900}
    .step b{display:block}.step span{font-size:14px;color:var(--muted)}
    .workspace{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:28px;margin-top:30px;align-items:start}
    .panel{background:var(--paper2);border:1px solid var(--line);border-radius:18px;padding:30px}
    .panel h2{font-size:28px;letter-spacing:-.04em;margin:0 0 5px}.sub{color:var(--muted);margin:0 0 25px}
    .credential-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.field{margin-bottom:18px}
    label{display:flex;justify-content:space-between;gap:10px;font-weight:800;margin-bottom:7px}
    label a{font-weight:600;font-size:13px;color:#496678}.hint{font-weight:500;color:var(--muted);font-size:13px;margin-top:6px}
    input,select,textarea{width:100%;border:1px solid #b9b3a9;background:#fff;border-radius:10px;padding:13px 14px;
      font:inherit;color:var(--ink);outline:none;transition:.15s}input:focus,select:focus,textarea:focus{border-color:#2a6172;box-shadow:0 0 0 3px #72c9b633}
    textarea{min-height:132px;resize:vertical}.examples{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0 22px}
    .example{border:1px solid #c7c0b4;background:#f6f1e8;border-radius:999px;padding:7px 11px;font-size:13px;cursor:pointer;color:#405261}
    .consent{display:flex;align-items:flex-start;justify-content:flex-start;font-weight:500;gap:9px;background:#f2eee5;padding:12px;border-radius:10px}
    .consent input{width:auto;margin-top:6px}.run{width:100%;border:0;background:var(--navy);color:#fff;border-radius:11px;padding:15px 18px;
      font-weight:900;font-size:17px;cursor:pointer;margin-top:14px}.run:hover{background:#0d3555}.run:disabled{opacity:.55;cursor:wait}
    .security{position:sticky;top:20px;background:var(--navy);color:#fff}.security h3{font-size:23px;margin:0 0 14px}
    .security ul{padding-left:19px;color:#c8d5de}.security li{margin:10px 0}.security strong{color:#fff}
    .key-state{margin-top:18px;border-top:1px solid #385168;padding-top:18px}.key-state button{border:1px solid #70879a;background:transparent;color:#fff;border-radius:8px;padding:8px 12px;cursor:pointer}
    .notice{font-size:13px;color:#9eb2c0;margin-top:12px}.progress,.result{display:none;margin-top:30px}.progress.active,.result.active{display:block}
    .progress-card{background:var(--navy);color:#fff;border-radius:18px;padding:28px}.pulse{display:inline-block;width:10px;height:10px;background:var(--mint);border-radius:50%;
      margin-right:9px;box-shadow:0 0 0 0 #72c9b699;animation:pulse 1.6s infinite}@keyframes pulse{70%{box-shadow:0 0 0 10px #72c9b600}}
    .progress small{color:#a9bdca}.result-head{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:14px}
    .result-head h2{font-size:34px;margin:0}.meta{color:var(--muted);font-size:14px}.download{border:1px solid #a8a196;background:#fff;border-radius:9px;padding:9px 12px;cursor:pointer}
    .answer{background:#fff;border:1px solid var(--line);border-radius:18px;padding:30px;white-space:pre-wrap;overflow-wrap:anywhere}
    .answer a{color:#145e75}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
    .metric{background:#ece6da;border-radius:11px;padding:14px}.metric b{font-size:22px;display:block}.metric span{font-size:12px;color:var(--muted)}
    .sources{display:grid;grid-template-columns:1fr 1fr;gap:10px}.source{background:#fff;border:1px solid var(--line);border-radius:11px;padding:14px;text-decoration:none}
    .source:hover{border-color:#52788b}.tag{font-size:11px;color:#4d6c7a;font-weight:900}.source b{display:block;margin:3px 0}.source small{color:var(--muted)}
    .error{display:none;background:#fff0ed;border:1px solid #e0a39b;color:var(--danger);border-radius:11px;padding:13px;margin:12px 0}.error.active{display:block}
    footer{max-width:1180px;margin:0 auto 50px;padding:25px;border-top:1px solid var(--line);display:flex;justify-content:space-between;color:var(--muted);font-size:13px}
    @media(max-width:850px){.hero-inner,.workspace{grid-template-columns:1fr}.promise{display:none}.steps{grid-template-columns:1fr}.step{border-right:0;border-bottom:1px solid var(--line)}
      .security{position:static}.credential-grid,.sources{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}.navlinks{display:none}
      footer{flex-direction:column;gap:8px}}
  </style>
</head>
<body>
  <header class="hero">
    <nav><a class="brand" href="/">Korean Bill &amp; Debate <small>v0.9 alpha</small></a>
      <div class="navlinks"><a href="/">MCP 연결</a><a href="https://github.com/epoko77-ai/korean-bill-debate-mcp">GitHub</a></div></nav>
    <div class="hero-inner"><div><div class="eyebrow">국회 입법조사 워크스페이스</div>
      <h1>법안 하나를 물으면,<br>심사 기록이 한 흐름으로.</h1>
      <p>법안 내용과 현재 상태부터 소위원회 회의록, 전문위원 검토보고서, 의원과 정부의 실제 질의·답변까지 공식 원문으로 연결합니다.</p></div>
      <div class="promise"><b>검색 결과가 아니라<br>확인 가능한 조사 결과</b><span>AI 요약 뒤에 반드시 열린국회·국회 회의록·의안정보시스템 원문을 제공합니다.</span></div></div>
  </header>
  <main>
    <section class="steps" aria-label="사용 순서"><div class="step"><div class="number">1</div><div><b>내 키로 연결</b><span>열린국회와 LLM API 키 입력</span></div></div>
      <div class="step"><div class="number">2</div><div><b>한 줄로 조사</b><span>법안 번호·정책 쟁점 자연어 질문</span></div></div>
      <div class="step"><div class="number">3</div><div><b>원문으로 확인</b><span>상태·회의록·검토보고서·발언 검증</span></div></div></section>
    <div class="workspace">
      <section class="panel"><h2>새 조사 시작</h2><p class="sub">회원가입과 설치 없이, 본인의 API 키로 한 번만 실행합니다.</p>
        <form id="research-form" autocomplete="off">
          <div class="credential-grid">
            <div class="field"><label for="assembly-key">열린국회 API 키 <a target="_blank" rel="noreferrer" href="https://open.assembly.go.kr/portal/openapi/openApiNaListPage.do">키 발급 ↗</a></label>
              <input id="assembly-key" type="password" required maxlength="256" autocomplete="new-password" placeholder="본인 인증키 입력"><div class="hint">의안·처리상태·회의 메타데이터 조회에 사용</div></div>
            <div class="field"><label for="provider">답변을 작성할 AI</label><select id="provider"><option value="openai">OpenAI</option><option value="anthropic">Anthropic (Claude)</option></select>
              <div class="hint">LLM 사용료는 선택한 제공자 계정에 부과됩니다.</div></div>
          </div>
          <div class="field"><label for="llm-key">LLM API 키 <a id="llm-key-link" target="_blank" rel="noreferrer" href="https://platform.openai.com/api-keys">키 발급 ↗</a></label>
            <input id="llm-key" type="password" required maxlength="2048" autocomplete="new-password" placeholder="OpenAI API 키 입력"><div class="hint">조사된 공식 자료를 읽고 답변을 작성할 때만 사용</div></div>
          <div class="field"><label for="question">무엇을 확인할까요?</label>
            <textarea id="question" required maxlength="500" placeholder="예: 2219564번 의안의 내용과 최신 처리상태, 소위 쟁점, 전문위원 검토와 의원·정부 발언을 원문과 함께 정리해줘"></textarea></div>
          <div class="examples"><button class="example" type="button">2219564번 의안 전체 흐름</button><button class="example" type="button">플랫폼 노동 규제 법안 쟁점</button><button class="example" type="button">인공지능 법안 소위 논의와 정부 답변</button></div>
          <label class="consent"><input id="consent" type="checkbox" required><span>두 키가 요청 처리를 위해 서버로 전송되고 저장되지 않으며, 질문과 조사된 공식 자료는 답변 생성을 위해 선택한 LLM 제공자로 전송된다는 점을 확인했습니다.</span></label>
          <div id="error" class="error" role="alert"></div><button id="run" class="run" type="submit">공식 기록 조사 시작</button>
        </form>
      </section>
      <aside class="panel security"><h3>키는 어떻게 처리되나요?</h3><ul>
        <li><strong>주소에 넣지 않습니다.</strong><br>두 키 모두 HTTPS 요청 본문으로만 전송됩니다.</li>
        <li><strong>저장하지 않습니다.</strong><br>DB·파일·쿠키·localStorage에 기록하지 않습니다.</li>
        <li><strong>용도를 분리합니다.</strong><br>열린국회 키는 공식 자료 조회에, LLM 키는 답변 생성에만 사용합니다.</li>
        <li><strong>탭을 닫으면 사라집니다.</strong><br>입력값은 현재 화면의 메모리에만 남습니다.</li></ul>
        <div class="key-state"><button id="clear-keys" type="button">화면에서 키 지우기</button><div class="notice">공용 PC에서는 사용 후 반드시 키를 지우고 창을 닫으세요.</div></div>
      </aside>
    </div>
    <section id="progress" class="progress" aria-live="polite"><div class="progress-card"><b><span class="pulse"></span><span id="progress-title">공식 의안과 처리상태를 확인하고 있습니다.</span></b><br><small id="progress-detail">첫 조사는 관련 PDF 확인 때문에 시간이 걸릴 수 있습니다. 이 창을 닫지 마세요.</small></div></section>
    <section id="result" class="result" aria-live="polite"><div class="result-head"><div><h2>조사 결과</h2><div id="result-meta" class="meta"></div></div><button id="download" class="download" type="button">결과 JSON 저장</button></div>
      <div id="metrics" class="metrics"></div><div id="answer" class="answer"></div><h3>직접 확인할 공식 원문</h3><div id="sources" class="sources"></div></section>
  </main>
  <footer><span>Korean Bill &amp; Debate MCP · Apache-2.0</span><span>AI 답변은 반드시 연결된 공식 원문과 함께 확인하세요.</span></footer>
  <script src="/workspace/app.js" defer></script>
</body></html>"""


_SCRIPT = r"""(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const form = $('research-form');
  const provider = $('provider');
  const llmKey = $('llm-key');
  const assemblyKey = $('assembly-key');
  const question = $('question');
  const run = $('run');
  const progress = $('progress');
  const result = $('result');
  const error = $('error');
  let lastResult = null;
  let progressTimer = null;

  const providerConfig = {
    openai: ['OpenAI API 키 입력', 'https://platform.openai.com/api-keys'],
    anthropic: ['Anthropic API 키 입력', 'https://console.anthropic.com/settings/keys']
  };
  provider.addEventListener('change', () => {
    const config = providerConfig[provider.value];
    llmKey.placeholder = config[0];
    $('llm-key-link').href = config[1];
  });
  document.querySelectorAll('.example').forEach((button) => {
    button.addEventListener('click', () => { question.value = button.textContent; question.focus(); });
  });
  $('clear-keys').addEventListener('click', () => {
    assemblyKey.value = ''; llmKey.value = ''; $('consent').checked = false; assemblyKey.focus();
  });

  const progressMessages = [
    ['공식 의안과 처리상태를 확인하고 있습니다.', '열린국회 공식 API에서 최신 상태를 조회합니다.'],
    ['전문위원 검토보고서를 찾고 있습니다.', '법체계·집행 가능성·수정 필요사항을 확인합니다.'],
    ['관련 소위원회 회의록을 읽고 있습니다.', '의원의 질문과 정부 답변, 앞뒤 맥락을 연결합니다.'],
    ['공식 근거를 바탕으로 답변을 작성합니다.', '확인 가능한 원문 링크를 주장 가까이에 배치합니다.']
  ];
  function startProgress() {
    let index = 0; progress.classList.add('active'); result.classList.remove('active');
    const update = () => { const item = progressMessages[Math.min(index, progressMessages.length - 1)]; $('progress-title').textContent = item[0]; $('progress-detail').textContent = item[1]; index += 1; };
    update(); progressTimer = window.setInterval(update, 9000); progress.scrollIntoView({behavior:'smooth', block:'center'});
  }
  function stopProgress() { if (progressTimer) window.clearInterval(progressTimer); progressTimer = null; progress.classList.remove('active'); }
  function showError(message) { error.textContent = message; error.classList.add('active'); }
  function clearError() { error.textContent = ''; error.classList.remove('active'); }
  function appendLinkedText(container, text) {
    const urlPattern = /(https:\/\/[^\s<>()\]]+)/g; let last = 0; let match;
    while ((match = urlPattern.exec(text)) !== null) {
      container.appendChild(document.createTextNode(text.slice(last, match.index)));
      const link = document.createElement('a'); link.href = match[0]; link.textContent = match[0]; link.target = '_blank'; link.rel = 'noreferrer'; container.appendChild(link); last = match.index + match[0].length;
    }
    container.appendChild(document.createTextNode(text.slice(last)));
  }
  function renderMetrics(evidence) {
    const values = [['관련 의안', evidence.bill_count || 0], ['관련 발언', evidence.speech_count || 0], ['질의·답변 흐름', evidence.thread_count || 0], ['원문 링크', (evidence.sources || []).length]];
    $('metrics').replaceChildren(...values.map(([label, value]) => { const box = document.createElement('div'); box.className='metric'; const b=document.createElement('b'); b.textContent=String(value); const span=document.createElement('span'); span.textContent=label; box.append(b,span); return box; }));
  }
  function renderSources(sources) {
    const cards = (sources || []).map((source) => { const a=document.createElement('a'); a.className='source'; a.href=source.url; a.target='_blank'; a.rel='noreferrer'; const tag=document.createElement('div'); tag.className='tag'; tag.textContent=source.type; const title=document.createElement('b'); title.textContent=source.title || '국회 공식 원문'; const detail=document.createElement('small'); detail.textContent=source.detail || source.url; a.append(tag,title,detail); return a; });
    if (!cards.length) { const empty=document.createElement('p'); empty.textContent='이번 조사에서 연결된 공식 원문이 없습니다. 질문 범위를 더 구체화해 다시 시도해 주세요.'; cards.push(empty); }
    $('sources').replaceChildren(...cards);
  }
  function renderResponse(data) {
    lastResult = data; $('answer').replaceChildren(); appendLinkedText($('answer'), data.answer || '답변이 없습니다.');
    renderMetrics(data.evidence || {}); renderSources((data.evidence || {}).sources || []);
    $('result-meta').textContent = `${data.provider} · ${data.model} · ${data.elapsed_seconds}초`;
    result.classList.add('active'); result.scrollIntoView({behavior:'smooth', block:'start'});
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); clearError(); run.disabled=true; run.textContent='조사 중…'; startProgress();
    try {
      const response = await fetch('/workspace/research', {method:'POST', cache:'no-store', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:question.value, assembly_api_key:assemblyKey.value, llm_provider:provider.value, llm_api_key:llmKey.value})});
      let data; try { data = await response.json(); } catch (_) { throw new Error('서버 응답을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.'); }
      if (!response.ok) throw new Error(data.error || '조사 요청에 실패했습니다.'); renderResponse(data);
    } catch (requestError) { showError(requestError.message || '조사 요청에 실패했습니다.'); error.scrollIntoView({behavior:'smooth',block:'center'}); }
    finally { stopProgress(); run.disabled=false; run.textContent='공식 기록 조사 시작'; }
  });
  $('download').addEventListener('click', () => {
    if (!lastResult) return; const blob=new Blob([JSON.stringify(lastResult,null,2)],{type:'application/json'}); const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=`korean-bill-research-${new Date().toISOString().slice(0,10)}.json`; link.click(); URL.revokeObjectURL(link.href);
  });
})();"""
