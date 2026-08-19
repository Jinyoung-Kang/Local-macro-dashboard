"""
services/consensus_service.py
기관 13F Money 교집합 (Consensus) 분석 퀀트 엔진
다수 기관의 분기별 포트폴리오를 대조하여 공통 보유, 동시 순매수/순매도 집중 종목 산출
"""
import pandas as pd
from services.sec_service import fetch_sec_13f_multi_quarters, classify_qoq_action

def fetch_all_selected_histories(selected_institutions: dict, max_quarters: int = 8):
    """
    선택된 기관들의 13F 과거 분기 데이터를 수집하여 딕셔너리로 반환합니다.
    (SEC EDGAR 타임아웃 및 차단 방어 로직 완벽 연동)
    """
    inst_histories = {}
    for inst_name, inst_info in selected_institutions.items():
        history_results, err = fetch_sec_13f_multi_quarters(inst_info['cik'], max_quarters=max_quarters)
        if not err and history_results:
            inst_histories[inst_name] = history_results
    return inst_histories


def get_common_available_dates(inst_histories: dict):
    """
    수집된 기관 데이터에서 존재하는 모든 공시 기준일(report_date) 목록을 최신순으로 추출합니다.
    """
    all_dates = set()
    for history in inst_histories.values():
        for _, q_meta in history:
            all_dates.add(q_meta['report_date'])
    return sorted(list(all_dates), reverse=True)


def calculate_consensus_by_date(inst_histories: dict, target_report_date: str):
    """
    특정 기준일(target_report_date)에 대한 각 기관의 포트폴리오를 대조하여 교집합, 동시 매수 및 동시 매도 내역을 연산합니다.
    """
    active_dfs = []
    participating_insts = []

    for inst_name, history in inst_histories.items():
        target_idx = None
        for idx, (_, q_meta) in enumerate(history):
            if q_meta['report_date'] == target_report_date:
                target_idx = idx
                break

        if target_idx is not None:
            curr_df, curr_meta = history[target_idx]
            curr_df = curr_df.copy()
            curr_df['institution'] = inst_name
            curr_df['report_date'] = curr_meta['report_date']
            participating_insts.append(inst_name)

            # 직전 분기 대비 액션 연산
            if target_idx + 1 < len(history):
                prev_df, _ = history[target_idx + 1]
                merged = pd.merge(
                    curr_df[['name', 'shares', 'weight', 'value']],
                    prev_df[['name', 'shares', 'weight', 'value']],
                    on='name',
                    how='outer',
                    suffixes=('_curr', '_prev')
                ).fillna(0)
                merged['shares_diff'] = merged['shares_curr'] - merged['shares_prev']
                merged['weight_diff'] = merged['weight_curr'] - merged['weight_prev']
                merged['action'] = merged.apply(classify_qoq_action, axis=1)

                curr_df = pd.merge(curr_df, merged[['name', 'action', 'weight_diff']], on='name', how='left')
            else:
                curr_df['action'] = "⚪ 비교 데이터 없음"
                curr_df['weight_diff'] = 0.0

            active_dfs.append(curr_df)

    if not active_dfs:
        return None

    all_records = pd.concat(active_dfs, ignore_index=True)

    # 종목별 교집합 집계
    summary_df = all_records.groupby('name').agg(
        holder_count=('institution', 'nunique'),
        holders=('institution', lambda x: list(x)),
        total_value=('value', 'sum'),
        avg_weight=('weight', 'mean'),
        max_weight=('weight', 'max'),
        actions=('action', lambda x: list(x))
    ).reset_index()

    summary_df['holders_str'] = summary_df['holders'].apply(
        lambda h_list: ", ".join([h.split()[1] if len(h.split()) > 1 else h for h in h_list])
    )

    # 동시 매수(신규 매수 또는 비중 확대) 기관 수 집계
    summary_df['buy_action_count'] = summary_df['actions'].apply(
        lambda acts: sum(1 for a in acts if "신규 매수" in str(a) or "비중 확대" in str(a))
    )

    # 동시 매도(전량 매도 또는 비중 축소) 기관 수 집계
    summary_df['sell_action_count'] = summary_df['actions'].apply(
        lambda acts: sum(1 for a in acts if "전량 매도" in str(a) or "비중 축소" in str(a))
    )

    return {
        "summary": summary_df.sort_values(by=['holder_count', 'total_value'], ascending=[False, False]).reset_index(drop=True),
        "participating_count": len(participating_insts),
        "participating_insts": participating_insts,
        "raw_records": all_records
    }
