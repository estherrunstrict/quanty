#!/usr/bin/env python3
"""Read-only test monitoring. Never imports trading code or contacts a broker.

Only allowlisted public summary fields leave this process. Order IDs, raw errors,
account identifiers, balances and credentials are never copied to the payload.
"""
import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def parse_time(value):
    try:
        text = str(value).replace(' KST', '+09:00').replace('Z', '+00:00')
        if len(text) <= 10:
            return None  # a date is not a verified run timestamp
        stamp = datetime.fromisoformat(text)
        return stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp
    except (ValueError, TypeError):
        return None


def age_hours(value, now):
    stamp = parse_time(value)
    if stamp is None or stamp > now:
        return None
    return round((now - stamp).total_seconds() / 3600, 2)


def freshness(value, now, limit=96):
    age = age_hours(value, now)
    return 'unknown' if age is None else ('delayed' if age > limit else 'observed')


def public_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def make_item(s, now):
    last = s.get('last_run') or s.get('timestamp')
    status = freshness(last, now)
    if s.get('is_stale') is True:
        status = 'delayed'
    return {'id': str(s['id']), 'name': str(s.get('name') or s['id']),
            'stage': 'real_test' if s.get('mode') == 'testing' else 'paper',
            'money_mode': 'real' if s.get('mode') == 'testing' else 'simulated',
            'status': status, 'last_observed_at': last, 'source': 'dashboard snapshot',
            'source_at': None, 'holdings_count': len(s['holdings']) if isinstance(s.get('holdings'), list) else None,
            'cycles': public_number(s.get('cycles_ytd')), 'unresolved_intents': None,
            'result': '실행 기록 수신 · 거래 성공 여부는 별도 확인' if status == 'observed' else '최근 실행 기록 확인 필요',
            'next_action': '체결·대사와 테스트 결과 검토', 'criteria': '승격 기준 미연결',
            'events': []}


def build(home, dash, now=None):
    now = now or datetime.now(timezone.utc)
    home, dash = Path(home), Path(dash)
    dashboard = read_json(dash / 'docs/data/dashboard_data.json')
    decisions = read_json(dash / 'docs/data/decisions.json')
    issues, items = [], []
    sources = []
    for name, data, key in [('Dashboard', dashboard, 'updated_at'), ('후보 레지스트리', decisions, 'generated_at')]:
        at = data.get(key) if data else None
        state = freshness(at, now, 96)
        sources.append({'name': name, 'status': state, 'at': at})
        if state != 'observed':
            issues.append(f'{name} 원본이 없거나 오래되었습니다. 최신 결과를 확인하세요.')
    strategies = (dashboard or {}).get('strategies') or []
    for s in strategies:
        if not isinstance(s, dict) or not s.get('id') or s.get('mode') not in ('paper', 'testing'):
            continue
        item = make_item(s, now)
        item['source_at'] = dashboard.get('updated_at')
        if s['id'] == 'usvb':
            ledger = read_json(home / 'toss-usvb-bot/toss_usvb_bot/state/ledger.json')
            item['source'] = 'USVB ledger · 읽기 전용'
            if ledger and ledger.get('mode') == 'testing':
                item['last_observed_at'] = ledger.get('updated')
                item['source_at'] = ledger.get('updated')
                item['status'] = freshness(ledger.get('updated'), now)
                item['holdings_count'] = len(ledger['positions']) if isinstance(ledger.get('positions'), dict) else None
                intents = ledger.get('order_intents')
                # A missing intent registry cannot prove zero outstanding orders.
                item['unresolved_intents'] = sum(v.get('state') in ('submitting', 'unknown', 'acknowledged', 'partial') for v in intents.values() if isinstance(v, dict)) if isinstance(intents, dict) else None
                item['result'] = '원장 갱신 수신 · 체결·청산 완료 판정 아님'
                item['next_action'] = '브로커 미체결·잔여 보유와 원장 대사 확인'
            else:
                item['status'] = 'unknown'
                item['result'] = '실자금 테스트 원장을 확인할 수 없음'
                item['next_action'] = 'USVB 원장 접근 및 실행 모드 확인'
        elif s['id'] == 'korea_etf':
            result = read_json(home / 'koreainvestment-autotrade/strategy_results/korea_etf_momentum_result.json')
            item['source'] = 'Korea ETF session result · 읽기 전용'
            if result and (result.get('session_summary') or {}).get('paper_trading') is True:
                item['last_observed_at'] = result.get('timestamp')
                item['source_at'] = result.get('timestamp')
                item['status'] = freshness(result.get('timestamp'), now)
                item['holdings_count'] = len(result['holdings']) if isinstance(result.get('holdings'), (list, dict)) else None
                item['result'] = '페이퍼 세션 기록 · ' + ('리밸런싱 대상일' if (result.get('session_summary') or {}).get('rebalance_day') else '리밸런싱 비대상일')
            else:
                item['status'] = 'unknown'
                item['result'] = '페이퍼 세션 근거 미확인'
        elif sources[0]['status'] != 'observed':
            item['status'] = 'unknown'
        if item['status'] == 'delayed':
            item['next_action'] = '최근 실행 시각과 스케줄 확인 · 자동 승격 판단 보류'
        if item['unresolved_intents']:
            item['status'] = 'attention'
            item['next_action'] = '미확정 주문 기록을 브로커 주문 상태와 대조'
        if item['last_observed_at']:
            item['events'].append({'at': item['last_observed_at'], 'label': item['result']})
        items.append(item)
    for c in (decisions or {}).get('candidate_pipeline') or []:
        if not isinstance(c, dict) or not c.get('slug'):
            continue
        status = 'blocked' if c.get('health') == 'paper-stalled' else 'unverified'
        item = {'id': 'candidate:' + str(c['slug']), 'name': str(c.get('title') or c['slug']),
                'stage': 'paper' if c.get('status') == 'paper-trading' else 'research',
                'money_mode': 'simulated' if c.get('status') == 'paper-trading' else 'none',
                'status': status, 'last_observed_at': None, 'source_at': decisions.get('generated_at'),
                'source': '후보 레지스트리 · 실행 텔레메트리 미연결',
                'holdings_count': None, 'cycles': None, 'unresolved_intents': None,
                'result': '레지스트리에서 페이퍼 정체로 보고됨' if status == 'blocked' else '후보 등록만 확인 · 실행 결과 미확인',
                'next_action': '실행 배포·스케줄 및 결과 파일 확인' if status == 'blocked' else '백테스트 결과와 검증 근거 연결',
                'criteria': str(c.get('promotion_criteria') or '승격 기준 미등록')[:800],
                'events': []}
        if sources[1]['status'] != 'observed':
            item['status'] = 'unknown'
            item['result'] = '후보 레지스트리 원본이 오래되었거나 시각 미확인'
        items.append(item)
    sources.append({'name': 'Paper Factory 실행 기록', 'status': 'unconnected', 'at': None})
    pending = (decisions or {}).get('open_decisions')
    return {'schema_version': 1, 'generated_at': now.isoformat(), 'refresh_seconds': 60,
            'collection_interval_minutes': 10, 'stale_after_hours': 96,
            'freshness_note': '실행 기록은 96시간 경과 시 지연 표시합니다. 거래소 캘린더 기반 장애 판정은 아직 연결되지 않았습니다.',
            'sources': sources, 'issues': issues, 'items': items,
            'pending_decisions': len(pending) if isinstance(pending, list) else None,
            'decision_source_at': (decisions or {}).get('generated_at')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', default='/home/ubuntu')
    parser.add_argument('--dashboard-dir', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--output')
    args = parser.parse_args()
    payload = build(args.home, args.dashboard_dir)
    target = Path(args.output) if args.output else Path(args.dashboard_dir) / 'docs/data/pipeline_data.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    os.replace(temp, target)
    print(f'Pipeline: {len(payload["items"])} monitored records; wrote {target}')


if __name__ == '__main__':
    main()
