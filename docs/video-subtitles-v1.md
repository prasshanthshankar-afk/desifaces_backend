# desifaces Video Subtitles v1

## Product contract

Subtitles are a sidecar media track, not permanently burned into the generated MP4.

Creators can choose **Include toggleable subtitles (CC)** before Video generation. When enabled and the selected saved Voice has an approved script, the final Video receives a WebVTT track. Viewers can turn captions on or off in the desifaces player.

If a historical Voice has no persisted script metadata, Video generation remains available but subtitle generation is disabled. Caption-sidecar failure must not invalidate an already completed/paid Video.

## Runtime flow

```
Approved Voice script + locale
        |
Video Studio subtitle toggle
        |
Longform create request
  require_subtitles
  tags.subtitle_language
        |
Fusion Extension parent job
        |
provider-safe child segments
        |
final stitch (concat / xfade)
        |
+----------------------+---------------------+
|                                            |
final MP4                                   WebVTT
video-output/<id>.mp4                       video-output/<id>.mp4.vtt
|                                            |
+----------------------+---------------------+
                       |
LongformJobView
  final_video_url
  subtitle_track_url
  subtitle_language
                       |
CaptionedVideo
  native <track>
  explicit CC On / Off
```

## Timing model

v1 uses the approved script and final segment lineage rather than retranscribing the finished audio.

- Each final segment contributes its actual/recorded segment duration.
- Concatenated videos use cumulative segment boundaries.
- Crossfaded videos subtract the transition overlap from the timeline.
- Long text is split into readable cues (sentence boundary first, then short word groups).
- Cue time within a segment is allocated proportionally by word count.
- If speaker metadata is present, the cue is rendered as `Speaker: text`.

A later precision enhancement can consume TTS word-boundary timestamps without changing the public subtitle contract.

## Storage and API

No database schema migration is required in v1.

The longform job tags persist:

- `require_subtitles`
- `subtitle_language`
- `subtitle_storage_path`
- `subtitles_enabled`
- optional `subtitle_error`

The job read API renews the signed WebVTT URL from the durable storage path so clients do not depend on an expired SAS URL.

## Failure behavior

Video is authoritative.

If MP4 generation succeeds but subtitle creation/upload fails:

- the Video remains successful;
- `subtitles_enabled=false`;
- a bounded `subtitle_error` is recorded for operational diagnosis;
- the user can still watch/download/share the Video.

## Compatibility

- Existing longform jobs without subtitle metadata behave exactly as before.
- Existing saved Voice assets without script metadata remain usable.
- Pricing and credit accounting are unchanged by v1.
- No provider selection or core Fusion contract is changed.
- Video sharing continues to share the MP4; toggleable captions apply within subtitle-aware players.

## Multi-person / Group Photo contract

The same sidecar model should be used for multi-person and group-photo conversations, but the Director final-stitch runtime must be updated at its source of truth before this is enabled.

Required Director contract:

```json
{
  "require_subtitles": true,
  "subtitle_language": "en-US",
  "show_speaker_names": true
}
```

The final-stitch response should expose:

```json
{
  "video_url": "...",
  "subtitle_track_url": "...",
  "subtitle_language": "en-US"
}
```

Cues should come from the approved conversation turns and preserve speaker names. No frontend-only approximation should be shipped if the Director runtime does not return a durable subtitle track.

## Certification gates

1. Subtitle ON + script metadata creates a non-empty WebVTT track.
2. Subtitle OFF creates no track and does not change Video output/pricing.
3. Historical Voice without script still creates Video with subtitles unavailable.
4. WebVTT duration remains within final stitched Video duration.
5. Concat and xfade timing are both covered.
6. Non-Latin UTF-8 text renders without corruption.
7. Player CC toggles track mode on/off without reloading the Video.
8. Expired subtitle SAS URLs are renewed on job read.
9. Caption-sidecar failure does not turn a successful Video into a failed paid job.
10. No credit, plan, provider, or generation-contract regression.
