from app.service import operational_generation_answer


def _context(*jobs, live=True):
    return {
        "surface": "web",
        "screen": "dashboard",
        "live_context_available": live,
        "generation": list(jobs),
    }


def test_latest_generation_returns_actual_first_record():
    answer = operational_generation_answer(
        "What is the status of my most recent generation?",
        _context(
            {
                "kind": "face",
                "people_mode": "single_or_unspecified",
                "status": "completed",
                "stage": "finalized",
                "progress": "100",
                "updated_at": "2026-08-29T18:42:00+00:00",
            },
            {
                "kind": "audio",
                "people_mode": "single_or_unspecified",
                "status": "failed",
                "updated_at": "2026-08-29T18:30:00+00:00",
            },
        ),
    )
    assert answer is not None
    assert "most recent Face generation" in answer
    assert "**completed**" in answer
    assert "Current stage: **finalized**" in answer
    assert "Progress: **100**" in answer
    assert "2026-08-29T18:42:00+00:00" in answer


def test_multi_person_video_query_selects_matching_record_not_first_unrelated_job():
    answer = operational_generation_answer(
        "What is the status of my multi-person video?",
        _context(
            {
                "kind": "face",
                "people_mode": "single_or_unspecified",
                "status": "completed",
            },
            {
                "kind": "video",
                "people_mode": "multi_person",
                "status": "processing",
                "stage": "stitching",
                "progress": "92",
                "final_output_available": False,
                "updated_at": "2026-08-29T18:40:00+00:00",
            },
        ),
    )
    assert answer is not None
    assert "multi-person video generation" in answer
    assert "**processing**" in answer
    assert "Current stage: **stitching**" in answer
    assert "Progress: **92**" in answer
    assert "Final video: **not available yet**" in answer


def test_completed_video_reports_actual_final_output_availability():
    answer = operational_generation_answer(
        "Is my latest video complete?",
        _context(
            {
                "kind": "video",
                "people_mode": "multi_person",
                "status": "completed",
                "stage": "complete",
                "final_output_available": True,
            }
        ),
    )
    assert answer is not None
    assert "**completed**" in answer
    assert "Final video: **available**" in answer


def test_completed_video_without_final_output_reports_data_mismatch_specifically():
    answer = operational_generation_answer(
        "What is the status of my latest video?",
        _context(
            {
                "kind": "video",
                "people_mode": "single_or_unspecified",
                "status": "completed",
                "final_output_available": False,
            }
        ),
    )
    assert answer is not None
    assert "marked complete" in answer
    assert "not recorded as available yet" in answer


def test_failed_generation_reports_failure_code_and_retryability():
    answer = operational_generation_answer(
        "Why did my latest audio generation fail?",
        _context(
            {
                "kind": "audio",
                "people_mode": "single_or_unspecified",
                "status": "failed",
                "stage": "tts",
                "failure_code": "VOICE_UNAVAILABLE",
                "retryable": True,
            }
        ),
    )
    assert answer is not None
    assert "**failed**" in answer
    assert "Failure code: **VOICE_UNAVAILABLE**" in answer
    assert "Retry: **available**" in answer


def test_no_matching_typed_generation_is_specific_and_does_not_send_user_elsewhere():
    answer = operational_generation_answer(
        "What is the status of my latest video?",
        _context(
            {
                "kind": "face",
                "people_mode": "single_or_unspecified",
                "status": "completed",
            }
        ),
    )
    assert answer is not None
    assert "do not see a recent **video** generation" in answer
    assert "open" not in answer.lower()
    assert "studio" not in answer.lower()


def test_live_context_unavailable_is_explicit():
    answer = operational_generation_answer(
        "What is the status of my most recent generation?",
        _context(live=False),
    )
    assert answer is not None
    assert "live generation history is unavailable" in answer


def test_general_product_question_still_uses_rag_llm_path():
    assert operational_generation_answer(
        "What is the difference between Face Studio and Fusion?",
        _context(),
    ) is None
