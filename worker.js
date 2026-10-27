// Cloudflare Workers 판 server.py — 같은 엔드포인트·같은 응답 형식(프론트 수정 없음).
// 키는 wrangler secret(배포) 또는 .env(로컬 `npx wrangler dev`)로 주입한다.
import { Buffer } from "node:buffer";
import INDEX_HTML from "./index.html";

const SYSTEM_PROMPT = "당신은 명확하고 통찰력 있는 답변을 제공하는 전문적인 AI 어시스턴트입니다.";
const TEMPERATURE = 0.7;
const NO_KEY = "서버에 Gemini API 키가 없습니다(GEMINI_API_KEYS 확인).";

// 회전 커서 [키idx, 모델idx] — 작성/추출은 모델 순서가 달라 따로 기억한다.
// ponytail: 워커 인스턴스마다 따로 기억(재시작 시 0부터). 소진된 조합을 잠깐 다시 시도할 뿐 동작엔 문제없음.
const cursors = { write: [0, 0], extract: [0, 0] };

const list = (s) => [...new Set((s || "").split(",").map((x) => x.trim()).filter(Boolean))];

function config(env) {
  const keys = list(env.GEMINI_API_KEYS);
  const single = (env.GEMINI_API_KEY || "").trim();
  if (single && !keys.includes(single)) keys.push(single);
  const models = list(env.GEMINI_MODELS);
  if (!models.length) models.push("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash");
  return {
    keys,
    models, // 생기부 작성: 정순(품질 높은 순)
    modelsExtract: [...models].reverse(), // 주제 추출: 역순(가벼운 모델 먼저)
    accessCode: (env.ACCESS_CODE || "").trim(),
    dsKey: (env.NVIDIA_NIM_API_KEY || "").trim(),
    dsBase: (env.DEEPSEEK_BASE_URL || "https://integrate.api.nvidia.com/v1").trim(),
    dsModel: (env.DEEPSEEK_MODEL || "deepseek-ai/deepseek-v4-pro").trim(),
  };
}

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

function shouldRotate(status, text) {
  // 이 (키, 모델) 조합을 더 못 쓰니 다음 조합으로: 소진(429/403 quota)·모델 없음(404)·과부하(503)
  if (status === 429 || status === 404 || status === 503) return true;
  const t = text.toLowerCase();
  return status === 403 && (t.includes("quota") || t.includes("exhaust"));
}

function advance(cur, nModels, nKeys) {
  // 모델 먼저, 한 키의 모델을 다 쓰면 다음 키로
  if (++cur[1] >= nModels) { cur[1] = 0; cur[0] = (cur[0] + 1) % nKeys; }
}

function geminiBody(prompt, system, temperature, jsonMode = false, fileParts = []) {
  const body = {
    system_instruction: { parts: [{ text: system }] },
    contents: [{ role: "user", parts: [{ text: prompt }, ...fileParts] }],
    generationConfig: { temperature },
  };
  if (jsonMode) body.generationConfig.responseMimeType = "application/json";
  return JSON.stringify(body);
}

// SSE 응답에서 data: 줄의 JSON만 꺼낸다.
async function* sseJson(resp) {
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value;
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      const p = line.startsWith("data:") ? line.slice(5).trim() : "";
      if (!p || p === "[DONE]") continue;
      try { yield JSON.parse(p); } catch {}
    }
  }
}

async function geminiGenerate(c, prompt, system, temperature, jsonMode, fileParts) {
  // 주제 추출·문제 생성·공문 분석용 단발 호출. 가벼운 모델부터, 소진 시 다음 조합.
  const models = c.modelsExtract, cur = cursors.extract;
  const body = geminiBody(prompt, system, temperature, jsonMode, fileParts);
  const nk = c.keys.length, nm = models.length;
  for (let i = 0; i < nk * nm; i++) {
    const [ki, mi] = cur;
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${models[mi]}:generateContent`;
    const r = await fetch(url, { method: "POST", headers: { "x-goog-api-key": c.keys[ki], "content-type": "application/json" }, body });
    const text = await r.text();
    if (r.ok) {
      const cands = JSON.parse(text).candidates || [];
      if (!cands.length) throw new HttpError(502, "Gemini 응답에 결과가 없습니다(안전 필터 등으로 차단되었을 수 있음).");
      return cands[0].content.parts[0].text;
    }
    if (shouldRotate(r.status, text)) {
      console.log(`[gemini/추출] 키 #${ki + 1}/${nk} · 모델 '${models[mi]}' 사용 불가(HTTP ${r.status}) → 다음 조합으로 전환`);
      advance(cur, nm, nk);
      continue;
    }
    throw new HttpError(502, `Gemini API 오류 ${r.status}: ${text.slice(0, 300)}`);
  }
  // 전부 소진 → DeepSeek 예비. 단 DeepSeek는 텍스트 전용이라 첨부가 있으면 못 넘긴다.
  if (c.dsKey && !fileParts.length) {
    console.log(`[gemini/추출] 모든 키(${nk}개)·모델(${nm}개) 소진 → DeepSeek '${c.dsModel}'로 전환`);
    return deepseekGenerate(c, prompt, system, temperature, jsonMode);
  }
  throw new HttpError(429, fileParts.length
    ? `모든 Gemini 키(${nk}개)·모델(${nm}개)의 사용량이 소진되었습니다. 첨부 파일이 있는 요청은 DeepSeek 예비 모델로 넘길 수 없습니다(텍스트 전용). 잠시 후 다시 시도하거나 키를 추가하세요.`
    : `모든 Gemini 키(${nk}개)·모델(${nm}개)의 사용량이 소진되었습니다. 잠시 후 다시 시도하거나 키를 추가하세요.`);
}

async function* geminiStream(c, prompt, system, temperature) {
  // 생기부 작성용 스트리밍. 품질 높은 모델부터, 스트림 시작 전 응답 코드로 소진 판별.
  const models = c.models, cur = cursors.write;
  const body = geminiBody(prompt, system, temperature);
  const nk = c.keys.length, nm = models.length;
  for (let i = 0; i < nk * nm; i++) {
    const [ki, mi] = cur;
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${models[mi]}:streamGenerateContent?alt=sse`;
    const r = await fetch(url, { method: "POST", headers: { "x-goog-api-key": c.keys[ki], "content-type": "application/json" }, body });
    if (!r.ok) {
      const text = await r.text();
      if (shouldRotate(r.status, text)) {
        console.log(`[gemini/작성] 키 #${ki + 1}/${nk} · 모델 '${models[mi]}' 사용 불가(HTTP ${r.status}) → 다음 조합으로 전환`);
        advance(cur, nm, nk);
        continue;
      }
      yield { error: `Gemini API 오류 ${r.status}: ${text.slice(0, 300)}` };
      return;
    }
    for await (const obj of sseJson(r))
      for (const cand of obj.candidates || [])
        for (const part of cand.content?.parts || [])
          if (part.text) yield { content: part.text };
    return;
  }
  if (c.dsKey) {
    console.log(`[gemini/작성] 모든 키(${nk}개)·모델(${nm}개) 소진 → DeepSeek '${c.dsModel}'로 전환`);
    yield* deepseekStream(c, prompt, system, temperature);
    return;
  }
  yield { error: `모든 Gemini 키(${nk}개)·모델(${nm}개)의 사용량이 소진되었습니다. (NVIDIA_NIM_API_KEY를 넣으면 DeepSeek로 자동 전환됩니다.)` };
}

function deepseekFetch(c, payload) {
  return fetch(`${c.dsBase}/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${c.dsKey}`, "content-type": "application/json" },
    body: JSON.stringify({ model: c.dsModel, ...payload }),
  });
}

const messages = (system, prompt) => [{ role: "system", content: system }, { role: "user", content: prompt }];

async function* deepseekStream(c, prompt, system, temperature) {
  try {
    const r = await deepseekFetch(c, { messages: messages(system, prompt), temperature, stream: true });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
    for await (const chunk of sseJson(r)) {
      const t = chunk.choices?.[0]?.delta?.content;
      if (t) yield { content: t };
    }
  } catch (e) {
    yield { error: `DeepSeek 호출 실패: ${e.message}` };
  }
}

async function deepseekGenerate(c, prompt, system, temperature, jsonMode) {
  const payload = { messages: messages(system, prompt), temperature };
  if (jsonMode) payload.response_format = { type: "json_object" };
  let content;
  try {
    const r = await deepseekFetch(c, payload);
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
    content = (await r.json()).choices?.[0]?.message?.content;
  } catch (e) {
    throw new HttpError(502, `DeepSeek 호출 실패: ${e.message}`);
  }
  if (!content) throw new HttpError(502, "DeepSeek 응답이 비어 있습니다.");
  return content;
}

function formNum(v, dflt) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : dflt;
}

async function handleGenerate(req, c) {
  const fd = await req.formData();
  const fileParts = [];
  for (const f of fd.getAll("files")) {
    if (typeof f === "string") continue;
    const type = f.type || "";
    if (!(type.startsWith("image/") || type === "application/pdf"))
      throw new HttpError(400, `지원하지 않는 파일 형식: ${type}`);
    // ponytail: 무료 플랜은 요청당 CPU 10ms — 수 MB 넘는 PDF는 인코딩에서 막힐 수 있음. 그러면 유료($5/월) 플랜.
    fileParts.push({ inline_data: { mime_type: type, data: Buffer.from(await f.arrayBuffer()).toString("base64") } });
  }
  let content;
  try {
    content = await geminiGenerate(
      c, fd.get("prompt") || "", fd.get("system") ?? SYSTEM_PROMPT,
      formNum(fd.get("temperature"), TEMPERATURE), fd.get("response_format") === "json_object", fileParts,
    );
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw new HttpError(502, `Gemini 호출 실패: ${e.message}`);
  }
  return json({ content });
}

async function handleChatStream(req, c) {
  const fd = await req.formData();
  const gen = geminiStream(c, fd.get("message") || "", fd.get("system") ?? SYSTEM_PROMPT, formNum(fd.get("temperature"), TEMPERATURE));
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    async pull(ctrl) {
      let next;
      try {
        next = await gen.next();
      } catch (e) {
        next = { value: { error: `Gemini 호출 실패: ${e.message}` } };
        gen.return();
      }
      if (next.done) {
        ctrl.enqueue(enc.encode("data: [DONE]\n\n"));
        ctrl.close();
      } else {
        ctrl.enqueue(enc.encode(`data: ${JSON.stringify(next.value)}\n\n`));
      }
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

export default {
  async fetch(req, env) {
    const { pathname } = new URL(req.url);
    const c = config(env);
    try {
      if (req.method === "GET" && pathname === "/")
        return new Response(INDEX_HTML, { headers: { "content-type": "text/html; charset=utf-8" } });

      const handler = { "/generate": handleGenerate, "/chat/stream": handleChatStream }[pathname];
      if (req.method !== "POST" || !handler) return json({ detail: "Not Found" }, 404);

      // 접속 암호 — ACCESS_CODE가 설정돼 있으면 X-Access-Code 헤더가 같아야 한다.
      if (c.accessCode && (req.headers.get("x-access-code") || "").trim() !== c.accessCode)
        throw new HttpError(401, "접속 암호가 올바르지 않습니다.");
      if (!c.keys.length) throw new HttpError(400, NO_KEY);
      return await handler(req, c);
    } catch (e) {
      if (e instanceof HttpError) return json({ detail: e.message }, e.status);
      throw e;
    }
  },
};
