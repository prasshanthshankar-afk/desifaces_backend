import asyncio
from app.services.heygen_service import HeyGenService


async def example_1_basic_video():
    """Basic video generation with Azure TTS audio"""
    service = HeyGenService()
    
    result = await service.create_video_from_azure_assets(
        face_image_path="/tmp/customer_face.jpg",
        audio_blob_path="heygen-audio/customer_tts.mp3",
        idempotency_key="order_12345"
    )
    
    print(f"Status: {result['status']}")
    print(f"Video URL: {result['video_url']}")
    print(f"Duration: {result['duration']:.1f}s")


async def example_2_with_text():
    """Video generation with HeyGen TTS"""
    service = HeyGenService()
    
    result = await service.create_video_with_text(
        face_image_path="/tmp/customer_face.jpg",
        script="Welcome to DESIFaces! Your personalized video is ready.",
        idempotency_key="order_12346",
        voice_id=None  # Auto-selects valid voice
    )
    
    print(f"Video URL: {result['video_url']}")


async def example_3_check_status():
    """Check status of existing video"""
    service = HeyGenService()
    
    status = await service.get_video_status("video_id_here")
    
    print(f"Status: {status['status']}")
    if status['video_url']:
        print(f"Video ready: {status['video_url']}")


async def example_4_list_voices():
    """List available voices"""
    service = HeyGenService()
    
    voices = await service.get_available_voices()
    
    print(f"Found {len(voices)} voices:")
    for voice in voices[:5]:
        print(f"  • {voice['name']} ({voice['language']})")
        print(f"    ID: {voice['voice_id']}")


async def example_5_custom_dimensions():
    """Video with custom dimensions"""
    service = HeyGenService()
    
    result = await service.create_video_from_azure_assets(
        face_image_path="/tmp/customer_face.jpg",
        audio_blob_path="heygen-audio/customer_tts.mp3",
        idempotency_key="order_12347",
        dimension={"width": 1080, "height": 1920},  # Portrait
        test_mode=True  # Faster for testing
    )
    
    print(f"Video: {result['video_url']}")


if __name__ == "__main__":
    # Run examples
    asyncio.run(example_1_basic_video())