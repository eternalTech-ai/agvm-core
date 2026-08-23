# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from storage import utc_timestamp


def _shared_blackboard(result: dict[str, Any]) -> dict[str, Any]:
    shared = dict(result.get("shared_evidence") or {})
    blackboard = dict(shared.get("blackboard") or {})
    return blackboard or shared


def _entry(
    *,
    phase: str,
    guide_refs: list[str],
    status: str,
    intended_behavior: str,
    implemented_behavior: str,
    live_evidence: dict[str, Any],
    open_gaps: list[str],
) -> dict[str, Any]:
    return {
        "phase": phase,
        "guide_refs": guide_refs,
        "status": status,
        "intended_behavior": intended_behavior,
        "implemented_behavior": implemented_behavior,
        "live_evidence": live_evidence,
        "open_gaps": open_gaps,
    }


def build_guide_compliance_matrix(*, runtime_audit: dict[str, Any], recent_sessions: list[dict[str, Any]]) -> dict[str, Any]:
    latest_benchmark = dict(runtime_audit.get("last_benchmark") or {})
    latest_benchmark_phase = str(latest_benchmark.get("phase") or "")
    latest_stream_benchmark = dict(runtime_audit.get("latest_stream_benchmark") or {})
    stream_benchmark = latest_stream_benchmark or latest_benchmark
    stream_benchmark_report = dict(stream_benchmark.get("report") or {})
    stream_benchmark_phase = str(stream_benchmark.get("phase") or "")
    latest_maintenance_benchmark = dict(runtime_audit.get("latest_maintenance_benchmark") or {})
    maintenance_benchmark_report = dict(latest_maintenance_benchmark.get("report") or {})
    latest_planner_merge_benchmark = dict(runtime_audit.get("latest_planner_merge_benchmark") or {})
    planner_merge_benchmark_report = dict(latest_planner_merge_benchmark.get("report") or {})
    latest_route_richness_benchmark = dict(runtime_audit.get("latest_route_richness_benchmark") or {})
    route_richness_benchmark_report = dict(latest_route_richness_benchmark.get("report") or {})
    latest_master_closure_benchmark = dict(runtime_audit.get("latest_master_closure_benchmark") or {})
    master_closure_benchmark_report = dict(latest_master_closure_benchmark.get("report") or {})
    latest_evaluation_benchmark = dict(runtime_audit.get("latest_evaluation_benchmark") or {})
    evaluation_benchmark_report = dict(latest_evaluation_benchmark.get("report") or {})
    benchmark_report = dict(latest_benchmark.get("report") or {})
    stream_report = (
        dict((stream_benchmark_report.get("suites") or {}).get("stream") or {})
        if stream_benchmark_phase == "all"
        else dict(stream_benchmark_report or {}) if stream_benchmark_phase == "stream" else {}
    )
    stream_ui_replay_readiness = dict(stream_report.get("ui_replay_readiness") or {})
    slice1_summary = (
        dict(benchmark_report.get("slice1_revalidation_summary") or {})
        if latest_benchmark_phase == "slice1_revalidation"
        else {}
    )
    evaluation_matrix = (
        dict(evaluation_benchmark_report.get("final_evaluation_matrix") or {})
        or dict(runtime_audit.get("final_evaluation_matrix") or {})
        or dict((((benchmark_report.get("suites") or {}).get("evaluation") or {}).get("final_evaluation_matrix") or {}))
    )
    planner_histogram = dict(runtime_audit.get("planner_mode_histogram") or {})
    llm_scout_enabled_ratio = float(runtime_audit.get("llm_scout_enabled_ratio") or 0.0)
    hybrid_merge_ratio = float(runtime_audit.get("hybrid_merge_ratio") or 0.0)
    planner_influence_ratio = float(runtime_audit.get("planner_influence_ratio") or 0.0)
    planner_family_dual_active_ratio = float(runtime_audit.get("planner_family_dual_active_ratio") or 0.0)
    planner_family_win_ratio = float(runtime_audit.get("planner_family_win_ratio") or 0.0)
    planner_family_overlap_ratio = float(runtime_audit.get("planner_family_overlap_ratio") or 0.0)
    planner_family_divergence_ratio = float(runtime_audit.get("planner_family_divergence_ratio") or 0.0)
    planner_family_attribution_ratio = float(runtime_audit.get("planner_family_attribution_ratio") or 0.0)
    planner_seed_ms_payload = runtime_audit.get("planner_seed_ms") or 0.0
    planner_seed_ms = float((planner_seed_ms_payload or {}).get("p50") if isinstance(planner_seed_ms_payload, dict) else planner_seed_ms_payload or 0.0)
    planner_seed_success_ratio = float(runtime_audit.get("planner_seed_success_ratio") or 0.0)
    ai_material_contribution_ratio = float(runtime_audit.get("ai_material_contribution_ratio") or 0.0)
    ai_contribution_reason_histogram = dict(runtime_audit.get("ai_contribution_reason_histogram") or {})
    answer_strand_count = float(runtime_audit.get("answer_strand_count") or 0.0)
    seed_goal_coverage_ratio = float(runtime_audit.get("seed_goal_coverage_ratio") or 0.0)
    seed_destination_presence_ratio = float(runtime_audit.get("seed_destination_presence_ratio") or 0.0)
    seed_used_by_bootstrap_ratio = float(runtime_audit.get("seed_used_by_bootstrap_ratio") or 0.0)
    branch_reuse_ratio = float(runtime_audit.get("branch_reuse_ratio") or 0.0)
    branch_enrich_ratio = float(runtime_audit.get("branch_enrich_ratio") or 0.0)
    branch_fork_ratio = float(runtime_audit.get("branch_fork_ratio") or 0.0)
    dual_origin_branch_ratio = float(runtime_audit.get("dual_origin_branch_ratio") or 0.0)
    merge_resolution_histogram = dict(runtime_audit.get("merge_resolution_histogram") or {})
    heuristic_calibration_scope_count = int(runtime_audit.get("heuristic_calibration_scope_count") or 0)
    heuristic_calibration_event_count = int(runtime_audit.get("heuristic_calibration_event_count") or 0)
    heuristic_calibration_gain = float(runtime_audit.get("heuristic_calibration_gain") or 0.0)
    calibrated_bootstrap_success_ratio = float(runtime_audit.get("calibrated_bootstrap_success_ratio") or 0.0)
    calibrated_branch_count_delta = float(runtime_audit.get("calibrated_branch_count_delta") or 0.0)
    calibrated_highway_use_delta = float(runtime_audit.get("calibrated_highway_use_delta") or 0.0)
    branch_controller_usage_ratio = float(runtime_audit.get("branch_controller_usage_ratio") or 0.0)
    branch_controller_override_ratio = float(runtime_audit.get("branch_controller_override_ratio") or 0.0)
    master_llm_success_ratio = float(runtime_audit.get("master_llm_success_ratio") or 0.0)
    master_fallback_timeout_ratio = float(runtime_audit.get("master_fallback_timeout_ratio") or 0.0)
    answer_now_before_exploration_complete_ratio = float(runtime_audit.get("answer_now_before_exploration_complete_ratio") or 0.0)
    final_closure_after_destination_resolution_ratio = float(runtime_audit.get("final_closure_after_destination_resolution_ratio") or 0.0)
    context_level_1_before_final_ratio = float(runtime_audit.get("context_level_1_before_final_ratio") or 0.0)
    master_surface_state_histogram = dict(runtime_audit.get("master_surface_state_histogram") or {})
    master_fallback_reason_histogram = dict(runtime_audit.get("master_fallback_reason_histogram") or {})
    closure_blocker_reason_histogram = dict(runtime_audit.get("closure_blocker_reason_histogram") or {})
    warm_hit_ratio = float(runtime_audit.get("warm_hit_ratio") or 0.0)
    warm_partial_reuse_ratio = float(runtime_audit.get("warm_partial_reuse_ratio") or 0.0)
    divergence_reset_ratio = float(runtime_audit.get("divergence_reset_ratio") or 0.0)
    answer_now_before_final_ratio = float(runtime_audit.get("answer_now_before_final_ratio") or 0.0)
    background_expansion_after_partial_ratio = float(runtime_audit.get("background_expansion_after_partial_ratio") or 0.0)
    raw_text_coverage_ratio = float(runtime_audit.get("raw_text_coverage_ratio") or 0.0)
    document_chunk_coverage_ratio = float(runtime_audit.get("document_chunk_coverage_ratio") or 0.0)
    support_density = float(runtime_audit.get("support_density") or 0.0)
    contradiction_exposure_ratio = float(runtime_audit.get("contradiction_exposure_ratio") or 0.0)
    highway_route_yield = float(runtime_audit.get("highway_route_yield") or 0.0)
    route_richness_score = float(runtime_audit.get("route_richness_score") or 0.0)
    highway_effective_use_ratio = float(runtime_audit.get("highway_effective_use_ratio") or 0.0)
    link_effective_use_ratio = float(runtime_audit.get("link_effective_use_ratio") or 0.0)
    heuristic_family_route_step_ratio = float(runtime_audit.get("heuristic_family_route_step_ratio") or 0.0)
    ai_family_route_step_ratio = float(runtime_audit.get("ai_family_route_step_ratio") or 0.0)
    dual_origin_family_route_step_ratio = float(runtime_audit.get("dual_origin_family_route_step_ratio") or 0.0)
    destination_reached_ratio = float(runtime_audit.get("destination_reached_ratio") or 0.0)
    execution_reorder_count = int(runtime_audit.get("execution_reorder_count") or 0)
    execution_reorder_reasons = dict(runtime_audit.get("execution_reorder_reasons") or {})
    branch_duplication_ratio = float(runtime_audit.get("branch_duplication_ratio") or 0.0)
    branch_merge_ratio = float(runtime_audit.get("branch_merge_ratio") or 0.0)
    warm_context_reuse_quality = float(runtime_audit.get("warm_context_reuse_quality") or 0.0)
    mode_timing_percentiles = dict(runtime_audit.get("mode_timing_percentiles") or {})
    maintenance_run_count = int(runtime_audit.get("maintenance_run_count") or 0)
    applied_maintenance_run_count = int(runtime_audit.get("applied_maintenance_run_count") or 0)
    maintenance_modes_histogram = dict(runtime_audit.get("maintenance_modes_histogram") or {})
    maintenance_improvement_ratio = float(runtime_audit.get("maintenance_improvement_ratio") or 0.0)
    maintenance_geometry_improvement_ratio = float(runtime_audit.get("maintenance_geometry_improvement_ratio") or 0.0)
    maintenance_identity_improvement_ratio = float(runtime_audit.get("maintenance_identity_improvement_ratio") or 0.0)
    maintenance_proactive_suggestion_ratio = float(runtime_audit.get("maintenance_proactive_suggestion_ratio") or 0.0)
    maintenance_repeated_evidence_ratio = float(runtime_audit.get("maintenance_repeated_evidence_ratio") or 0.0)
    sleep_review_change_ratio = float(runtime_audit.get("sleep_review_change_ratio") or 0.0)
    sleep_bridge_adjustment_ratio = float(runtime_audit.get("sleep_bridge_adjustment_ratio") or 0.0)
    evolve_structural_change_ratio = float(runtime_audit.get("evolve_structural_change_ratio") or 0.0)
    evolve_new_highway_ratio = float(runtime_audit.get("evolve_new_highway_ratio") or 0.0)
    sleep_vs_evolve_overlap_ratio = float(runtime_audit.get("sleep_vs_evolve_overlap_ratio") or 0.0)
    maintenance_mode_specific_quality_delta = dict(runtime_audit.get("maintenance_mode_specific_quality_delta") or {})

    blackboards = []
    master_states = []
    worker_registries = []
    planner_seed_runtimes = []
    heavy_sessions = []
    nav_worker_sessions = []
    for session in recent_sessions:
        result = dict(session.get("result") or {})
        if not result:
            continue
        blackboard = _shared_blackboard(result)
        if blackboard:
            blackboards.append(blackboard)
        master_state = dict(blackboard.get("master_state") or {})
        if master_state:
            master_states.append(master_state)
        worker_registry = dict(blackboard.get("worker_registry") or {})
        if worker_registry:
            worker_registries.append(worker_registry)
            if any(str(worker_id or "").startswith("nav::") for worker_id in worker_registry):
                nav_worker_sessions.append(session)
        planner_runtime = dict(result.get("planner_runtime") or {})
        planner_seed_runtime = dict(result.get("planner_seed_runtime") or planner_runtime.get("planner_seed_runtime") or planner_runtime)
        if planner_seed_runtime and any(
            key in planner_seed_runtime
            for key in ("planner_seed_enabled", "planner_seed_status", "planner_seed_source", "answer_strands")
        ):
            planner_seed_runtimes.append(planner_seed_runtime)
        if str(result.get("retrieval_mode") or "") == "heavy":
            heavy_sessions.append(session)

    master_decision_sources = {
        str(decision.get("decision_source") or "")
        for state in master_states
        for decision in list(state.get("decision_history") or [])
        if str(decision.get("decision_source") or "")
    }
    heavy_long_form_recent = any(
        len(str((session.get("result") or {}).get("answer_full") or "")) >= 900
        and len(str((session.get("result") or {}).get("context_dossier") or "")) >= 900
        for session in heavy_sessions
    )
    planner_seed_sources = sorted(
        {
            str(runtime.get("planner_seed_source") or "").strip()
            for runtime in planner_seed_runtimes
            if str(runtime.get("planner_seed_source") or "").strip()
        }
    )
    planner_seed_contract_ready = (
        answer_strand_count > 0.0
        and seed_goal_coverage_ratio > 0.0
        and seed_destination_presence_ratio > 0.0
        and seed_used_by_bootstrap_ratio > 0.0
    )
    planner_merge_contract_ready = (
        dual_origin_branch_ratio > 0.0
        and (branch_reuse_ratio + branch_enrich_ratio + branch_fork_ratio) > 0.0
        and planner_family_attribution_ratio > 0.0
    ) or bool(planner_merge_benchmark_report.get("all_pass"))
    route_substrate_ready = (
        route_richness_score > 0.0
        and heuristic_family_route_step_ratio > 0.0
        and ai_family_route_step_ratio > 0.0
        and highway_effective_use_ratio > 0.0
        and destination_reached_ratio > 0.0
    ) or bool(route_richness_benchmark_report.get("all_pass"))
    heavy_long_form = heavy_long_form_recent or bool(dict(evaluation_matrix.get("context_richness") or {}).get("pass"))
    stream_replay_ready = all(
        bool(stream_ui_replay_readiness.get(key))
        for key in (
            "answer_surface_ready",
            "dossier_growth_ready",
            "evidence_ledger_ready",
            "timeline_ready",
            "graph_optional",
        )
    )
    route_truth_ready = all(
        bool(stream_ui_replay_readiness.get(key))
        for key in ("route_trace_ready", "route_travel_ready")
    )
    answer_surface_state_ready = bool(stream_ui_replay_readiness.get("answer_surface_states_ready"))
    closure_blockers_ready = bool(stream_ui_replay_readiness.get("closure_blockers_ready"))
    legacy_alive_ready = bool(stream_ui_replay_readiness.get("alive_not_static"))
    stream_green = bool(stream_report) and bool(stream_report.get("all_pass")) and stream_replay_ready and (
        route_truth_ready or legacy_alive_ready
    ) and route_substrate_ready
    master_closure_green = bool(master_closure_benchmark_report) and bool(master_closure_benchmark_report.get("all_pass"))
    recent_master_llm_seen = any(source == "llm" for source in master_decision_sources)
    master_ai_ready = master_llm_success_ratio > 0.0 and master_closure_green
    master_ai_gaps: list[str] = []
    if master_llm_success_ratio <= 0.0:
        master_ai_gaps.append("Aggregate audit does not show successful LLM master decisions.")
    if not master_closure_green:
        master_ai_gaps.append("Master closure benchmark is missing or not yet green.")
    maintenance_green = (
        stream_green
        and bool(maintenance_benchmark_report)
        and bool(maintenance_benchmark_report.get("all_pass"))
        and maintenance_run_count >= 3
        and applied_maintenance_run_count >= 2
        and maintenance_repeated_evidence_ratio >= 1.0
        and maintenance_proactive_suggestion_ratio > 0.0
        and maintenance_improvement_ratio > 0.0
        and (maintenance_geometry_improvement_ratio > 0.0 or maintenance_identity_improvement_ratio > 0.0)
        and sleep_review_change_ratio > 0.0
        and evolve_structural_change_ratio > 0.0
        and bool(maintenance_mode_specific_quality_delta)
        and raw_text_coverage_ratio >= 0.9
        and support_density >= 0.1
        and route_truth_ready
    )

    entries = [
        _entry(
            phase="phase_1_runtime_parity",
            guide_refs=["55.3"],
            status="pass" if planner_histogram and latest_benchmark else "partial",
            intended_behavior="Audit, planner mode, timings, and benchmark state must reflect the real runtime.",
            implemented_behavior="Audit exposes runtime signature, planner histogram, timing percentiles, and latest benchmark.",
            live_evidence={
                "planner_mode_histogram": planner_histogram,
                "last_benchmark_phase": latest_benchmark_phase,
                "slice1_revalidation_summary": slice1_summary,
            },
            open_gaps=[] if planner_histogram and latest_benchmark else ["Missing recent benchmark or planner histogram evidence."],
        ),
        _entry(
            phase="phase_2_blackboard_contract",
            guide_refs=["53.3", "53.7"],
            status="pass" if blackboards and worker_registries and master_states else "partial",
            intended_behavior="Every search should expose a canonical blackboard with master state and worker registry.",
            implemented_behavior="Recent sessions expose nested blackboard state, worker registry, and master state in results/traces.",
            live_evidence={
                "recent_blackboard_count": len(blackboards),
                "recent_worker_registry_count": len(worker_registries),
                "recent_master_state_count": len(master_states),
                "slice1_revalidation_summary": slice1_summary,
            },
            open_gaps=[] if blackboards and worker_registries and master_states else ["Recent sessions do not all expose full blackboard payloads."],
        ),
        _entry(
            phase="phase_3_ai_master",
            guide_refs=["53.2", "53.8", "55.4"],
            status="pass" if master_ai_ready else "partial",
            intended_behavior="The master should judge semantically with AI, using fallback rules only when necessary.",
            implemented_behavior="Master state is present and decision sources are explicit; fallback remains bounded and attributable when retrieval LLM times out or fails.",
            live_evidence={
                "master_decision_sources": sorted(master_decision_sources),
                "recent_master_llm_seen": recent_master_llm_seen,
                "master_ai_ready": master_ai_ready,
                "master_llm_success_ratio": master_llm_success_ratio,
                "master_fallback_timeout_ratio": master_fallback_timeout_ratio,
                "answer_now_before_exploration_complete_ratio": answer_now_before_exploration_complete_ratio,
                "final_closure_after_destination_resolution_ratio": final_closure_after_destination_resolution_ratio,
                "context_level_1_before_final_ratio": context_level_1_before_final_ratio,
                "master_surface_state_histogram": master_surface_state_histogram,
                "master_fallback_reason_histogram": master_fallback_reason_histogram,
                "closure_blocker_reason_histogram": closure_blocker_reason_histogram,
                "latest_master_closure_benchmark_phase": str(latest_master_closure_benchmark.get("phase") or "") or None,
                "latest_master_closure_all_pass": master_closure_green,
                "branch_controller_usage_ratio": branch_controller_usage_ratio,
                "branch_controller_override_ratio": branch_controller_override_ratio,
                "retrieval_llm_runtime": dict((runtime_audit.get("llm_runtime") or {}).get("retrieval") or {}),
                "slice1_ai_master_pass": bool(slice1_summary.get("phase_3_ai_master")) if slice1_summary else None,
            },
            open_gaps=master_ai_gaps,
        ),
        _entry(
            phase="phase_4_hybrid_race",
            guide_refs=["53.1", "53.5", "55.5"],
            status="pass"
            if planner_histogram.get("hybrid")
            and llm_scout_enabled_ratio > 0
            and planner_seed_contract_ready
            and planner_merge_contract_ready
            and route_substrate_ready
            and master_closure_green
            and final_closure_after_destination_resolution_ratio > 0.0
            else "partial",
            intended_behavior="Heuristic scout and planner-AI should launch in parallel by default.",
            implemented_behavior="Hybrid planner mode is active, the fast planner seed emits shared answer strands, bootstrap consumes those strands, and merge outcomes preserve AI attribution through explicit reuse, enrich, or fork decisions.",
            live_evidence={
                "hybrid_session_count": int(planner_histogram.get("hybrid") or 0),
                "llm_scout_enabled_ratio": llm_scout_enabled_ratio,
                "hybrid_merge_ratio": hybrid_merge_ratio,
                "planner_influence_ratio": planner_influence_ratio,
                "planner_family_dual_active_ratio": planner_family_dual_active_ratio,
                "planner_family_win_ratio": planner_family_win_ratio,
                "planner_family_overlap_ratio": planner_family_overlap_ratio,
                "planner_family_divergence_ratio": planner_family_divergence_ratio,
                "planner_seed_ms": planner_seed_ms,
                "planner_seed_success_ratio": planner_seed_success_ratio,
                "ai_material_contribution_ratio": ai_material_contribution_ratio,
                "ai_contribution_reason_histogram": ai_contribution_reason_histogram,
                "answer_strand_count": answer_strand_count,
                "seed_goal_coverage_ratio": seed_goal_coverage_ratio,
                "seed_destination_presence_ratio": seed_destination_presence_ratio,
                "seed_used_by_bootstrap_ratio": seed_used_by_bootstrap_ratio,
                "branch_reuse_ratio": branch_reuse_ratio,
                "branch_enrich_ratio": branch_enrich_ratio,
                "branch_fork_ratio": branch_fork_ratio,
                "dual_origin_branch_ratio": dual_origin_branch_ratio,
                "merge_resolution_histogram": merge_resolution_histogram,
                "planner_family_attribution_ratio": planner_family_attribution_ratio,
                "planner_merge_all_pass": bool(planner_merge_benchmark_report.get("all_pass")),
                "route_richness_score": route_richness_score,
                "heuristic_family_route_step_ratio": heuristic_family_route_step_ratio,
                "ai_family_route_step_ratio": ai_family_route_step_ratio,
                "dual_origin_family_route_step_ratio": dual_origin_family_route_step_ratio,
                "highway_effective_use_ratio": highway_effective_use_ratio,
                "destination_reached_ratio": destination_reached_ratio,
                "execution_reorder_count": execution_reorder_count,
                "execution_reorder_reasons": execution_reorder_reasons,
                "route_richness_all_pass": bool(route_richness_benchmark_report.get("all_pass")),
                "master_closure_all_pass": master_closure_green,
                "answer_now_before_exploration_complete_ratio": answer_now_before_exploration_complete_ratio,
                "final_closure_after_destination_resolution_ratio": final_closure_after_destination_resolution_ratio,
                "context_level_1_before_final_ratio": context_level_1_before_final_ratio,
                "closure_blocker_reason_histogram": closure_blocker_reason_histogram,
                "recent_planner_seed_runtime_count": len(planner_seed_runtimes),
                "recent_planner_seed_sources": planner_seed_sources,
                "planner_seed_contract_ready": planner_seed_contract_ready,
                "planner_merge_contract_ready": planner_merge_contract_ready,
                "heuristic_calibration_scope_count": heuristic_calibration_scope_count,
                "heuristic_calibration_event_count": heuristic_calibration_event_count,
                "heuristic_calibration_gain": heuristic_calibration_gain,
                "calibrated_bootstrap_success_ratio": calibrated_bootstrap_success_ratio,
                "calibrated_branch_count_delta": calibrated_branch_count_delta,
                "slice1_hybrid_race_pass": bool(slice1_summary.get("phase_4_hybrid_race")) if slice1_summary else None,
            },
            open_gaps=[]
            if planner_histogram.get("hybrid")
            and llm_scout_enabled_ratio > 0
            and planner_seed_contract_ready
            and planner_merge_contract_ready
            and route_substrate_ready
            and master_closure_green
            and final_closure_after_destination_resolution_ratio > 0.0
            else [
                "Hybrid race evidence is still incomplete in recent live sessions.",
                "Fast planner seed or shared answer strand evidence is still missing or too weak in audit data.",
                "Dual-family merge evidence is still missing, or AI attribution is being lost after convergence.",
                "Route substrate evidence is still too weak: one family may still look landing-only, or highways are only considered and not traversed effectively.",
                "Master closure evidence is still too weak: final sealing may still be happening without destination-resolution proof.",
            ],
        ),
        _entry(
            phase="phase_5_ai_navigation",
            guide_refs=["53.4", "53.6", "55.6"],
            status="pass" if nav_worker_sessions else "partial",
            intended_behavior="Navigator AI workers should appear as real runtime workers on qualifying searches.",
            implemented_behavior="Recent heavy sessions expose `nav::` workers in the shared worker registry.",
            live_evidence={
                "recent_nav_worker_sessions": len(nav_worker_sessions),
                "slice1_ai_navigation_pass": bool(slice1_summary.get("phase_5_ai_navigation")) if slice1_summary else None,
            },
            open_gaps=[] if nav_worker_sessions else ["Recent sessions did not expose AI navigation workers."],
        ),
        _entry(
            phase="phase_6_modes_and_long_form",
            guide_refs=["54.1", "54.2", "54.7", "55.7", "55.8"],
            status="pass" if heavy_long_form else "partial",
            intended_behavior="Broad/heavy queries should auto-route to heavy mode and produce full dossier-scale outputs.",
            implemented_behavior="Heavy sessions now emit long `answer_full` and `context_dossier` payloads.",
            live_evidence={
                "recent_heavy_session_count": len(heavy_sessions),
                "heavy_long_form_detected": heavy_long_form,
                "heavy_long_form_recent": heavy_long_form_recent,
                "latest_evaluation_phase": str(latest_evaluation_benchmark.get("phase") or "") or None,
                "evaluation_context_richness_pass": bool(dict(evaluation_matrix.get("context_richness") or {}).get("pass")) if evaluation_matrix else None,
                "slice1_long_form_pass": bool(slice1_summary.get("phase_6_modes_and_long_form")) if slice1_summary else None,
            },
            open_gaps=[] if heavy_long_form else ["Recent heavy sessions and the final evaluation matrix still do not prove dossier-scale heavy output."],
        ),
        _entry(
            phase="phase_7_stream_and_ui_support",
            guide_refs=["53.10", "54.8", "54.9", "55.9", "55.10"],
            status="pass" if stream_green and answer_surface_state_ready and closure_blockers_ready else "partial",
            intended_behavior="The stream should visibly fill context and the UI should be able to replay it.",
            implemented_behavior="Stream suites are benchmarked against live dossier growth, partial answer emission, canonical route replay readiness, and reservoir/context-quality visibility while the graph remains secondary.",
            live_evidence={
                "latest_benchmark_phase": latest_benchmark_phase,
                "latest_stream_benchmark_phase": stream_benchmark_phase or None,
                "stream_report_all_pass": bool(stream_report.get("all_pass")) if stream_report else None,
                "stream_ui_replay_readiness": stream_ui_replay_readiness or None,
                "answer_surface_state_ready": answer_surface_state_ready,
                "closure_blockers_ready": closure_blockers_ready,
                "warm_hit_ratio": warm_hit_ratio,
                "warm_partial_reuse_ratio": warm_partial_reuse_ratio,
                "divergence_reset_ratio": divergence_reset_ratio,
                "answer_now_before_final_ratio": answer_now_before_final_ratio,
                "answer_now_before_exploration_complete_ratio": answer_now_before_exploration_complete_ratio,
                "context_level_1_before_final_ratio": context_level_1_before_final_ratio,
                "final_closure_after_destination_resolution_ratio": final_closure_after_destination_resolution_ratio,
                "background_expansion_after_partial_ratio": background_expansion_after_partial_ratio,
                "document_mode_detected_ratio": float(runtime_audit.get("document_mode_detected_ratio") or 0.0),
                "document_anchor_top_match_ratio": float(runtime_audit.get("document_anchor_top_match_ratio") or 0.0),
                "document_chunk_used_before_final_ratio": float(runtime_audit.get("document_chunk_used_before_final_ratio") or 0.0),
                "document_fact_support_ratio": float(runtime_audit.get("document_fact_support_ratio") or 0.0),
                "raw_text_coverage_ratio": raw_text_coverage_ratio,
                "document_chunk_coverage_ratio": document_chunk_coverage_ratio,
                "support_density": support_density,
                "contradiction_exposure_ratio": contradiction_exposure_ratio,
                "ai_material_contribution_ratio": ai_material_contribution_ratio,
                "ai_contribution_reason_histogram": ai_contribution_reason_histogram,
                "highway_route_yield": highway_route_yield,
                "route_richness_score": route_richness_score,
                "highway_effective_use_ratio": highway_effective_use_ratio,
                "link_effective_use_ratio": link_effective_use_ratio,
                "heuristic_family_route_step_ratio": heuristic_family_route_step_ratio,
                "ai_family_route_step_ratio": ai_family_route_step_ratio,
                "dual_origin_family_route_step_ratio": dual_origin_family_route_step_ratio,
                "execution_reorder_count": execution_reorder_count,
                "execution_reorder_reasons": execution_reorder_reasons,
                "route_richness_all_pass": bool(route_richness_benchmark_report.get("all_pass")),
                "branch_duplication_ratio": branch_duplication_ratio,
                "branch_merge_ratio": branch_merge_ratio,
                "warm_context_reuse_quality": warm_context_reuse_quality,
                "mode_timing_percentiles": mode_timing_percentiles or None,
            },
            open_gaps=[]
            if stream_green and answer_surface_state_ready and closure_blockers_ready
            else [
                "Stream benchmark evidence is missing, stale, or not yet route/UI replay ready.",
                "Warm carryover and answer-now runtime evidence are not yet strong enough in recent audit data.",
                "Answer-surface lifecycle states or closure blockers are not yet visible enough in stream payloads.",
            ],
        ),
        _entry(
            phase="phase_8_sleep_evolve_and_memory_depth",
            guide_refs=["55.11", "55.12"],
            status="pass" if maintenance_green else "partial",
            intended_behavior="Sleep/evolve should learn from traces, corrections, region summaries, preserve long node content, and surface proactive follow-up work backed by repeated live evidence.",
            implemented_behavior="Maintenance now persists repeated review/apply runs, measures before/after quality deltas, records proactive follow-up candidates, and can promote phase 8 only when repeated benchmarked evidence remains strong.",
            live_evidence={
                "identity_memory_ratio": float(runtime_audit.get("identity_memory_ratio") or 0.0),
                "guide_area_blank_ratio": float(runtime_audit.get("guide_area_blank_ratio") or 0.0),
                "maintenance_run_count": maintenance_run_count,
                "applied_maintenance_run_count": applied_maintenance_run_count,
                "maintenance_modes_histogram": maintenance_modes_histogram,
                "maintenance_improvement_ratio": maintenance_improvement_ratio,
                "maintenance_geometry_improvement_ratio": maintenance_geometry_improvement_ratio,
                "maintenance_identity_improvement_ratio": maintenance_identity_improvement_ratio,
                "maintenance_proactive_suggestion_ratio": maintenance_proactive_suggestion_ratio,
                "maintenance_repeated_evidence_ratio": maintenance_repeated_evidence_ratio,
                "sleep_review_change_ratio": sleep_review_change_ratio,
                "sleep_bridge_adjustment_ratio": sleep_bridge_adjustment_ratio,
                "evolve_structural_change_ratio": evolve_structural_change_ratio,
                "evolve_new_highway_ratio": evolve_new_highway_ratio,
                "sleep_vs_evolve_overlap_ratio": sleep_vs_evolve_overlap_ratio,
                "maintenance_mode_specific_quality_delta": maintenance_mode_specific_quality_delta,
                "heuristic_calibration_scope_count": heuristic_calibration_scope_count,
                "heuristic_calibration_event_count": heuristic_calibration_event_count,
                "heuristic_calibration_gain": heuristic_calibration_gain,
                "calibrated_bootstrap_success_ratio": calibrated_bootstrap_success_ratio,
                "calibrated_branch_count_delta": calibrated_branch_count_delta,
                "calibrated_highway_use_delta": calibrated_highway_use_delta,
                "latest_maintenance_benchmark_phase": str(latest_maintenance_benchmark.get("phase") or "") or None,
                "latest_maintenance_benchmark_all_pass": bool(maintenance_benchmark_report.get("all_pass")) if maintenance_benchmark_report else None,
                "raw_text_coverage_ratio": raw_text_coverage_ratio,
                "support_density": support_density,
                "route_trace_ready": route_truth_ready,
            },
            open_gaps=[]
            if maintenance_green
            else [
                "Need repeated live maintenance runs with at least two applied passes to prove learning over time.",
                "Need proactive follow-up signals and measurable quality improvements across maintenance runs.",
                "Geometry density or identity overconcentration evidence is still not strong enough to claim final closure.",
            ],
        ),
    ]
    status_counts = {
        "pass": sum(1 for entry in entries if entry["status"] == "pass"),
        "partial": sum(1 for entry in entries if entry["status"] == "partial"),
        "fail": sum(1 for entry in entries if entry["status"] == "fail"),
    }
    return {
        "generated_at": utc_timestamp(),
        "entries": entries,
        "summary": {
            "status_counts": status_counts,
            "recent_session_count": len(recent_sessions),
            "guide_aligned": status_counts["fail"] == 0 and status_counts["partial"] == 0,
        },
    }


def build_guide_checklist(matrix: dict[str, Any]) -> dict[str, str]:
    return {
        str(entry.get("phase") or ""): str(entry.get("status") or "partial")
        for entry in list(matrix.get("entries") or [])
        if str(entry.get("phase") or "")
    }
