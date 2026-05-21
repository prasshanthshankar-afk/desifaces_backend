create or replace view public.v_dashboard_home as
 SELECT user_id,
    updated_at,
    gauges_json,
    alerts_json,
    face_carousel_json,
    video_carousel_json,
    header_json
   FROM dashboard_home_cache;;

