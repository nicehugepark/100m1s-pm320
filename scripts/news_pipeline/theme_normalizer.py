"""
테마 정규화 — canonical name + aliases 매핑.
대표 지시 2026-04-10: 동일 테마는 동일 표현. 들쭉날쭉 금지.

REQ-056 (2026-04-28): theme_dictionary.json SSOT 자동 로드 (이중 출처 해소).
- 기존 하드코딩 THEME_ALIASES (98 lines, line 13~110)는 fallback으로만 잔존
- 모듈 로드 시 1회 _load_theme_aliases() 호출 → JSON canonical_themes 119건 자동 부착
- JSON 부재/파싱 실패 시 _FALLBACK_THEME_ALIASES로 graceful degrade
- 토구사 v2.1.0 갱신 시 두 파일 수동 동기 작업 폐기 (FLR-AGT-002 패턴 차단)
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .db import connect

_DICT_PATH = Path(__file__).parent / "theme_dictionary.json"


def _load_theme_aliases() -> dict[str, list[str]]:
    """REQ-056 — theme_dictionary.json에서 canonical name + aliases 자동 로드.

    반환: {canonical_name: [aliases...]} dict.
    JSON 부재/파싱 실패 시 _FALLBACK_THEME_ALIASES (하드코딩) 반환.
    """
    try:
        with open(_DICT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        result: dict[str, list[str]] = {}
        for theme in data.get("canonical_themes", []) or []:
            name = theme.get("name")
            if not name:
                continue
            result[name] = list(theme.get("aliases") or [])
        # JSON 정합성 가드: 최소 50건 이상이어야 정상 (canonical 119건 기대)
        if len(result) < 50:
            return _FALLBACK_THEME_ALIASES
        return result
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # graceful degrade — 하드코딩 fallback 유지 (production 안전성)
        return _FALLBACK_THEME_ALIASES


def _load_theme_meta() -> dict[str, dict]:
    """REQ-057 + REQ-076 Phase 2-A — theme_dictionary canonical 메타 로드.

    반환: {canonical_name: {
              "parent": str|None,        # legacy 단일 (호환)
              "parents": list[str],      # REQ-076 v2.2.0 다중 부모 (SSOT)
              "is_active": bool,
          }} dict.

    REQ-076 Phase 2-A: dictionary v2.2.0부터 entry는 `parents` 배열을 신규 SSOT로 사용.
    - `parents` 배열 키 존재 → 그대로 채택
    - 부재 시 (구버전 dictionary) → `parent` 단일 string을 [parent] 또는 []로 정규화 (fallback)
    - `parent` legacy 키는 deprecated이지만 후방 호환 위해 잔존 노출 (parents[0] 또는 None)

    ensure_theme() 신규 INSERT 시 parents 리스트 전체를 theme_parents 테이블에 부착.
    JSON 부재 시 빈 dict — fallback 동작 (parent_id=None, parents=[], is_active=1).
    """
    try:
        with open(_DICT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        meta: dict[str, dict] = {}
        for theme in data.get("canonical_themes", []) or []:
            name = theme.get("name")
            if not name:
                continue
            # REQ-076 — parents 배열 우선
            parents_arr: list[str]
            if "parents" in theme:
                raw = theme.get("parents") or []
                parents_arr = [p for p in raw if isinstance(p, str) and p.strip()]
            else:
                # fallback — 구버전 dictionary 호환
                legacy = theme.get("parent")
                parents_arr = (
                    [legacy] if isinstance(legacy, str) and legacy.strip() else []
                )
            # legacy parent 노출 (parents[0] 또는 None)
            parent_legacy = parents_arr[0] if parents_arr else None
            meta[name] = {
                "parent": parent_legacy,
                "parents": parents_arr,
                "is_active": bool(theme.get("is_active", True)),
            }
        return meta
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# REQ-056 — fallback 하드코딩 dict (JSON 부재 시 graceful degrade).
# 본래 하드코딩 SSOT였으나 REQ-056부터 JSON SSOT로 전환.
# 토구사 v2.1.0+ canonical 갱신은 theme_dictionary.json만 수정 (본 dict는 동기 안 함).
_FALLBACK_THEME_ALIASES = {
    "2차전지": [
        "배터리",
        "이차전지",
        "리튬이온",
        "전고체배터리",
        "전고체 배터리",
        "전고체",
    ],
    "전쟁재건": [
        "중동재건",
        "중동 재건",
        "우크라재건",
        "우크라 재건",
        "재건",
        "전쟁 재건",
        "이란 종전",
    ],
    "건설": ["건설대형주", "건설업", "건물 건설", "토목 건설"],
    "AI데이터센터": [
        "AI인프라",
        "AI 데이터센터",
        "데이터센터",
        "AI 데이터센터 수혜",
        "AI데이터센터 수혜",
    ],
    "광통신": [
        "광통신 인프라",
        "광반도체",
        "광 반도체",
        "실리콘포토닉스",
        "광섬유",
        "광모듈",
    ],
    "반도체": [
        "반도체장비",
        "반도체후공정",
        "반도체기판",
        "반도체 소재",
        "반도체소재",
        "AI반도체",
        "AI 반도체",
        "반도체검사장비",
        "반도체 검사장비",
        "반도체검사",
        "반도체 검사",
        "후공정 장비",
        "반도체 후공정",
    ],
    "HBM": ["고대역폭메모리", "HBM4"],
    "K-뷰티": ["화장품", "K뷰티", "K-뷰티 해외 성장"],
    "방산": ["방위산업", "무기", "드론", "드론·플라잉카", "군수"],
    "태양광": ["신재생에너지", "태양광 장비", "태양전지"],
    "6G": ["차세대통신", "6G통신", "AI통신인프라", "6G·양자암호"],
    "양자": ["양자컴퓨터", "양자암호", "양자기술", "양자 기술"],
    "원전": ["원자력", "SMR", "원전수주"],
    "해운": ["해상운송", "벌크운송", "컨테이너"],
    "저PBR": ["자산가치", "부동산자산보유", "저평가 리레이팅"],
    "생체인식": ["바이오인식", "지문인식", "홍채"],
    "전력설비": ["전동기", "발전기", "전력기기"],
    "5G": ["5G부품", "RF", "안테나"],
    "테스트소켓": ["테스트장비", "반도체 테스트"],
    "FC-BGA": ["패키징", "반도체기판", "PKG기판"],
    "건설자재": ["시멘트", "콘크리트", "레미콘"],
    "통신장비": ["통신인프라", "이동통신중계기"],
    "케이블": ["절연선", "전선"],
    # 2026-04-10 트리 확장 추가
    "전기차": ["EV", "전기차·배터리", "전기자동차"],
    "2차전지 장비 수혜": ["배터리장비", "이차전지 장비", "2차전지 장비"],
    "전기차·하이브리드·PBV 성장축": ["전기차 성장", "하이브리드차"],
    "바이오": ["바이오텍", "제약바이오", "바이오주"],
    "GLP-1 비만치료제": ["비만치료제", "GLP-1", "위고비", "마운자로"],
    "비만약 테마주": ["비만약", "비만주"],
    "면역항암제": ["면역항암", "면역관문억제제"],
    "이중항체": ["이중특이항체", "bispecific"],
    "ADC 신약": ["항체약물접합체", "ADC"],
    "가상자산": ["크립토", "코인", "블록체인", "가상화폐"],
    "크립토 제도화 수혜": ["크립토 제도화", "가상자산 제도화"],
    "금융": ["금융업", "은행업"],
    "금융지주회사": ["금융지주", "은행지주"],
    "로봇": ["로보틱스", "로봇공학", "로봇 장비"],
    "휴머노이드 로봇": ["휴머노이드", "인간형 로봇", "옵티머스"],
    "물류자동화": ["물류로봇", "스마트물류"],
    "공중 기동 로봇": ["군용드론", "공중로봇"],
    "반도체 팹 건설 수주": ["팹 건설", "반도체 건설"],
    "부동산 재개발": ["재개발", "재건축", "정비사업"],
    # 2026-04-10 테마 트리 상위 노드 + 미매핑 테마 추가 (불일치 전수 조사)
    "AI": ["인공지능", "AI기술"],
    "AI인프라": ["AI 인프라", "AI 인프라 수혜"],
    "전쟁": ["전쟁 테마", "지정학"],
    "전쟁완화": ["전쟁 완화", "종전", "휴전"],
    "전쟁악화": ["전쟁 악화", "전쟁 격화"],
    "중동전쟁": ["중동 전쟁", "이란전쟁", "이란 전쟁"],
    "호르무즈 해협": ["호르무즈해협", "호르무즈", "호르무즈해협 리스크"],
    "은행": ["은행업", "시중은행", "은행주"],
    "비철금속": ["비철", "알루미늄", "구리", "비철금속 소재"],
    "비료": ["비료주", "화학비료", "요소비료", "비료 수혜"],
}


# REQ-056 — module-level SSOT (theme_dictionary.json 자동 로드).
# 모듈 import 시 1회 평가 (cron이 매 빌드 새 프로세스 → 자연 fresh).
# JSON 부재/파싱 실패 시 _FALLBACK_THEME_ALIASES로 graceful degrade.
THEME_ALIASES = _load_theme_aliases()

# REQ-057 — canonical parent/is_active 메타 (ensure_theme INSERT 시 부착).
# dictionary 정의를 DB row에 일치시켜 (A) 결함 (신규 canonical parent_id 미동기) 재발 차단.
THEME_META = _load_theme_meta()


def _build_reverse_map():
    """alias → canonical 역매핑."""
    rmap = {}
    for canonical, aliases in THEME_ALIASES.items():
        rmap[canonical.lower()] = canonical
        for a in aliases:
            rmap[a.lower()] = canonical
    return rmap


_REVERSE = _build_reverse_map()


def normalize(theme_name: str) -> str:
    """테마명을 canonical name으로 정규화. 매칭 안 되면 원본 반환."""
    return _REVERSE.get(theme_name.lower().strip(), theme_name.strip())


def normalize_list(themes: list) -> list:
    """테마 리스트를 정규화 + 중복 제거."""
    seen = set()
    result = []
    for t in themes:
        name = t if isinstance(t, str) else t.get("name", "")
        canonical = normalize(name)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def ensure_theme(
    name: str,
    category: str = None,
    aliases: list = None,
    parents: list = None,
):
    """themes 테이블에 없으면 생성, 있으면 ID 반환.

    REQ-057 — 신규 INSERT 시 dictionary `parents`/`is_active` 메타 자동 부착.
    REQ-076 Phase 2-A — 다중 부모(N:M) 지원.

    Args:
        name: 테마명 (canonical로 정규화됨)
        category: 카테고리 (선택)
        aliases: alias 리스트 (선택; 미지정 시 THEME_ALIASES에서 조회)
        parents: 부모 테마명 리스트 (선택; 미지정 시 dictionary `parents` 메타 사용).
            - 리스트의 각 부모에 대해 재귀 ensure_theme로 ID 확보 후
              `theme_parents(child_id, parent_id, weight=1.0, source='migrated')` INSERT.
            - UNIQUE(child_id, parent_id) 충돌 시 IGNORE (재호출 안전).
            - 단일 부모 호환: 첫 부모를 `themes.parent_id` 컬럼에도 부착 (Phase 3에서 제거).

    후방 호환: `parents=None` 미전달 시 기존 동작과 동일 — dictionary 메타 사용.
    """
    canonical = normalize(name)
    now = datetime.now().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM themes WHERE name=?", (canonical,)
        ).fetchone()
        if row:
            return row["id"]
        als = json.dumps(
            aliases or THEME_ALIASES.get(canonical, []), ensure_ascii=False
        )
        # REQ-057 — dictionary 메타 부착 (신규 INSERT만; 기존 row는 보존)
        meta = THEME_META.get(canonical, {})
        is_active = 1 if meta.get("is_active", True) else 0
        conn.execute(
            "INSERT INTO themes(name, category, aliases_json, is_active, created_at) "
            "VALUES(?,?,?,?,?)",
            (canonical, category, als, is_active, now),
        )
        conn.commit()
        new_id = conn.execute(
            "SELECT id FROM themes WHERE name=?", (canonical,)
        ).fetchone()["id"]

    # REQ-076 Phase 2-A — parents 결정: 명시 인자 우선 → dictionary 메타 fallback
    if parents is not None:
        parent_list = [p for p in parents if isinstance(p, str) and p.strip()]
    else:
        parent_list = list(meta.get("parents") or [])
        # 구버전 호환: parents 비어있고 legacy parent 단일 값만 있는 케이스
        if not parent_list and meta.get("parent"):
            parent_list = [meta["parent"]]

    if parent_list:
        # 첫 parent: themes.parent_id 컬럼에 부착 (Phase 1 잔존 호환, Phase 3에서 제거)
        first_parent_id = ensure_theme(parent_list[0])
        with connect() as conn:
            conn.execute(
                "UPDATE themes SET parent_id=? WHERE id=? AND parent_id IS NULL",
                (first_parent_id, new_id),
            )
            conn.commit()
        # 전체 parents: theme_parents 테이블에 다중 INSERT (UNIQUE 충돌 IGNORE)
        for pname in parent_list:
            pid = ensure_theme(pname)
            with connect() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO theme_parents
                       (child_id, parent_id, weight, source, created_at)
                       VALUES (?, ?, 1.0, 'migrated', ?)""",
                    (new_id, pid, now),
                )
                conn.commit()
    return new_id


def link_stock_theme(
    stock_code: str, theme_name: str, date: str, source: str = "ishikawa"
):
    """stock_themes 매핑 생성/갱신. 비활성 테마는 ValueError raise (호출처가 catch).

    [Q-20260511-FIX-B-1] 비활성 테마 silent return → raise 전환.
    silent return은 거짓 충실성(FLR-AGT-002 동형) — 호출처(_apply_industry_seeds)가
    attached.append를 link 성공 여부와 무관하게 실행해 로그는 attached 출력,
    DB는 0행 INSERT 발생. 호출처가 except ValueError로 catch + WARN 로깅 + 조건부
    append 하도록 의존.
    """
    theme_id = ensure_theme(theme_name)
    with connect() as conn:
        # 비활성 테마 매핑 방지 — silent skip 금지, raise로 호출처에 명시 전달
        active = conn.execute(
            "SELECT is_active FROM themes WHERE id=?", (theme_id,)
        ).fetchone()
        if active and not active["is_active"]:
            raise ValueError(
                f"theme_id={theme_id} (name={theme_name}) is inactive, link skipped"
            )
        # DOC-20260530-REQ-002 — deny override 가드 (대표 권한 SSOT).
        # 모든 부착 경로(industry_seed / fresh_listing / ishikawa V3 / togusa /
        # pipeline)가 본 함수를 거치므로 단일 chokepoint 가드로 전 경로 봉쇄
        # (FLR-20260428-TEC-001 — 분기마다 가드 복붙 금지). 테이블 부재 환경
        # (init_schema 미실행 DB)에서는 graceful skip.
        try:
            denied = conn.execute(
                "SELECT 1 FROM stock_theme_overrides "
                "WHERE stock_code=? AND deny_theme_id=? LIMIT 1",
                (stock_code, theme_id),
            ).fetchone()
        except sqlite3.OperationalError:
            denied = None  # 테이블 미존재 — 가드 비활성 (graceful degrade)
        if denied:
            raise ValueError(
                f"theme_id={theme_id} (name={theme_name}) denied by owner override "
                f"for stock={stock_code}, link skipped"
            )
        # Q-20260511-FIX-A-LLM-CACHE — source='manual'/'owner' 보존 가드.
        # 재해석 시 manual 매핑이 ishikawa/togusa로 덮어쓰여지면 fix-B 등에서
        # 부여한 정정 매핑이 silently 손실됨. ON CONFLICT 시 기존 source가 manual
        # 계열이면 source 컬럼은 보존, date_last만 갱신.
        conn.execute(
            """INSERT INTO stock_themes(stock_code, theme_id, date_added, date_last, source)
               VALUES(?,?,?,?,?)
               ON CONFLICT(stock_code, theme_id) DO UPDATE SET
                 date_last = excluded.date_last,
                 source = CASE
                   WHEN stock_themes.source LIKE 'manual%' THEN stock_themes.source
                   WHEN stock_themes.source = 'owner' THEN stock_themes.source
                   ELSE excluded.source
                 END""",
            (stock_code, theme_id, date, date, source),
        )
        conn.commit()


def apply_owner_overrides(stock_code: str, date: str) -> list[str]:
    """DOC-20260530-REQ-002 — 종목별 force override 적용 (대표 권한).

    stock_theme_overrides의 force_theme_id를 source='owner'로 부착한다.
    deny는 link_stock_theme 내부 가드가 처리하므로 여기서는 force만 다룬다.
    interpret() 1종목 처리 종료 시점에 호출 — 자동 부착 이후 마지막에 적용해
    재해석에도 owner 부여가 link_stock_theme의 source='owner' 보존 가드로 유지된다.

    Returns: force 부착된 theme_id 리스트 (없으면 []).
    부착 자체가 deny되면(동일 종목 deny+force 충돌 row) ValueError를 link_stock_theme이
    raise → catch + WARN. 거짓 충실성(FLR-AGT-002) 방지 — 실제 부착 0건이면 [] 반환.
    """
    with connect() as conn:
        try:
            rows = conn.execute(
                "SELECT DISTINCT force_theme_id FROM stock_theme_overrides "
                "WHERE stock_code=? AND force_theme_id IS NOT NULL",
                (stock_code,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # 테이블 미존재 — graceful degrade
        # theme_id → name (link_stock_theme은 이름 입력 — ensure_theme 재사용)
        force_pairs = []
        for r in rows:
            nm = conn.execute(
                "SELECT name FROM themes WHERE id=?", (r["force_theme_id"],)
            ).fetchone()
            if nm:
                force_pairs.append((r["force_theme_id"], nm["name"]))
    applied: list[str] = []
    for tid, tname in force_pairs:
        try:
            link_stock_theme(stock_code, tname, date, "owner")
            applied.append(tname)
        except ValueError as e:
            # 비활성 테마 또는 동일 종목 deny+force 충돌 — 명시 WARN, 부착 0건
            print(f"[{stock_code}] owner force SKIP theme={tname}({tid}) reason={e}")
        except Exception as e:  # noqa: BLE001
            print(f"[{stock_code}] owner force FAIL theme={tname}({tid}) err={e}")
    return applied


def migrate_from_themes_json():
    """기존 stocks.themes_json → themes + stock_themes 마이그레이션.

    NOTE: themes_json 컬럼이 DROP된 DB에서는 no-op.
    """
    now = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()}
        if "themes_json" not in cols:
            print("themes_json column already dropped — nothing to migrate")
            return 0
        stocks = conn.execute("SELECT code, themes_json FROM stocks").fetchall()
    count = 0
    for s in stocks:
        themes = json.loads(s["themes_json"] or "[]")
        for t in themes:
            name = t if isinstance(t, str) else t.get("name", t)
            if name:
                link_stock_theme(s["code"], name, now, "pipeline")
                count += 1
    print(f"migrated: {count} stock-theme links from {len(stocks)} stocks")
    return count


## ── 테마 트리 조작 API (경량, B-tree 스타일 O(1) 수정) ──


def set_parent(child_name: str, parent_name: str):
    """테마의 부모를 설정. parent_name이 None이면 루트로."""
    child_id = ensure_theme(child_name)
    parent_id = ensure_theme(parent_name) if parent_name else None
    with connect() as conn:
        conn.execute("UPDATE themes SET parent_id=? WHERE id=?", (parent_id, child_id))
        conn.commit()
    return child_id, parent_id


def move_subtree(theme_name: str, new_parent_name: str):
    """서브트리 전체를 새 부모 아래로 이동. 자식들은 그대로 따라감."""
    return set_parent(theme_name, new_parent_name)


def get_ancestors(theme_name: str) -> list:
    """루트까지의 상위 테마 체인. [직계부모, ..., 루트]"""
    canonical = normalize(theme_name)
    chain = []
    with connect() as conn:
        row = conn.execute(
            "SELECT id, parent_id FROM themes WHERE name=?", (canonical,)
        ).fetchone()
        while row and row["parent_id"]:
            parent = conn.execute(
                "SELECT id, name, parent_id FROM themes WHERE id=?", (row["parent_id"],)
            ).fetchone()
            if not parent:
                break
            chain.append(parent["name"])
            row = parent
    return chain


def get_children(theme_name: str) -> list:
    """직계 자식 테마 목록."""
    canonical = normalize(theme_name)
    with connect() as conn:
        tid = conn.execute(
            "SELECT id FROM themes WHERE name=?", (canonical,)
        ).fetchone()
        if not tid:
            return []
        rows = conn.execute(
            "SELECT name FROM themes WHERE parent_id=? ORDER BY name", (tid["id"],)
        ).fetchall()
        return [r["name"] for r in rows]


def get_subtree(theme_name: str) -> dict:
    """전체 서브트리를 딕셔너리로 반환. {name: {children: [...]}}"""
    canonical = normalize(theme_name)
    with connect() as conn:
        tid = conn.execute(
            "SELECT id FROM themes WHERE name=?", (canonical,)
        ).fetchone()
        if not tid:
            return {}

        def _build(parent_id):
            children = conn.execute(
                "SELECT id, name FROM themes WHERE parent_id=? ORDER BY name",
                (parent_id,),
            ).fetchall()
            return {c["name"]: _build(c["id"]) for c in children}

        return {canonical: _build(tid["id"])}


def print_tree(include_inactive: bool = False):
    """전체 테마 트리를 콘솔에 출력. include_inactive=True면 비활성도 표시."""
    active_filter = (
        "" if include_inactive else "AND (is_active = 1 OR is_active IS NULL)"
    )
    with connect() as conn:
        roots = conn.execute(
            f"""SELECT id, name FROM themes
               WHERE parent_id IS NULL
               AND id IN (SELECT DISTINCT parent_id FROM themes WHERE parent_id IS NOT NULL {active_filter})
               {active_filter}
               ORDER BY name"""
        ).fetchall()
        standalone = conn.execute(
            f"""SELECT name FROM themes
               WHERE parent_id IS NULL
               AND id NOT IN (SELECT DISTINCT parent_id FROM themes WHERE parent_id IS NOT NULL)
               {active_filter}
               ORDER BY name"""
        ).fetchall()

        def _pt(pid, depth=0):
            for c in conn.execute(
                f"SELECT id, name, is_active FROM themes WHERE parent_id=? {active_filter} ORDER BY name",
                (pid,),
            ).fetchall():
                marker = " [inactive]" if c["is_active"] == 0 else ""
                print(f"{'  ' * depth}└─ {c['name']}{marker}")
                _pt(c["id"], depth + 1)

        for r in roots:
            print(f"\n{r['name']}")
            _pt(r["id"], 1)
        if standalone:
            print(f"\n[독립 테마] {', '.join(s['name'] for s in standalone)}")
        # 비활성 카운트
        inactive_cnt = conn.execute(
            "SELECT COUNT(*) as c FROM themes WHERE is_active = 0"
        ).fetchone()["c"]
        if inactive_cnt:
            print(f"\n[비활성 테마: {inactive_cnt}개]")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "tree":
        print_tree()
    elif len(sys.argv) > 1 and sys.argv[1] == "set-parent" and len(sys.argv) == 4:
        child, parent = sys.argv[2], sys.argv[3]
        set_parent(child, parent if parent != "null" else None)
        print(f"{child} → parent: {parent}")
    elif len(sys.argv) > 1 and sys.argv[1] == "migrate":
        migrate_from_themes_json()
    else:
        print("Usage:")
        print("  python -m scripts.news_pipeline.theme_normalizer tree")
        print(
            "  python -m scripts.news_pipeline.theme_normalizer set-parent <child> <parent|null>"
        )
        print("  python -m scripts.news_pipeline.theme_normalizer migrate")
