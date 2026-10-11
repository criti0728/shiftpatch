from .schemas import FinalAnswer


def validate_final(raw, dispatcher):
    answer = FinalAnswer.model_validate(raw)
    for result_id in answer.evidence_refs:
        dispatcher.require_result(result_id)
    known = {candidate["id"]: candidate for candidate in dispatcher.candidates}
    for note in answer.candidate_notes:
        if note.candidate_id not in known:
            raise ValueError("説明が未計算の候補を参照しています。")
        for result_id in note.evidence_refs:
            dispatcher.require_result(result_id)
        own_result_ids = {key for key, result in dispatcher.results.items()
                          if result["data"].get("id") == note.candidate_id}
        if not own_result_ids.intersection(note.evidence_refs):
            raise ValueError("候補の説明は、その候補自身の計算結果を参照してください。")
    if answer.outcome != "ready":
        if answer.recommended_candidate_id:
            raise ValueError("確認待ち・実行不能の結果に推奨候補は設定できません。")
        if answer.outcome == "needs_input" and not answer.questions:
            raise ValueError("不足している入力を質問に明示してください。")
        if answer.outcome == "no_feasible_plan" and any(c["feasible"] for c in known.values()):
            raise ValueError("実行可能な計算候補があるため、全案実行不能とは確定できません。")
        return answer
    if not answer.evidence_refs:
        raise ValueError("結果の根拠IDが必要です。")
    if dispatcher.mode == "in_service":
        candidate = known.get(answer.recommended_candidate_id)
        if candidate is None or not candidate["feasible"]:
            raise ValueError("推奨候補が未計算、または実行不能です。")
        if len(known) < 3:
            raise ValueError("現在設定に加えて、異なる代案を2つ以上計算してください。")
        if not any(note.candidate_id == candidate["id"] for note in answer.candidate_notes):
            raise ValueError("推奨候補の説明・根拠・トレードオフが必要です。")
        read_results = [r for r in dispatcher.results.values()
                        if r["tool"].startswith("read_") and "error" not in r["data"]]
        if not read_results:
            raise ValueError("状況を調べる読み取りツールによる根拠が必要です。")
        recommended_result_ids = {key for key, result in dispatcher.results.items()
                                  if result["data"].get("id") == candidate["id"]}
        if not recommended_result_ids.intersection(answer.evidence_refs):
            raise ValueError("推奨候補の計算結果を根拠として参照してください。")
        if answer.plan_result_id or answer.sales_basis_result_id or answer.comparison_result_id:
            raise ValueError("営業中の結果に準備計画の参照は指定できません。")
    else:
        if answer.recommended_candidate_id or answer.candidate_notes:
            raise ValueError("準備案を販売モード候補として返すことはできません。")
        tool = "calculate_prep" if dispatcher.mode == "pre_open" else "calculate_next_day_prep"
        plan = dispatcher.require_result(answer.plan_result_id, tool)
        dispatcher.require_result(answer.sales_basis_result_id, "estimate_sales")
        if plan["sales_basis_result_id"] != answer.sales_basis_result_id:
            raise ValueError("準備案と販売基準が同じ計算に紐づいていません。")
        if dispatcher.mode == "post_close":
            dispatcher.require_result(answer.comparison_result_id, "compare_plan_actual")
            if plan["comparison_result_id"] != answer.comparison_result_id:
                raise ValueError("翌日準備案と締め比較結果が一致しません。")
    return answer
