from enum import Enum

class LongformJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    stitching = "stitching"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"

class SegmentStatus(str, Enum):
    queued = "queued"
    tts_pending = "tts_pending"
    video_pending = "video_pending"
    succeeded = "succeeded"
    failed = "failed"