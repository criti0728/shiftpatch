#!/usr/bin/env python3
"""Live Gemini evaluation; always isolated synthetic snapshots, never user's cafe.

No fake transport or calculation fallback. Reports observations rather than
claiming success when a model did not revise or budgets prevented completion.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
from dev import load_env
from app.agent.runner import generate_proposal
from app.agent.model_client import GeminiTransport
from app.seed import seed_state
from app.operations import close_day


def scenarios():
    baseline = seed_state()
    absent = deepcopy(baseline)
    absent['staff'][2]['absent'] = True
    scarce = deepcopy(baseline)
    bread = next(i for i in scarce['inventory'] if i['id'] == 'bread')
    bread['on_hand'] = bread['reserved'] + 2
    risk = deepcopy(baseline)
    next(i for i in risk['inventory'] if i['id'] == 'bread')['on_hand'] = 0
    closed = deepcopy(baseline)
    close_day(closed)
    return [('normal', baseline, 'in_service'), ('staff_absence', absent, 'in_service'),
            ('bread_shortage', scarce, 'in_service'), ('existing_order_risk', risk, 'in_service'),
            ('pre_open', deepcopy(baseline), 'pre_open'), ('post_close', closed, 'post_close')]


def observe(result, before, state, mode):
    trace = result.get('trace', [])
    model_trace = [t for t in trace if t['origin'] == 'model' and not t.get('cached')]
    reads = [(t['tool'], t['args']) for t in model_trace if t['tool'].startswith('read_')]
    revisions = [c for c in result['candidates'] if c.get('parent_candidate_id')]
    revision_proofs = []
    for child in revisions:
        parent = next((c for c in result['candidates'] if c['id'] == child['parent_candidate_id']), None)
        refs = [result.get('results', {}).get(r, {}) for r in child.get('based_on_result_ids', [])]
        if parent and parent.get('origin') == 'model' and child['limits'] != parent['limits'] and any(r.get('data', {}).get('id') == parent['id'] for r in refs):
            revision_proofs.append({'parent': parent['id'], 'child': child['id'], 'observed_results': child['based_on_result_ids']})
    valid = result['status'] == 'ready' and result['source'] == 'gemini' and result.get('model_calls', 0) > 0
    return {'validated_result': valid, 'snapshot_unchanged': before == state,
            'mode': mode, 'read_path': reads, 'revision_proofs': revision_proofs,
            'model_calls': result.get('model_calls'), 'tool_calls': result.get('tool_calls'),
            'elapsed_ms': result.get('elapsed_ms'), 'token_usage': result.get('token_usage', {}),
            'error': result.get('error'), 'outcome': result.get('outcome'),
            'existing_at_risk': [c['metrics']['existing_at_risk'] for c in result['candidates']]}


async def main(args):
    load_env()
    # Verify must never quietly exercise the offline path.
    os.environ['SHIFTPATCH_AI_MODE'] = 'gemini'
    try:
        check = GeminiTransport()
        await check.aclose()
    except Exception as exc:
        print('Gemini 설정 또는 인증 초기화 실패: ' + type(exc).__name__ + '. .env의 모델·인증·베이스 URL을 확인하세요.', file=sys.stderr)
        return 2
    reports = []
    out = ROOT / args.output
    out.mkdir(parents=True, exist_ok=True)
    for round_id in range(args.rounds):
        for name, state, mode in scenarios():
            if args.scenario != 'all' and name != args.scenario:
                continue
            state['snapshot_id'] = f'live-{name}-{round_id}'
            before = deepcopy(state)
            print(f'Gemini 실제 실행: {name} ({round_id+1}/{args.rounds})', flush=True)
            result = await generate_proposal(state, mode, 'preserve_drinks')
            report = {'scenario': name, 'round': round_id+1, **observe(result, before, state, mode)}
            report['context_relevant_reads'] = (any(t[0] == 'read_staff_menu_times' for t in report['read_path']) if name == 'staff_absence' else any(t[0] in {'read_inventory', 'read_menu_recipes'} for t in report['read_path']) if name == 'bread_shortage' else True)
            report['existing_risk_preserved'] = (bool(report['existing_at_risk']) and all(risk > 0 for risk in report['existing_at_risk']) and result.get('recommended_candidate_id') is None) if name == 'existing_order_risk' else True
            reports.append(report)
            # No SDK Content, thought signatures, prompts or credentials are saved.
            (out / f'{name}-{round_id+1}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"  {result['status']}; model={result.get('model_calls', 0)}; {result.get('elapsed_ms', 0)}ms", flush=True)
    service = [r for r in reports if r['scenario'] in {'normal', 'staff_absence', 'bread_shortage'} and r['validated_result'] and r['read_path']]
    paths = {json.dumps(r['read_path'], sort_keys=True) for r in service}
    adapted = len(paths) >= 2
    revised = any(r['revision_proofs'] for r in service)
    all_valid = bool(reports) and all(r['validated_result'] and r['snapshot_unchanged'] and r['context_relevant_reads'] and r['existing_risk_preserved'] for r in reports)
    full_suite = args.scenario == 'all'
    passed = all_valid and (adapted and revised if full_suite else True)
    summary = {'type': 'live_gemini', 'created_at': datetime.now(timezone.utc).isoformat(),
               'model_id': os.getenv('SHIFTPATCH_GEMINI_MODEL'), 'rounds': args.rounds,
               'scope': args.scenario, 'passed': passed, 'all_results_validated': all_valid,
               'different_investigation_paths_observed': adapted,
               'observed_result_based_revision': revised,
               'agent_behavior_verified': full_suite and passed,
               'disclaimer': '합성 매장의 실제 Gemini 호출 검증. 실매장 수요 정확도·매출 효과·Cloud Run 배포 성공 증거는 아닙니다.',
               'reports': reports}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"검증 {'통과' if passed else '미통과'}: {out / 'summary.json'}", flush=True)
    if not full_suite:
        print('단일 시나리오는 접속·계약 검사이며 에이전트성 검증은 전체 시나리오가 필요합니다.')
    return 0 if passed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', choices=['all', 'normal', 'staff_absence', 'bread_shortage', 'existing_order_risk', 'pre_open', 'post_close'], default='all')
    parser.add_argument('--rounds', type=int, choices=range(1, 6), default=1)
    parser.add_argument('--output', default='test-results/gemini-live')
    sys.exit(asyncio.run(main(parser.parse_args())))
