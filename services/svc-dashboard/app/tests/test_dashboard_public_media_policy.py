import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/desifaces_v3")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault(
    "AZURE_STORAGE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=https;AccountName=testaccount;AccountKey=QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=;EndpointSuffix=core.windows.net",
)

from app.services import dashboard_public_media_service as policy


class PublicMediaClassifierTests(unittest.TestCase):
    def test_screenshot_story_dialogue_audio_is_hidden(self):
        item = {
            "studio": "audio",
            "title": (
                "story_dialogue_workflow_id=ccdc73bc-9c1b-47cb-9c11-b30e1279a589 "
                "participant_id=ef642256-9264-585a-9266-38b9740b3322 "
                "dialogue_turn_id=e10a5374-4b32-51fd-ad8f-f8f4dc017a86 "
                "scene_id=a042c948-3be2-5bc2-9ee7-2c7b8c2388fa"
            ),
        }
        self.assertTrue(policy.is_internal_customer_hidden_item(item))

    def test_standalone_audio_is_visible(self):
        item = {
            "studio": "audio",
            "title": "Tamil narration preview",
            "reuse_payload": {"audio_url": "https://example.test/audio.mp3"},
        }
        self.assertFalse(policy.is_internal_customer_hidden_item(item))

    def test_fusion_scene_child_is_hidden(self):
        item = {
            "studio": "fusion",
            "title": "Scene 3",
            "metadata_json": {
                "scene_id": "scene-3",
                "render_kind": "child_render",
                "parent_longform_job_id": "parent-1",
            },
        }
        self.assertTrue(policy.is_internal_customer_hidden_item(item))

    def test_explicit_final_fusion_survives_scene_context(self):
        item = {
            "studio": "fusion",
            "title": "Final story",
            "metadata_json": {
                "scene_id": "scene-3",
                "render_kind": "final",
                "output_role": "final",
                "share_url": "https://example.test/final.mp4",
            },
        }
        self.assertFalse(policy.is_internal_customer_hidden_item(item))

    def test_final_video_row_is_visible(self):
        item = {
            "studio": "video",
            "title": "Talking Video",
            "metadata_json": {"output_role": "final"},
        }
        self.assertFalse(policy.is_internal_customer_hidden_item(item))


class PublicMediaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orig_base = policy._base_get_dashboard_library
        self.orig_policy_rows = policy._policy_rows
        self.orig_normalize = policy._normalize_library_item
        self.orig_signer = policy.AzureBlobSasSigner.from_connection_string

    async def asyncTearDown(self):
        policy._base_get_dashboard_library = self.orig_base
        policy._policy_rows = self.orig_policy_rows
        policy._normalize_library_item = self.orig_normalize
        policy.AzureBlobSasSigner.from_connection_string = self.orig_signer

    async def test_all_hides_internal_story_audio_but_keeps_standalone_audio(self):
        internal = {
            "library_id": "audio:internal",
            "source_job_id": "job-internal",
            "studio": "audio",
            "title": "story_dialogue_workflow_id=w1 dialogue_turn_id=t1 scene_id=s1",
            "preview_url": "https://example.test/internal.mp3",
        }
        standalone = {
            "library_id": "audio:standalone",
            "source_job_id": "job-standalone",
            "studio": "audio",
            "title": "Saved narration",
            "preview_url": "https://example.test/standalone.mp3",
        }

        async def fake_base(*args, **kwargs):
            return {"items": [internal, standalone], "total": 2, "source": "base"}

        async def fake_rows(*args, **kwargs):
            return [internal]

        policy._base_get_dashboard_library = fake_base
        policy._policy_rows = fake_rows

        out = await policy.get_dashboard_library(object(), "u1", "all", 100, 0)
        ids = [x["library_id"] for x in out["items"]]

        self.assertEqual(ids, ["audio:standalone"])
        self.assertEqual(out["display_scope"], "customer_final_outputs")

    async def test_video_adds_public_fusion_final_and_rejects_fusion_child(self):
        normal_video = {
            "library_id": "video:normal",
            "source_job_id": "job-normal",
            "studio": "video",
            "title": "Talking Video",
            "preview_url": "https://example.test/normal.mp4",
            "thumbnail_url": "https://example.test/normal.jpg",
        }
        fusion_final_raw = {
            "library_id": "fusion:final",
            "source_job_id": "fusion-final-job",
            "studio": "fusion",
            "title": "Final Multi-Person Story",
            "preview_url": "https://example.test/final.mp4",
            "thumbnail_url": "https://example.test/final.jpg",
            "metadata_json": {"render_kind": "final", "output_role": "final"},
        }
        fusion_child_raw = {
            "library_id": "fusion:child",
            "source_job_id": "fusion-child-job",
            "studio": "fusion",
            "title": "Scene 2",
            "preview_url": "https://example.test/scene2.mp4",
            "metadata_json": {"scene_id": "scene-2", "render_kind": "child_render"},
        }

        async def fake_base(*args, **kwargs):
            return {"items": [normal_video], "total": 1, "source": "base"}

        async def fake_rows(*args, **kwargs):
            return [fusion_final_raw, fusion_child_raw]

        def fake_normalize(row, signer):
            if row["library_id"] == "fusion:final":
                return {
                    "library_id": "fusion:final",
                    "source_job_id": "fusion-final-job",
                    "studio": "video",
                    "asset_type": "video",
                    "title": "Final Multi-Person Story",
                    "preview_url": "https://example.test/final.mp4",
                    "thumbnail_url": "https://example.test/final.jpg",
                    "reuse_payload": {"video_url": "https://example.test/final.mp4"},
                }
            raise AssertionError("child fusion row must never be normalized for publication")

        policy._base_get_dashboard_library = fake_base
        policy._policy_rows = fake_rows
        policy._normalize_library_item = fake_normalize
        policy.AzureBlobSasSigner.from_connection_string = lambda _: object()

        out = await policy.get_dashboard_library(object(), "u1", "video", 100, 0)
        ids = {x["library_id"] for x in out["items"]}

        self.assertIn("video:normal", ids)
        self.assertIn("fusion:final", ids)
        self.assertNotIn("fusion:child", ids)

        final = next(x for x in out["items"] if x["library_id"] == "fusion:final")
        self.assertTrue(final.get("thumbnail_url"))


if __name__ == "__main__":
    unittest.main()
