#!/usr/bin/env node
// build-page-manifest.js
// 🔴 P0-2 (FLR-20260605-TEC-001) — 공유/카드 링크 존재 보장 manifest 생성.
//
// 목적:
//   라이브에 실제 배포된 종목 OG landing 페이지 목록을 단일 JSON 으로 박제.
//   `pm320/{date}/{code}.html` 실파일을 스캔 → `data/page-manifest.json` 생성 (Q-119 stock 제거).
//   manifest = "실제 배포 상태" SSOT (DB/카드 카운트 아님, 디스크 실파일 기준).
//   renderer.js 공유 URL 생성부가 이 manifest 로 대상 페이지 존재를 검증 → 404 URL 봉쇄.
//
// 동기화 시점:
//   kiwoom_cron push add-set (종목페이지 `news/` 배포) 직전/직후 실행 → manifest 가
//   배포와 동기됨 (P0-1 완결성 게이트와 정합). 스캔 대상 = 배포될/배포된 그 디렉토리.
//   배포 worktree 에서 실행하면 그 worktree 의 실파일이 곧 origin 으로 push 됨 → 정합.
//
// 보수성:
//   페이지가 없으면 manifest 에서도 빠짐 → renderer 가 폴백(404 안 만듦).
//   manifest 자체가 없거나 parse 실패면 renderer 는 기존 PRE_MARKET 휴리스틱으로 degrade.
//
// Usage:
//   node scripts/build-page-manifest.js              # repo 루트의 pm320 스캔
//   M1S_HOMEPAGE=/path node scripts/build-page-manifest.js   # 대상 repo override (cron worktree)
//
// DSN: records/2026-04/DOC-20260430-DSN-001-arch-frontend.md §3.2 (공유 URL)

'use strict';

const fs = require('fs');
const path = require('path');

// 스캔 대상 repo 루트 — M1S_HOMEPAGE env 우선(cron worktree 격리 정합), 없으면 본 스크립트 기준 repo 루트.
const REPO_ROOT = process.env.M1S_HOMEPAGE || path.resolve(__dirname, '..');
// Q-20260606-119 — 종목페이지 경로 /pm320/stock/{date}/{code} → /pm320/{date}/{code} (stock 세그먼트 제거).
//   스캔 대상 = pm320/ 하위 {date}/ 디렉토리. 같은 폴더의 {date}.html 날짜 스텁 파일은 DATE_RE 디렉토리
//   매칭에서 자연 제외(파일이지 디렉토리 아님). og/sw/calendar/renderer 경로와 정합.
const STOCK_HTML_BASE = path.join(REPO_ROOT, 'pm320');
const OUT_PATH = path.join(REPO_ROOT, 'data', 'page-manifest.json');

// {date}/{code}.html 패턴 검증 — date = YYYY-MM-DD, code = 6자리 숫자(우선주 5+1자리 포함 6자리).
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const CODE_RE = /^\d{5,6}[0-9A-Z]?\.html$/i; // 6자리 숫자 또는 우선주 코드(예 066575). 안전하게 폭 허용.
// Q-20260608-404fix — 날짜 랜딩 파일 `pm320/{date}.html` 검증(FLR-20260605-TEC-001 "링크 존재 보장" 동형).
//   종목 디렉토리(pages)와 별개로, calendar.js `_dateHasStaticPage` 가 실배포 랜딩만 true 판정하도록
//   landings 키에 라이브 디스크의 날짜 랜딩 실파일을 박제. 데이터 존재(calHasData)≠랜딩 배포 봉쇄.
const LANDING_RE = /^(\d{4}-\d{2}-\d{2})\.html$/;

function buildManifest() {
  const pages = {};
  const landings = []; // Q-20260608-404fix — 라이브 배포된 날짜 랜딩 `pm320/{date}.html` (ISO date 배열).
  let total = 0;

  let dateDirs;
  try {
    dateDirs = fs.readdirSync(STOCK_HTML_BASE, { withFileTypes: true });
  } catch (e) {
    // pm320 자체가 없으면 빈 manifest (보수적). 종목페이지 0건 배포 상태.
    console.error(`[page-manifest] pm320 디렉토리 없음(${STOCK_HTML_BASE}) — 빈 manifest 생성`);
    dateDirs = [];
  }

  for (const d of dateDirs) {
    // 날짜 랜딩 파일 `pm320/{date}.html` — 디렉토리 아닌 파일. 실배포된 랜딩만 landings 에 박제.
    if (d.isFile()) {
      const lm = LANDING_RE.exec(d.name);
      if (lm) landings.push(lm[1]);
      continue;
    }
    if (!d.isDirectory() || !DATE_RE.test(d.name)) continue;
    const dateDir = path.join(STOCK_HTML_BASE, d.name);
    let files;
    try {
      files = fs.readdirSync(dateDir);
    } catch (e) {
      continue;
    }
    const codes = [];
    for (const f of files) {
      if (!f.endsWith('.html')) continue;
      if (!CODE_RE.test(f)) continue;
      codes.push(f.replace(/\.html$/i, ''));
    }
    if (codes.length) {
      codes.sort();
      pages[d.name] = codes;
      total += codes.length;
    }
  }

  landings.sort();
  return {
    schema: 'page-manifest/v1',
    generated_at: new Date().toISOString(),
    total_pages: total,
    total_landings: landings.length,
    pages,
    landings, // 실배포 날짜 랜딩(`pm320/{date}.html`) — calendar._dateHasStaticPage SSOT.
  };
}

function main() {
  const manifest = buildManifest();
  // data 디렉토리 보장
  fs.mkdirSync(path.dirname(OUT_PATH), { recursive: true });
  fs.writeFileSync(OUT_PATH, JSON.stringify(manifest, null, 0) + '\n', 'utf-8');
  const dates = Object.keys(manifest.pages).length;
  console.log(`[page-manifest] ${OUT_PATH} 생성 — ${dates}일 ${manifest.total_pages}페이지 + 랜딩 ${manifest.total_landings}일`);
}

if (require.main === module) {
  main();
}

module.exports = { buildManifest };
