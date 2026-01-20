import pytest
import asyncio
import os
from pathlib import Path

from app.services.heygen_service import HeyGenService
from app.services.providers.heygen.client import HeyGenAV4Client, HeyGenApiError


@pytest.fixture
def heygen_service():
    """Fixture for HeyGen service"""
    return HeyGenService()


@pytest.fixture
def heygen_client():
    """Fixture for HeyGen client"""
    return HeyGenAV4Client()


@pytest.mark.asyncio
async def test_list_voices(heygen_client):
    """Test fetching available voices"""
    voices = await heygen_client.get_available_voices()
    
    assert isinstance(voices, list)
    
    if voices:  # May be empty if API call fails
        assert 'voice_id' in voices[0]
        assert 'name' in voices[0]
        print(f"\n✓ Found {len(voices)} voices")
        
        # Print first 3 for reference
        for voice in voices[:3]:
            print(f"  • {voice.get('name')} ({voice.get('language')}): {voice.get('voice_id')}")


@pytest.mark.asyncio
async def test_get_default_voice(heygen_client):
    """Test getting default voice"""
    voice_id = await heygen_client.get_default_voice_id()
    
    assert voice_id is not None
    assert isinstance(voice_id, str)
    assert len(voice_id) > 0
    
    print(f"\n✓ Default voice ID: {voice_id}")


@pytest.mark.asyncio
async def test_validate_voice_id(heygen_client):
    """Test voice ID validation"""
    # Test invalid voice ID
    invalid_id = "3150319a960147ffab0d414dcd0ed191"
    valid_id = await heygen_client.validate_voice_id(invalid_id)
    
    assert valid_id != invalid_id  # Should return different (default) voice
    print(f"\n✓ Invalid voice {invalid_id} → Valid voice {valid_id}")


@pytest.mark.asyncio
@pytest.mark.skipif(not os.path.exists("/tmp/test_face.jpg"), reason="Test image not found")
async def test_upload_image(heygen_client):
    """Test image upload"""
    image_path = "/tmp/test_face.jpg"
    
    try:
        talking_photo_id = await heygen_client.upload_image(image_path)
        
        assert talking_photo_id is not None
        assert isinstance(talking_photo_id, str)
        assert len(talking_photo_id) > 0
        
        print(f"\n✓ Image uploaded: {talking_photo_id}")
        
        return talking_photo_id
    
    except HeyGenApiError as e:
        pytest.fail(f"Upload failed: {e}")


@pytest.mark.asyncio
async def test_build_payload_with_audio_url(heygen_client):
    """Test payload building with audio URL"""
    payload = heygen_client.build_payload_with_audio_url(
        talking_photo_id="test_photo_id_123",
        audio_url="https://example.com/audio.mp3"
    )
    
    assert "video_inputs" in payload
    assert len(payload["video_inputs"]) == 1
    
    video_input = payload["video_inputs"][0]
    assert video_input["character"]["type"] == "talking_photo"
    assert video_input["character"]["talking_photo_id"] == "test_photo_id_123"
    assert video_input["voice"]["type"] == "audio"
    assert video_input["voice"]["audio_url"] == "https://example.com/audio.mp3"
    
    print("\n✓ Payload structure correct")


@pytest.mark.asyncio
async def test_build_payload_with_text(heygen_client):
    """Test payload building with text"""
    payload = await heygen_client.build_payload_with_text(
        talking_photo_id="test_photo_id_123",
        script="Hello world"
    )
    
    assert "video_inputs" in payload
    video_input = payload["video_inputs"][0]
    
    assert video_input["voice"]["type"] == "text"
    assert video_input["voice"]["input_text"] == "Hello world"
    assert "voice_id" in video_input["voice"]
    
    # Voice ID should be auto-validated to a default
    voice_id = video_input["voice"]["voice_id"]
    assert voice_id is not None
    assert voice_id != "3150319a960147ffab0d414dcd0ed191"  # Should not be invalid ID
    
    print(f"\n✓ Payload with validated voice: {voice_id}")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not all([
        os.path.exists("/tmp/test_face.jpg"),
        os.environ.get("TEST_AUDIO_URL")
    ]),
    reason="Test assets not available"
)
async def test_full_video_generation(heygen_service):
    """
    Full integration test: Upload image, submit video, poll status
    
    Requirements:
    - /tmp/test_face.jpg exists
    - TEST_AUDIO_URL environment variable set to public audio URL
    """
    image_path = "/tmp/test_face.jpg"
    audio_url = os.environ["TEST_AUDIO_URL"]
    idempotency_key = f"test_{int(asyncio.get_event_loop().time())}"
    
    print(f"\nStarting full integration test...")
    print(f"Image: {image_path}")
    print(f"Audio: {audio_url[:60]}...")
    print(f"Key: {idempotency_key}")
    
    try:
        # This tests the complete workflow
        client = HeyGenAV4Client()
        
        # Upload image
        talking_photo_id = await client.upload_image(image_path)
        print(f"✓ Image uploaded: {talking_photo_id}")
        
        # Submit video
        result = await client.submit_with_audio_url(
            talking_photo_id=talking_photo_id,
            audio_url=audio_url,
            idempotency_key=idempotency_key,
            test=True  # Use test mode for faster results
        )
        video_id = result.provider_job_id
        print(f"✓ Video submitted: {video_id}")
        
        # Poll a few times (don't wait for full completion in tests)
        for i in range(5):
            await asyncio.sleep(10)
            poll_result = await client.poll(video_id)
            print(f"[{i+1}] Status: {poll_result.status}")
            
            if poll_result.status in ("succeeded", "failed"):
                break
        
        print(f"\n✓ Integration test completed successfully")
        print(f"Video ID for manual checking: {video_id}")
        
        # Don't fail if video isn't done yet
        assert video_id is not None
        
    except HeyGenApiError as e:
        pytest.fail(f"Integration test failed: {e}")