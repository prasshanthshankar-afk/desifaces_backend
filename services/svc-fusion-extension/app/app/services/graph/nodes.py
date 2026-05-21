from __future__ import annotations

from typing import Any, Dict, List

from app.services.graph.state import StoryGraphState, ShotPlanItem, ChildRenderRecord


class StoryGraphNodes:
    def __init__(
        self,
        *,
        pricing_service: Any,
        render_router_service: Any,
        stitch_service: Any,
        asset_resolver_service: Any,
        direction_parser_service: Any = None,
        shot_planner_service: Any = None,
    ) -> None:
        self.pricing_service = pricing_service
        self.render_router_service = render_router_service
        self.stitch_service = stitch_service
        self.asset_resolver_service = asset_resolver_service
        self.direction_parser_service = direction_parser_service
        self.shot_planner_service = shot_planner_service

    async def load_inputs(self, state: StoryGraphState) -> StoryGraphState:
        resolved = await self.asset_resolver_service.resolve_story_inputs(
            job_id=state["job_id"],
            user_id=state["user_id"],
            face_artifact_id=state["face_artifact_id"],
            audio_artifact_id=state["audio_artifact_id"],
        )
        state["face_ref_urls"] = list(resolved.get("face_ref_urls") or [])
        state["audio_url"] = str(resolved.get("audio_url") or "")
        state["transcript"] = str(resolved.get("transcript") or "")
        state["word_timings"] = list(resolved.get("word_timings") or [])
        state["audio_duration_sec"] = float(resolved.get("audio_duration_sec") or 0.0)
        state["status"] = "inputs_loaded"
        return state

    async def reserve_parent_pricing(self, state: StoryGraphState) -> StoryGraphState:
        result = await self.pricing_service.reserve_story_job(
            job_id=state["job_id"],
            user_id=state["user_id"],
            story_input={
                "face_artifact_id": state["face_artifact_id"],
                "audio_artifact_id": state["audio_artifact_id"],
                "video_direction": state["video_direction"],
                "aspect_ratio": state.get("aspect_ratio") or "9:16",
            },
            plan={
                "audio_duration_sec": state.get("audio_duration_sec") or 0.0,
                "shot_plan": state.get("shot_plan") or [],
            },
            current_pricing=state.get("pricing") or {},
        )
        state["pricing"] = dict(result.get("pricing") or {})
        state["pricing_summary"] = dict(result.get("pricing_summary") or {})
        state["status"] = "pricing_reserved"
        return state

    async def parse_video_direction(self, state: StoryGraphState) -> StoryGraphState:
        if self.direction_parser_service is not None:
            parsed = await self.direction_parser_service.parse(
                video_direction=state["video_direction"],
                transcript=state.get("transcript") or "",
                audio_duration_sec=state.get("audio_duration_sec") or 0.0,
            )
        else:
            parsed = {
                "emotion": "neutral",
                "gesture_style": "moderate",
                "movement_intensity": "medium",
                "background_style": "cinematic",
                "camera_style": "portrait_medium",
                "insert_density": "medium",
            }
        state["parsed_direction"] = dict(parsed or {})
        state["status"] = "direction_parsed"
        return state

    async def build_shot_plan(self, state: StoryGraphState) -> StoryGraphState:
        if self.shot_planner_service is not None:
            plan = await self.shot_planner_service.build_shot_plan(
                parsed_direction=state.get("parsed_direction") or {},
                audio_duration_sec=state.get("audio_duration_sec") or 0.0,
                transcript=state.get("transcript") or "",
                word_timings=state.get("word_timings") or [],
                aspect_ratio=state.get("aspect_ratio") or "9:16",
            )
        else:
            duration = max(float(state.get("audio_duration_sec") or 0.0), 1.0)
            anchor_end = min(duration, 6.0)
            plan = [
                {
                    "shot_id": "anchor_1",
                    "shot_type": "anchor_closeup",
                    "render_kind": "anchor",
                    "provider_hint": "heygen",
                    "start_sec": 0.0,
                    "end_sec": anchor_end,
                    "aspect_ratio": state.get("aspect_ratio") or "9:16",
                    "background_style": (state.get("parsed_direction") or {}).get("background_style", "cinematic"),
                    "camera_style": (state.get("parsed_direction") or {}).get("camera_style", "portrait_medium"),
                }
            ]
            if duration > 8.0:
                plan.append(
                    {
                        "shot_id": "insert_1",
                        "shot_type": "dynamic_insert",
                        "render_kind": "dynamic_insert",
                        "provider_hint": "runway",
                        "start_sec": anchor_end,
                        "end_sec": min(duration, anchor_end + 4.0),
                        "aspect_ratio": state.get("aspect_ratio") or "9:16",
                        "background_style": (state.get("parsed_direction") or {}).get("background_style", "cinematic"),
                        "camera_style": "dynamic",
                        "gesture_style": (state.get("parsed_direction") or {}).get("gesture_style", "moderate"),
                        "movement_intensity": (state.get("parsed_direction") or {}).get("movement_intensity", "medium"),
                    }
                )
        state["shot_plan"] = [ShotPlanItem(**item) for item in plan]
        state["status"] = "shot_plan_built"
        return state

    async def route_providers(self, state: StoryGraphState) -> StoryGraphState:
        shot_plan = state.get("shot_plan") or []
        provider_plan: Dict[str, Any] = {"anchor_provider": "heygen", "dynamic_provider": "runway", "shots": []}
        for shot in shot_plan:
            routed = dict(shot)
            if shot.get("render_kind") == "anchor":
                routed["provider_hint"] = "heygen"
            elif not routed.get("provider_hint"):
                routed["provider_hint"] = provider_plan["dynamic_provider"]
            provider_plan["shots"].append(routed)
        state["provider_plan"] = provider_plan
        state["status"] = "providers_routed"
        return state

    async def submit_anchor_shots(self, state: StoryGraphState) -> StoryGraphState:
        submitted: List[ChildRenderRecord] = []
        for shot in state.get("provider_plan", {}).get("shots", []):
            if shot.get("render_kind") != "anchor":
                continue
            result = await self.render_router_service.submit_child_render(
                parent_story_job_id=state["job_id"],
                render_kind="anchor",
                provider_hint=str(shot.get("provider_hint") or "heygen"),
                face_artifact_id=state["face_artifact_id"],
                audio_artifact_id=state.get("audio_artifact_id"),
                video_direction=state["video_direction"],
                shot_spec=shot,
                aspect_ratio=state.get("aspect_ratio") or "9:16",
            )
            submitted.append(
                ChildRenderRecord(
                    shot_id=str(shot.get("shot_id") or ""),
                    render_kind="anchor",
                    provider_hint=str(shot.get("provider_hint") or "heygen"),
                    fusion_job_id=str(result.get("job_id") or ""),
                    status="queued",
                    start_sec=float(shot.get("start_sec") or 0.0),
                    end_sec=float(shot.get("end_sec") or 0.0),
                )
            )
        state["anchor_jobs"] = submitted
        state["status"] = "anchor_submitted"
        return state

    async def submit_dynamic_insert_jobs(self, state: StoryGraphState) -> StoryGraphState:
        submitted: List[ChildRenderRecord] = []
        for shot in state.get("provider_plan", {}).get("shots", []):
            if shot.get("render_kind") != "dynamic_insert":
                continue
            result = await self.render_router_service.submit_child_render(
                parent_story_job_id=state["job_id"],
                render_kind="dynamic_insert",
                provider_hint=str(shot.get("provider_hint") or "runway"),
                face_artifact_id=state["face_artifact_id"],
                audio_artifact_id=None,
                video_direction=state["video_direction"],
                shot_spec=shot,
                aspect_ratio=state.get("aspect_ratio") or "9:16",
            )
            submitted.append(
                ChildRenderRecord(
                    shot_id=str(shot.get("shot_id") or ""),
                    render_kind="dynamic_insert",
                    provider_hint=str(shot.get("provider_hint") or "runway"),
                    fusion_job_id=str(result.get("job_id") or ""),
                    status="queued",
                    start_sec=float(shot.get("start_sec") or 0.0),
                    end_sec=float(shot.get("end_sec") or 0.0),
                )
            )
        state["insert_jobs"] = submitted
        state["status"] = "inserts_submitted"
        return state

    async def poll_child_jobs(self, state: StoryGraphState) -> StoryGraphState:
        anchor_jobs = await self.render_router_service.poll_child_jobs(state.get("anchor_jobs") or [])
        insert_jobs = await self.render_router_service.poll_child_jobs(state.get("insert_jobs") or [])
        state["anchor_jobs"] = anchor_jobs
        state["insert_jobs"] = insert_jobs
        failed = [job for job in [*anchor_jobs, *insert_jobs] if str(job.get("status") or "") == "failed"]
        pending = [job for job in [*anchor_jobs, *insert_jobs] if str(job.get("status") or "") not in {"succeeded", "failed"}]
        if failed:
            state.setdefault("failures", []).extend(failed)
            state["status"] = "child_failed"
        elif pending:
            state["status"] = "children_processing"
        else:
            state["status"] = "children_completed"
        return state

    async def compose_timeline(self, state: StoryGraphState) -> StoryGraphState:
        clip_inputs = [*(state.get("anchor_jobs") or []), *(state.get("insert_jobs") or [])]
        result = await self.stitch_service.compose_story_timeline(
            job_id=state["job_id"],
            user_id=state["user_id"],
            audio_url=state["audio_url"],
            shot_plan=state.get("shot_plan") or [],
            clip_jobs=clip_inputs,
        )
        state["composed_video_url"] = result.get("final_video_url")
        state["final_artifact_id"] = result.get("final_artifact_id")
        state["status"] = "timeline_composed"
        return state

    async def commit_parent_pricing(self, state: StoryGraphState) -> StoryGraphState:
        result = await self.pricing_service.commit_story_job(
            job_id=state["job_id"],
            user_id=state["user_id"],
            pricing=state.get("pricing") or {},
            final_video_duration_sec=float(state.get("audio_duration_sec") or 0.0),
        )
        state["pricing"] = dict(result.get("pricing") or {})
        state["pricing_summary"] = dict(result.get("pricing_summary") or {})
        state["status"] = "pricing_committed"
        return state

    async def release_parent_pricing(self, state: StoryGraphState) -> StoryGraphState:
        result = await self.pricing_service.release_story_job(
            job_id=state["job_id"],
            user_id=state["user_id"],
            pricing=state.get("pricing") or {},
            reason="story_render_failed",
        )
        state["pricing"] = dict(result.get("pricing") or {})
        state["pricing_summary"] = dict(result.get("pricing_summary") or {})
        state["status"] = "pricing_released"
        return state

    async def mark_complete(self, state: StoryGraphState) -> StoryGraphState:
        state["status"] = "completed"
        return state
