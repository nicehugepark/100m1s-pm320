# PM320 레포 격리 — 실 flip(S5b) 롤백 절차 (실행팀용)

- 근거: DOC-20260707-REQ-001 §2 S5. 본 문서 = 실 flip 시 **즉시 롤백** 원블록.
- 전제: S5-준비 단계에서 파이프라인 자립화·launchd 초안·dry-cycle 검증 완료(라이브·launchd 무변경).
- flr_reference: FLR-20260608-TEC-001(경로 divergence), FLR-20260519-TEC-001(cron race), FLR-AGT-002(라이브 검증 의무).

---

## 실 flip(S5b) 시 새로 세우는 것 (참고)
1. pm320 레포 배포 경로(예: `~/company/100m1s-pm320`)에 S2/S3 자립 코드+데이터 배치.
2. `launchd/generate_drafts.sh` 로 초안 생성(PM320_REPO=실배포경로) → `launchctl load` 로 신 plist 적재.
3. 구 launchd(메인/cron WT 기준) 언로드 + 미러 배선(subdomain-sync·pick-guard·1520) 폐지.

---

## 🔴 롤백 절차 (문제 발생 시 즉시 실행 — 원상복구)

구 launchd plist 원본은 `~/Library/LaunchAgents/com.100m1s.*.plist` 에 그대로 남아 있음(flip 시 삭제 금지 — 백업).

```bash
# 0) 신 pm320 기준 launchd 즉시 언로드 (신 배선 정지)
for L in pm320-push pm320-minute-backfill kiwoom-scraper news-pipeline themes-pipeline \
         macro-indicators kr-index-intraday market-anomaly-sensor nxt-roster \
         wire-collector us-digest us-intraday news-recovery kakao-token-rotate cafe-scraper; do
  launchctl unload ~/Library/LaunchAgents/com.100m1s.$L.plist 2>/dev/null || true
done

# 1) 구 launchd plist 원본 재적재 (메인/cron WT 기준 — flip 전 상태로 복원)
#    ⚠️ 구 plist 를 flip 시 백업해 둔 경로에서 복원. 미백업 시 git(메인 레포 scripts/*/*.plist)에서 복구.
for L in pm320-push pm320-minute-backfill kiwoom-scraper news-pipeline themes-pipeline \
         macro-indicators kr-index-intraday market-anomaly-sensor nxt-roster \
         wire-collector us-digest us-intraday news-recovery kakao-token-rotate cafe-scraper; do
  launchctl load ~/Library/LaunchAgents/com.100m1s.$L.plist 2>/dev/null || true
done

# 2) 미러 배선 복원 (서빙 이중화 원상 — flip 시 폐지했던 것)
for L in pm320-subdomain-sync pm320-subdomain-sync-1520 pm320-subdomain-pick-guard; do
  launchctl load ~/Library/LaunchAgents/com.100m1s.$L.plist 2>/dev/null || true
done

# 3) 서빙 target 원복 (S3 서빙 강등을 했었다면)
#    - 100m1s.com/pm320.html redirect stub → 원본 pm320.html 복원 (git revert)
#    - pm320.100m1s.com CNAME/Pages source 무변경(원래 정본이므로 건드리지 않음)

# 4) 검증 (롤백 후 라이브 정상)
curl -sI https://pm320.100m1s.com/ | head -1          # 200
curl -sI https://100m1s.com/pm320.html | head -1      # 200 (stub 복원 확인)
```

## 롤백 판정 기준(언제 롤백하나)
- 첫 15:20 cycle 에서 pm320.100m1s.com 픽 미노출 (15:21 데드라인 초과) → **즉시 롤백**.
- stock-{date}.json 종목수/PICK 이 직전 영업일 대비 논리 붕괴(0종목·크래시) → 즉시 롤백.
- launchd 잡 크래시 루프(`launchctl list | grep 100m1s` 의 상태 코드 비0 반복) → 해당 잡 언로드 후 롤백.

## 무변경 보증(본 S5-준비 단계)
- 원본 메인 레포·cron WT·라이브 서빙·launchd 로드본 **전부 무변경**. pm320 격리 WT 코드/초안/문서만 생성.
- dry-cycle 은 임시 scratchpad 경로에서만 실행(정본 DB·라이브 write 0건).
