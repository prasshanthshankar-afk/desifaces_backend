BEGIN;

-- 120 "premiere" presets: 40 story_arc, 20 stage, 20 lighting, 20 shot, 10 typography, 10 edit
-- Idempotent via fixed UUIDs + ON CONFLICT UPDATE.
INSERT INTO public.music_style_presets(
  id, name, tags, preset_type, language_hint, content, text_for_embedding
) VALUES

-- ============================================================
-- STORY ARCS (1..40)
-- ============================================================
('00000000-0000-0000-0000-000000000001','Coastal Calm Montage',ARRAY['no_face','broll','ocean','cinematic','calm'],'story_arc','en',
'{"visual_style":"broll_montage","motifs":["ocean_waves","sunrise_horizon","fishing_boats","foam_macro","salt_air"],"time_of_day":["dawn","golden_hour"],"camera":{"language":"wide_slow","shots":["drone_wide","shore_macro","boat_silhouette"]},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade","matchcut"]},"palette":"teal_gold"}'::jsonb,
'No-face cinematic ocean montage: waves, sunrise horizon, fishing boats, macro foam, teal-gold grade, wide drone, slow pacing, crossfades and match cuts.'),

('00000000-0000-0000-0000-000000000002','Monsoon Journey (Rain + Roads)',ARRAY['no_face','broll','monsoon','journey','nostalgia'],'story_arc','en',
'{"visual_style":"journey_montage","motifs":["rain_on_windshield","wet_roads","train_window","tea_stall","street_reflections"],"time_of_day":["overcast","evening"],"camera":{"language":"pov_handheld","shots":["pov_drive","train_window","reflection_close"]},"edit":{"pace":"medium","cut_density":"medium","transitions":["dissolve","whip_pan_light"]},"palette":"steel_blue_teal"}'::jsonb,
'No-face monsoon journey: rain windshield, wet roads, train window, tea stalls, reflections; POV handheld; dissolves and light whip pans.'),

('00000000-0000-0000-0000-000000000003','Rural Harvest Warmth',ARRAY['no_face','broll','rural','fields','warm','earthy'],'story_arc','en',
'{"visual_style":"documentary_broll","motifs":["paddy_fields","hands_working","bullock_cart","dust_sunrays","kids_playing"],"time_of_day":["morning","golden_hour"],"camera":{"language":"docu_steady","shots":["wide_fields","hands_close","walking_feet"]},"edit":{"pace":"slow","cut_density":"low","transitions":["straight_cut","dip_to_white"]},"palette":"warm_earth"}'::jsonb,
'No-face rural harvest: paddy fields, hands working, bullock cart, dusty sunrays; warm earthy grade; steady documentary pacing.'),

('00000000-0000-0000-0000-000000000004','Urban Neon Hustle Peak',ARRAY['no_face','broll','city','neon','high_energy','chorus_peak'],'story_arc','en',
'{"visual_style":"city_montage","motifs":["crowded_streets","metro_timelapse","neon_signs","rain_reflections","street_food"],"time_of_day":["night"],"camera":{"language":"dynamic_handheld","shots":["timelapse","market_handheld","skyline_wide"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","whip_pan","glitch_light"]},"palette":"magenta_cyan_blue"}'::jsonb,
'High-energy no-face city montage: metro timelapse, neon rain, crowds, street food; fast cut density; whip pans and glitch accents.'),

('00000000-0000-0000-0000-000000000005','Desert Royal Heritage',ARRAY['no_face','broll','desert','royal','heritage','cinematic'],'story_arc','en',
'{"visual_style":"heritage_cinema","motifs":["palace_arches","sand_dunes","fort_walls","textile_macro","camel_silhouette"],"time_of_day":["sunset","blue_hour"],"camera":{"language":"cinematic_wide","shots":["symmetry_wide","dune_wide","macro_textile"]},"edit":{"pace":"medium","cut_density":"medium","transitions":["matchcut_shape","fade_black"]},"palette":"amber_blue"}'::jsonb,
'No-face desert royal: palaces, dunes, forts, textiles; amber-blue grade; symmetry wides; shape match cuts.'),

('00000000-0000-0000-0000-000000000006','Festival Burst (Colors + Fireworks)',ARRAY['no_face','broll','festival','celebration','colors','high_energy'],'story_arc','en',
'{"visual_style":"festival_montage","motifs":["color_powder","drums_crowd","lanterns","fireworks","dancing_feet"],"time_of_day":["day","night"],"camera":{"language":"wide_inserts","shots":["crowd_wide","hands_throw_color","fireworks_sky"]},"edit":{"pace":"fast","cut_density":"high","transitions":["flash_cut","hard_cut","strobe_sync"]},"palette":"festival_multicolor"}'::jsonb,
'No-face festival montage: color powder, drums, lanterns, fireworks; fast edits; flash cuts and strobe sync on chorus.'),

('00000000-0000-0000-0000-000000000007','Night Drive Noir',ARRAY['no_face','broll','night_drive','noir','moody','city'],'story_arc','en',
'{"visual_style":"noir_drive","motifs":["dashboard_glow","tunnel_lights","headlight_bokeh","mirror_reflections","rain_glass"],"time_of_day":["night"],"camera":{"language":"pov_windshield","shots":["pov_drive","bokeh_close","mirror_reflection"]},"edit":{"pace":"medium","cut_density":"medium","transitions":["dissolve","light_leak"]},"palette":"blue_magenta"}'::jsonb,
'No-face night drive noir: dashboard glow, tunnels, bokeh, reflections, rain glass; dissolves and light leaks; blue-magenta palette.'),

('00000000-0000-0000-0000-000000000008','Temple Devotional Serenity',ARRAY['no_face','broll','temple','devotional','calm','spiritual'],'story_arc','en',
'{"visual_style":"devotional_broll","motifs":["bells_close","incense_smoke","lamp_flames","architecture_symmetry","prayer_hands"],"time_of_day":["dawn","evening"],"camera":{"language":"steady_cinematic","shots":["symmetry_wide","macro_flame","smoke_slow"]},"edit":{"pace":"slow","cut_density":"low","transitions":["fade_black","matchcut_shape"]},"palette":"warm_gold"}'::jsonb,
'Devotional no-face montage: bells, incense, lamp flames, symmetry architecture; warm gold; slow edits and fades.'),

('00000000-0000-0000-0000-000000000009','Himalayan Serenity',ARRAY['no_face','broll','mountains','serene','cinematic'],'story_arc','en',
'{"visual_style":"nature_cinema","motifs":["snow_peaks","prayer_flags","pine_fog","river_flow","stone_paths"],"time_of_day":["morning","blue_hour"],"camera":{"language":"wide_slow","shots":["drone_wide","fog_slow","river_macro"]},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade","matchcut"]},"palette":"cool_teal"}'::jsonb,
'No-face Himalayan nature cinema: snow peaks, prayer flags, fog, river flow; cool teal grade; slow wide shots and match cuts.'),

('00000000-0000-0000-0000-000000000010','Goa Fishing Life (B-roll Story)',ARRAY['no_face','broll','goa','fishing','community','coastal'],'story_arc','en',
'{"visual_style":"coastal_story","motifs":["nets_throwing","boats_returning","market_fish","waves_close","sunset_docks"],"time_of_day":["morning","sunset"],"camera":{"language":"docu_cinematic","shots":["dock_wide","hands_nets","market_inserts"]},"edit":{"pace":"medium","cut_density":"medium","transitions":["straight_cut","crossfade"]},"palette":"teal_warm"}'::jsonb,
'No-face Goa fishing story: nets, boats, fish market, waves; docu-cinematic; medium pacing with crossfades.'),

('00000000-0000-0000-0000-000000000011','Train to the City (Contrast Arc)',ARRAY['no_face','broll','journey','train','contrast','city'],'story_arc','en',
'{"visual_style":"contrast_montage","motifs":["village_morning","train_window","station_crowds","city_skyline","night_neon"],"time_of_day":["morning","night"],"camera":{"language":"pov_to_wide","shots":["train_window","station_handheld","skyline_wide"]},"edit":{"pace":"build","cut_density":"rises_to_chorus","transitions":["matchcut","hard_cut_on_chorus"]},"palette":"warm_to_neon"}'::jsonb,
'No-face contrast arc: village morning → train window → city neon; cut density rises into chorus; match cuts and chorus hard cuts.'),

('00000000-0000-0000-0000-000000000012','Wedding Sangeet Glam',ARRAY['broll','wedding','sangeet','celebration','glam'],'story_arc','en',
'{"visual_style":"wedding_montage","motifs":["mehndi_hands","garlands","bokeh_lights","dance_feet","stage_led"],"time_of_day":["night"],"camera":{"language":"glam_slowmo","shots":["slowmo_dance","detail_hands","wide_stage"]},"edit":{"pace":"fast_on_chorus","cut_density":"high","transitions":["flash_cut","whip_pan"]},"palette":"gold_pink"}'::jsonb,
'Wedding sangeet montage: mehndi hands, garlands, glam bokeh, dance feet; gold-pink palette; chorus fast edits with flash cuts.'),

('00000000-0000-0000-0000-000000000013','Street Food Night Market',ARRAY['no_face','broll','city','street_food','texture'],'story_arc','en',
'{"visual_style":"street_food_docu","motifs":["tandoor_fire","steam_macro","hands_serving","spice_colors","crowd_flow"],"time_of_day":["night"],"camera":{"language":"handheld_inserts","shots":["macro_fire","hands_close","wide_lane"]},"edit":{"pace":"medium_fast","cut_density":"medium_high","transitions":["hard_cut","matchcut_on_color"]},"palette":"warm_neon"}'::jsonb,
'No-face street food docu: tandoor fire, steam, hands serving, spice colors, crowd flow; handheld inserts; match cuts on color.'),

('00000000-0000-0000-0000-000000000014','Tech City Future (Glass + Lines)',ARRAY['no_face','broll','future','tech','sleek'],'story_arc','en',
'{"visual_style":"sleek_future","motifs":["glass_buildings","light_trails","screens_reflection","metro_lines","geometry"],"time_of_day":["night"],"camera":{"language":"smooth_gimbal","shots":["wide_glass","light_trails","abstract_lines"]},"edit":{"pace":"fast","cut_density":"high","transitions":["glitch","hard_cut"]},"palette":"cyan_blue"}'::jsonb,
'No-face sleek future city: glass buildings, light trails, reflections; smooth gimbal; fast pacing; cyan-blue palette with glitch cuts.'),

('00000000-0000-0000-0000-000000000015','Countryside Romance (Soft Pastels)',ARRAY['no_face','broll','countryside','romance','soft'],'story_arc','en',
'{"visual_style":"soft_cinema","motifs":["fields_wind","flowers_macro","sun_flare","riverbank","handwritten_notes"],"time_of_day":["golden_hour"],"camera":{"language":"slow_cinematic","shots":["wide_fields","macro_flowers","flare_close"]},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade","dissolve"]},"palette":"pastel_warm"}'::jsonb,
'No-face soft countryside romance: wind fields, flower macros, sun flares; pastel warm grade; slow pacing with dissolves.'),

('00000000-0000-0000-0000-000000000016','Underground Rap Grit',ARRAY['no_face','broll','rap','gritty','urban'],'story_arc','en',
'{"visual_style":"grit_docu","motifs":["alley_graffiti","shoes_walking","train_tracks","hands_gestures","crowd_edges"],"time_of_day":["night"],"camera":{"language":"handheld_raw","shots":["low_angle_feet","graffiti_wide","fast_inserts"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","whip_pan"]},"palette":"contrast_grit"}'::jsonb,
'No-face gritty rap montage: alleys, graffiti, feet, tracks; raw handheld; high cut density; hard cuts and whip pans.'),

('00000000-0000-0000-0000-000000000017','Beach Night Bonfire',ARRAY['no_face','broll','beach','bonfire','night'],'story_arc','en',
'{"visual_style":"night_broll","motifs":["bonfire_flames","sparks","waves_dark","string_lights","silhouette_optional"],"time_of_day":["night"],"camera":{"language":"macro_to_wide","shots":["flame_macro","wide_fire","ocean_night"]},"edit":{"pace":"medium","cut_density":"medium","transitions":["crossfade","fade_black_short"]},"palette":"warm_on_black"}'::jsonb,
'No-face beach bonfire: flame macros, sparks, dark waves; warm-on-black grade; medium pacing; fades and crossfades.'),

('00000000-0000-0000-0000-000000000018','Sports Training Montage',ARRAY['no_face','broll','sports','training','hype'],'story_arc','en',
'{"visual_style":"training_montage","motifs":["running_feet","sweat_macro","rope_jump","stadium_steps","breath_fog"],"time_of_day":["dawn"],"camera":{"language":"kinetic_handheld","shots":["low_angle_feet","tight_face_off","wide_track"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","speed_ramp_light"]},"palette":"high_contrast"}'::jsonb,
'No-face training montage: feet, sweat macros, rope jumps, stadium steps; kinetic handheld; high cut density; light speed ramps.'),

('00000000-0000-0000-0000-000000000019','Nature Macro Poetry',ARRAY['no_face','broll','nature','macro','calm'],'story_arc','en',
'{"visual_style":"macro_poetry","motifs":["water_droplets","leaf_texture","ripples","dust_in_light","birds_silhouette"],"time_of_day":["morning"],"camera":{"language":"macro_slow","shots":["macro_leaf","macro_drop","wide_silhouette"]},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade"]},"palette":"soft_green"}'::jsonb,
'No-face nature macro poetry: droplets, leaves, ripples, dust light; macro slow camera; soft green palette; crossfades.'),

('00000000-0000-0000-0000-000000000020','Road Trip Freedom (Sun + Wind)',ARRAY['no_face','broll','roadtrip','freedom','travel'],'story_arc','en',
'{"visual_style":"roadtrip_montage","motifs":["highway_lines","sun_flare","open_sky","roadside_stops","laughing_offscreen"],"time_of_day":["day"],"camera":{"language":"pov_gimbal","shots":["pov_road","wide_sky","roadside_inserts"]},"edit":{"pace":"medium_fast","cut_density":"medium","transitions":["matchcut","whip_pan_light"]},"palette":"bright_warm"}'::jsonb,
'No-face roadtrip freedom: highways, sun flares, open sky, roadside stops; POV gimbal; match cuts and light whip pans.'),

('00000000-0000-0000-0000-000000000021','Coffee Shop Indie Cozy',ARRAY['no_face','broll','indie','cozy','cafe'],'story_arc','en',
'{"visual_style":"cozy_docu","motifs":["coffee_pour","notebook_pages","window_rain","warm_lamps","street_outside"],"time_of_day":["evening"],"camera":{"language":"steady_inserts","shots":["coffee_macro","hands_write","window_rain"]},"edit":{"pace":"slow","cut_density":"low","transitions":["dissolve","crossfade"]},"palette":"warm_amber"}'::jsonb,
'No-face cozy indie: coffee pour, notebooks, rainy window, warm lamps; steady inserts; slow edits; warm amber palette.'),

('00000000-0000-0000-0000-000000000022','Beach Day Pop Bright',ARRAY['no_face','broll','beach','pop','bright'],'story_arc','en',
'{"visual_style":"bright_pop","motifs":["sunny_waves","friends_running","sand_kicks","ice_drinks","kite_sky"],"time_of_day":["day"],"camera":{"language":"wide_fast","shots":["wide_beach","slowmo_sand","sky_kite"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","flash_cut"]},"palette":"bright_cyan_yellow"}'::jsonb,
'No-face bright beach pop: sunny waves, sand kicks, kites; fast edits with flash cuts; bright cyan-yellow palette.'),

('00000000-0000-0000-0000-000000000023','River Ghat Dawn Ritual',ARRAY['no_face','broll','river','dawn','ritual'],'story_arc','en',
'{"visual_style":"ritual_broll","motifs":["river_mist","lamp_floats","steps_stone","chants_offscreen","hands_water"],"time_of_day":["dawn"],"camera":{"language":"steady_cinematic","shots":["mist_wide","lamp_macro","steps_symmetry"]},"edit":{"pace":"slow","cut_density":"low","transitions":["fade_black","crossfade"]},"palette":"soft_gold_blue"}'::jsonb,
'No-face dawn ritual: river mist, lamp floats, stone steps; steady cinematic; slow pacing; soft gold-blue grade.'),

('00000000-0000-0000-0000-000000000024','Startup Grind Montage',ARRAY['no_face','broll','startup','work','modern'],'story_arc','en',
'{"visual_style":"modern_docu","motifs":["laptop_keys","whiteboard_markers","late_night_screens","coffee_cups","city_commute"],"time_of_day":["day","night"],"camera":{"language":"clean_gimbal","shots":["desk_macro","whiteboard_inserts","commute_timelapse"]},"edit":{"pace":"medium_fast","cut_density":"medium_high","transitions":["hard_cut","matchcut_on_shape"]},"palette":"clean_neutral"}'::jsonb,
'No-face startup grind: laptop keys, whiteboards, late night screens, commute timelapse; clean gimbal; medium-high cuts; neutral palette.'),

('00000000-0000-0000-0000-000000000025','Bollywood Masala Montage',ARRAY['no_face','broll','bollywood','dramatic','color'],'story_arc','en',
'{"visual_style":"masala_montage","motifs":["dramatic_sky","fabric_swirl","dance_feet","street_lights","smoke_haze"],"time_of_day":["night"],"camera":{"language":"cinematic_dynamic","shots":["wide_stage_like","fabric_macro","crowd_energy"]},"edit":{"pace":"fast","cut_density":"high","transitions":["flash_cut","whip_pan","strobe_sync_light"]},"palette":"gold_pink_blue"}'::jsonb,
'No-face Bollywood masala: fabric swirls, dance feet, smoke haze, dramatic sky; fast cinematic cuts; gold-pink-blue palette.'),

('00000000-0000-0000-0000-000000000026','Minimal Lyric Video (Clean)',ARRAY['no_face','lyric_video','minimal','clean'],'story_arc','en',
'{"visual_style":"lyric_video","motifs":["subtle_gradient","grain_light","simple_icons","line_breaks","breath_spaces"],"camera":{"language":"static","shots":["type_only"]},"edit":{"pace":"calm","cut_density":"low","transitions":["fade"]},"palette":"mono_soft"}'::jsonb,
'No-face minimal lyric video: subtle gradients, clean type, light grain, calm pacing, simple fades.'),

('00000000-0000-0000-0000-000000000027','Kinetic Lyric Video (Bold Punch)',ARRAY['no_face','lyric_video','kinetic','bold','chorus_peak'],'story_arc','en',
'{"visual_style":"kinetic_typography","motifs":["big_words","punch_in","word_splits","hit_emphasis","color_pops"],"camera":{"language":"type_motion","shots":["type_cards"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","glitch_light"]},"palette":"neon_pops"}'::jsonb,
'No-face kinetic lyric video: bold words, punch-ins, splits on hits, neon pops, fast edits with glitch accents.'),

('00000000-0000-0000-0000-000000000028','Abstract Visualizer (Particles)',ARRAY['no_face','abstract','visualizer','particles','premium'],'story_arc','en',
'{"visual_style":"abstract_visualizer","motifs":["particles","fluid_waves","neon_lines","geometry_pulses","bloom_peaks"],"sync":{"kick":"pulse","chorus":"bloom_peaks"},"edit":{"pace":"medium","cut_density":"medium"},"palette":"neon_teal_magenta"}'::jsonb,
'Abstract no-face visualizer: particles, fluid waves, neon lines, geometry pulses synced to kick and chorus bloom peaks.'),

('00000000-0000-0000-0000-000000000029','Heritage City Morning',ARRAY['no_face','broll','heritage','city','morning'],'story_arc','en',
'{"visual_style":"heritage_docu","motifs":["old_lanes","temple_bells","cycle_rickshaw","chai_steam","arches"],"time_of_day":["morning"],"camera":{"language":"docu_cinematic","shots":["lane_wide","steam_macro","arch_symmetry"]},"edit":{"pace":"medium","cut_density":"medium"},"palette":"warm_stone"}'::jsonb,
'No-face heritage city morning: old lanes, cycle rickshaw, chai steam, arches; warm stone grade; docu-cinematic pacing.'),

('00000000-0000-0000-0000-000000000030','Seaside EDM Sunrise',ARRAY['no_face','broll','edm','sunrise','energy'],'story_arc','en',
'{"visual_style":"edm_broll","motifs":["sunrise_flare","crowd_silhouette_optional","waves_fast","neon_overlay","spark_particles"],"time_of_day":["sunrise"],"camera":{"language":"dynamic_wide","shots":["drone_sweep","flare_close","wave_macro"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","flash_cut"]},"palette":"neon_sunrise"}'::jsonb,
'No-face seaside EDM sunrise: fast wave cuts, neon overlays, sunrise flares, particles; high cut density with flash cuts.'),

('00000000-0000-0000-0000-000000000031','Forest Mist Mystery',ARRAY['no_face','broll','forest','mist','mystery'],'story_arc','en',
'{"visual_style":"mystery_cinema","motifs":["mist_trees","footsteps","shadows","owl_silhouette","wet_leaves"],"time_of_day":["dawn","night"],"camera":{"language":"slow_steady","shots":["tree_wide","leaf_macro","shadow_walk"]},"edit":{"pace":"slow","cut_density":"low","transitions":["dissolve","fade_black"]},"palette":"deep_green"}'::jsonb,
'No-face forest mystery: misty trees, wet leaves, shadows; slow steady camera; dissolves and fade-to-black; deep green palette.'),

('00000000-0000-0000-0000-000000000032','Snow City Night Calm',ARRAY['no_face','broll','snow','city','calm'],'story_arc','en',
'{"visual_style":"winter_noir","motifs":["snow_fall","street_lamps","quiet_roads","breath_fog","window_warm"],"time_of_day":["night"],"camera":{"language":"steady_wide","shots":["lamp_wide","snow_macro","window_close"]},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade"]},"palette":"cool_warm_mix"}'::jsonb,
'No-face winter calm: snowfall, lamps, quiet roads, warm windows; steady wides; slow cuts; cool-warm mixed palette.'),

('00000000-0000-0000-0000-000000000033','Ocean Storm Build (Dramatic)',ARRAY['no_face','broll','ocean','storm','dramatic'],'story_arc','en',
'{"visual_style":"storm_cinema","motifs":["dark_waves","cloud_timelapse","spray_hits","rocks","lightning_far"],"time_of_day":["overcast"],"camera":{"language":"wide_power","shots":["wave_crash","cloud_timelapse","rock_wide"]},"edit":{"pace":"build_to_chorus","cut_density":"rises","transitions":["hard_cut","matchcut_on_motion"]},"palette":"steel_gray"}'::jsonb,
'No-face ocean storm: dark waves, cloud timelapse, spray; builds into chorus; rising cut density; steel-gray palette.'),

('00000000-0000-0000-0000-000000000034','Sunset Skate Street',ARRAY['no_face','broll','skate','street','sunset'],'story_arc','en',
'{"visual_style":"street_sport","motifs":["wheels_close","sun_flare","city_edges","friends_group","graffiti"],"time_of_day":["sunset"],"camera":{"language":"low_angle","shots":["wheel_macro","wide_skate","handheld_follow"]},"edit":{"pace":"fast","cut_density":"high","transitions":["hard_cut","whip_pan_light"]},"palette":"sunset_orange"}'::jsonb,
'No-face skate street: low-angle wheels, sunset flares, graffiti; fast edits; whip pans; sunset orange palette.'),

('00000000-0000-0000-0000-000000000035','Meditation Breath Visuals',ARRAY['no_face','abstract','calm','meditation'],'story_arc','en',
'{"visual_style":"calm_abstract","motifs":["slow_particles","soft_gradients","water_ripples","light_bloom","silence_spaces"],"sync":{"breath":"slow_pulse"},"edit":{"pace":"slow","cut_density":"low","transitions":["crossfade"]},"palette":"soft_blue"}'::jsonb,
'No-face meditation visuals: slow particles, soft gradients, ripples, gentle bloom; breath-like slow pulse; crossfades; soft blue palette.'),

('00000000-0000-0000-0000-000000000036','Patriotic India Montage (Landscapes)',ARRAY['no_face','broll','india','patriotic','montage'],'story_arc','en',
'{"visual_style":"national_montage","motifs":["flag_colors_abstract","mountains","rivers","cities","villages"],"camera":{"language":"wide_proud","shots":["drone_wide","city_wide","river_wide"]},"edit":{"pace":"build_to_final_chorus","cut_density":"rises"},"palette":"saffron_white_green"}'::jsonb,
'No-face patriotic montage: India landscapes across mountains, rivers, cities, villages; proud wides; builds to final chorus; tricolor palette.'),

('00000000-0000-0000-0000-000000000037','Deep House Minimal City',ARRAY['no_face','broll','deep_house','minimal','city'],'story_arc','en',
'{"visual_style":"minimal_city","motifs":["architecture_lines","night_walk","reflections","slow_timelapse","empty_streets"],"camera":{"language":"smooth_gimbal","shots":["line_symmetry","reflection_close","slow_timelapse"]},"edit":{"pace":"medium","cut_density":"low_medium","transitions":["dissolve"]},"palette":"mono_cool"}'::jsonb,
'No-face deep house minimal city: lines, reflections, slow timelapse, empty streets; smooth gimbal; low-medium cuts; cool mono palette.'),

('00000000-0000-0000-0000-000000000038','Afro Beats Party Street',ARRAY['no_face','broll','party','street','high_energy'],'story_arc','en',
'{"visual_style":"party_street","motifs":["street_dance_feet","lights_bokeh","crowd_hands","smoke_puffs","confetti"],"camera":{"language":"handheld_party","shots":["dance_feet","crowd_wide","bokeh_close"]},"edit":{"pace":"fast","cut_density":"high","transitions":["flash_cut","hard_cut"]},"palette":"vibrant"}'::jsonb,
'No-face party street: dance feet, bokeh lights, crowd hands, smoke puffs, confetti; fast cuts; vibrant palette.'),

('00000000-0000-0000-0000-000000000039','Lo-fi Study Night',ARRAY['no_face','broll','lofi','study','cozy'],'story_arc','en',
'{"visual_style":"lofi_room","motifs":["desk_lamp","notebook","rain_window","city_noise","coffee_steam"],"camera":{"language":"static_inserts","shots":["lamp_close","writing_close","rain_window"]},"edit":{"pace":"slow","cut_density":"low"},"palette":"warm_fade"}'::jsonb,
'No-face lo-fi study night: desk lamp, notebooks, rain window, coffee steam; static inserts; low cut density; warm faded grade.'),

('00000000-0000-0000-0000-000000000040','Epic Trailer Montage (No Faces)',ARRAY['no_face','broll','epic','trailer','cinematic'],'story_arc','en',
'{"visual_style":"epic_trailer","motifs":["storm_clouds","fast_landscapes","fire_sparks","city_wide","dramatic_light"],"camera":{"language":"wide_power","shots":["drone_wide","timelapse","macro_sparks"]},"edit":{"pace":"build_peak_release","cut_density":"high_on_chorus"},"palette":"high_contrast"}'::jsonb,
'Epic no-face trailer montage: storms, landscapes, sparks, city wides; build→peak→release; high cut density on chorus; high contrast.'),

-- ============================================================
-- STAGE PRESETS (41..60)
-- ============================================================
('00000000-0000-0000-0000-000000000041','Stadium Concert Stage',ARRAY['stage','concert','premiere','led_wall','crowd'],'stage','en',
'{"venue":"stadium","set_pieces":["mega_led_wall","pyro_sparks","crowd_lightsticks","riser_platforms"],"blocking":"hero_center_band_wings","camera_defaults":["crane_wide","tele_close","crowd_sweep"],"vfx":["haze_beams","spark_bursts"]}'::jsonb,
'Premiere stadium stage: mega LED wall, pyro sparks, crowd lightsticks, risers; crane wides, tele closeups, sweeping crowd shots.'),

('00000000-0000-0000-0000-000000000042','EDM Club Stage (Neon + Lasers)',ARRAY['stage','club','edm','neon','lasers'],'stage','en',
'{"venue":"club","set_pieces":["dj_riser","laser_grid","neon_tubes","fog_bursts"],"blocking":"center_riser_crowd_cutaways","camera_defaults":["handheld_close","overhead_drop","whip_pan"],"vfx":["laser_sweeps","glitch_flares"]}'::jsonb,
'EDM club stage: DJ riser, lasers, neon tubes, fog bursts; handheld close energy; overhead drops; whip pans.'),

('00000000-0000-0000-0000-000000000043','Rooftop Indie Stage',ARRAY['stage','rooftop','indie','minimal','golden_hour'],'stage','en',
'{"venue":"rooftop","set_pieces":["string_lights","rug","city_backdrop","minimal_led"],"blocking":"intimate_circle","camera_defaults":["slow_push","tripod_wide","instrument_details"],"lighting_defaults":{"palette":"warm","intensity":"low"}}'::jsonb,
'Rooftop indie: string lights, minimal set, city backdrop; intimate blocking; slow pushes; instrument detail inserts; warm low light.'),

('00000000-0000-0000-0000-000000000044','Classical Auditorium',ARRAY['stage','classical','auditorium','elegant'],'stage','en',
'{"venue":"auditorium","set_pieces":["wood_panels","soft_spotlights","minimal_backdrop"],"blocking":"center_seated","camera_defaults":["tripod_wide","tele_hands","slow_pan"],"vibe":"elegant"}'::jsonb,
'Classical auditorium: wood panels, soft spotlights; elegant center blocking; tripod wides and tele hand closeups.'),

('00000000-0000-0000-0000-000000000045','Street Performance Circle',ARRAY['stage','street','raw','handheld'],'stage','en',
'{"venue":"street","set_pieces":["portable_speakers","street_lamps","crowd_ring"],"blocking":"center_circle","camera_defaults":["handheld_close","crowd_orbit","low_angle_steps"],"vibe":"raw_authentic"}'::jsonb,
'Street stage: raw authentic circle; handheld close; crowd orbit; low-angle steps; street lamps and portable speakers.'),

('00000000-0000-0000-0000-000000000046','Temple Courtyard Stage',ARRAY['stage','courtyard','devotional','calm'],'stage','en',
'{"venue":"courtyard","set_pieces":["oil_lamps","stone_arches","flower_strings","incense_haze"],"blocking":"center_calm","camera_defaults":["symmetry_wide","macro_flame","slow_push"],"vibe":"serene"}'::jsonb,
'Temple courtyard: stone arches, oil lamps, flowers; serene blocking; symmetry wides; flame macros; slow pushes.'),

('00000000-0000-0000-0000-000000000047','Beach Bonfire Jam',ARRAY['stage','beach','bonfire','night','cozy'],'stage','en',
'{"venue":"beach","set_pieces":["bonfire","string_lights","simple_percussion"],"blocking":"circle_around_fire","camera_defaults":["flame_macro","wide_fire","silhouette_back"],"vibe":"intimate"}'::jsonb,
'Beach bonfire jam: circle around fire, string lights; flame macros + wide fire; intimate vibe.'),

('00000000-0000-0000-0000-000000000048','Warehouse Industrial',ARRAY['stage','industrial','warehouse','grit'],'stage','en',
'{"venue":"warehouse","set_pieces":["metal_frames","smoke_haze","single_led_bar"],"blocking":"strong_center","camera_defaults":["handheld_close","low_angle","hard_cut_wides"],"vibe":"industrial_grit"}'::jsonb,
'Industrial warehouse stage: metal frames, haze, LED bars; strong center; low angles; gritty handheld close.'),

('00000000-0000-0000-0000-000000000049','Luxury Ballroom',ARRAY['stage','ballroom','glam','wedding'],'stage','en',
'{"venue":"ballroom","set_pieces":["chandeliers","gold_drapes","mirror_floor","led_panels"],"blocking":"grand_center","camera_defaults":["crane_wide","slow_orbit","detail_glitter"],"vibe":"glam"}'::jsonb,
'Luxury ballroom: chandeliers, gold drapes, mirror floor; grand center; crane wides and slow orbits; glam details.'),

('00000000-0000-0000-0000-000000000050','Mountain Ridge Minimal',ARRAY['stage','mountain','minimal','cinematic'],'stage','en',
'{"venue":"mountain_ridge","set_pieces":["wide_sky","wind_flags","minimal_riser"],"blocking":"solo_center","camera_defaults":["drone_wide","slow_push","profile_silhouette"],"vibe":"epic_minimal"}'::jsonb,
'Mountain ridge stage: epic minimal; drone wides and slow pushes; flags in wind; silhouette profiles optional.'),

('00000000-0000-0000-0000-000000000051','Retro Studio TV Set',ARRAY['stage','retro','tv_set','performance'],'stage','en',
'{"venue":"tv_studio","set_pieces":["retro_panels","practical_lights","props_mics"],"blocking":"front_stage","camera_defaults":["multi_cam_cuts","close_reactions","wide_master"],"vibe":"retro_show"}'::jsonb,
'Retro TV studio set: practical lights, retro panels; multi-cam style; wide master + reaction closeups.'),

('00000000-0000-0000-0000-000000000052','Cyber Grid Stage',ARRAY['stage','cyber','future','led_grid'],'stage','en',
'{"venue":"cyber_stage","set_pieces":["grid_led_floor","hologram_lines","laser_planes"],"blocking":"center_grid","camera_defaults":["smooth_gimbal","overhead_grid","glitch_cuts"],"vibe":"futuristic"}'::jsonb,
'Cyber grid stage: LED floor grid, hologram lines, laser planes; smooth gimbal + overhead grid shots; futuristic vibe.'),

('00000000-0000-0000-0000-000000000053','Small Pub Acoustic',ARRAY['stage','pub','acoustic','intimate'],'stage','en',
'{"venue":"pub","set_pieces":["wood_bar","warm_lamps","small_crowd_tables"],"blocking":"close_stage","camera_defaults":["slow_push","handheld_close","detail_hands"],"vibe":"intimate_acoustic"}'::jsonb,
'Small pub acoustic stage: warm lamps, wood bar, intimate tables; slow push-ins; handheld close; hand detail shots.'),

('00000000-0000-0000-0000-000000000054','Open-Air Festival Ground',ARRAY['stage','festival','open_air','crowd'],'stage','en',
'{"venue":"open_air","set_pieces":["flags","tower_lights","pyro_optional","big_crowd"],"blocking":"wide_hero","camera_defaults":["crane_wide","crowd_sweep","tele_close"],"vibe":"festival_peak"}'::jsonb,
'Open-air festival ground: flags, tower lights, big crowd; crane wides; sweeping crowd; tele close hero.'),

('00000000-0000-0000-0000-000000000055','Desert Campfire Stage',ARRAY['stage','desert','campfire','heritage'],'stage','en',
'{"venue":"desert_camp","set_pieces":["campfire","tents","lanterns","folk_props"],"blocking":"circle_fire","camera_defaults":["fire_macro","wide_tents","instrument_macro"],"vibe":"folk_night"}'::jsonb,
'Desert campfire stage: lanterns, tents, folk props; circle blocking; fire macro + wide tents; folk night vibe.'),

('00000000-0000-0000-0000-000000000056','Riverfront Steps',ARRAY['stage','riverfront','ritual','cinematic'],'stage','en',
'{"venue":"river_steps","set_pieces":["stone_steps","mist","lamps","boats"],"blocking":"symmetry_center","camera_defaults":["symmetry_wide","mist_wide","lamp_macro"],"vibe":"dawn_ritual"}'::jsonb,
'Riverfront steps stage: mist, lamps, stone symmetry; symmetry wides; lamp macros; dawn ritual vibe.'),

('00000000-0000-0000-0000-000000000057','Corporate LED Launch',ARRAY['stage','corporate','clean','led'],'stage','en',
'{"venue":"conference_stage","set_pieces":["clean_led_wall","spotlights","minimal_props"],"blocking":"center_hero","camera_defaults":["clean_wide","tele_close","slide_like_cuts"],"vibe":"polished"}'::jsonb,
'Corporate LED launch stage: clean LED wall, minimal props; polished vibe; clean wide and tele close.'),

('00000000-0000-0000-0000-000000000058','Blackbox Theater',ARRAY['stage','theater','minimal','dramatic'],'stage','en',
'{"venue":"blackbox","set_pieces":["black_backdrop","single_spot","smoke_haze"],"blocking":"single_hero","camera_defaults":["slow_orbit","spot_close","wide_negative_space"],"vibe":"dramatic_minimal"}'::jsonb,
'Blackbox theater: black backdrop, single spot, haze; dramatic minimal; slow orbits; negative-space wides.'),

('00000000-0000-0000-0000-000000000059','Anime Neon Alley Stage',ARRAY['stage','alley','neon','stylized'],'stage','en',
'{"venue":"neon_alley","set_pieces":["signboards","steam_vents","wet_reflections","lights"],"blocking":"alley_walkthrough","camera_defaults":["handheld_follow","sign_macro","wide_alley"],"vibe":"stylized"}'::jsonb,
'Neon alley stage: signboards, steam vents, wet reflections; walkthrough blocking; handheld follow + sign macros.'),

('00000000-0000-0000-0000-000000000060','Garden Lantern Stage',ARRAY['stage','garden','lantern','romance'],'stage','en',
'{"venue":"garden","set_pieces":["lanterns","fairy_lights","flowers"],"blocking":"soft_center","camera_defaults":["slow_push","bokeh_close","wide_garden"],"vibe":"romantic"}'::jsonb,
'Garden lantern stage: fairy lights and flowers; romantic vibe; slow pushes; bokeh closeups; wide garden shots.'),

-- ============================================================
-- LIGHTING PACKS (61..80)
-- ============================================================
('00000000-0000-0000-0000-000000000061','Neon Pulse Lighting',ARRAY['lighting','neon','club','pulse','chorus_peak'],'lighting','en',
'{"palette":["magenta","cyan","deep_blue"],"cues":{"intro":"slow_pulse","verse":"steady","chorus":"fast_pulse_strobe","bridge":"dim_blue"},"rules":{"strobe_max_hz":8,"avoid_full_white":true}}'::jsonb,
'Neon pulse lighting: magenta/cyan/deep blue; chorus fast pulse + strobe <=8hz; intro slow pulse; bridge dim blue.'),

('00000000-0000-0000-0000-000000000062','Golden Hour Soft Bloom',ARRAY['lighting','golden_hour','cinematic','warm'],'lighting','en',
'{"palette":["gold","amber","soft_warm"],"cues":{"intro":"warm_rim","verse":"soft_fill","chorus":"flare_peaks","outro":"fade_warm"},"rules":{"contrast":"soft","bloom":"subtle"}}'::jsonb,
'Golden hour cinematic lighting: warm rim + soft fill; chorus flare peaks; soft contrast; subtle bloom.'),

('00000000-0000-0000-0000-000000000063','Monsoon Blue Mood',ARRAY['lighting','monsoon','blue','moody','rain'],'lighting','en',
'{"palette":["steel_blue","teal","cool_white"],"cues":{"verse":"cool_soft","chorus":"teal_peaks","bridge":"near_mono"},"rules":{"highlights":"controlled","reflections":"boost"}}'::jsonb,
'Monsoon blue mood lighting: steel-blue/teal; controlled highlights; boosted reflections; teal peaks on chorus.'),

('00000000-0000-0000-0000-000000000064','Lantern Warm Flicker',ARRAY['lighting','lantern','warm','festival','calm'],'lighting','en',
'{"palette":["warm_orange","gold","soft_amber"],"cues":{"verse":"lantern_warm","chorus":"warm_peak","bridge":"dim_warm"},"rules":{"flicker":"subtle","avoid_strobe":true}}'::jsonb,
'Lantern warm lighting: soft amber palette, subtle flicker; no strobe; warm chorus peak; dim bridge.'),

('00000000-0000-0000-0000-000000000065','Stadium Beam Sweep',ARRAY['lighting','stadium','beams','premiere'],'lighting','en',
'{"palette":["white","gold","blue"],"cues":{"intro":"beam_sweep_slow","chorus":"beam_sweep_fast","bridge":"single_spot"},"rules":{"beam_count":"high","haze":"on"}}'::jsonb,
'Stadium beams: wide sweeps, haze on; chorus fast sweeps; bridge single-spot moment.'),

('00000000-0000-0000-0000-000000000066','Firelight + Shadow',ARRAY['lighting','firelight','night','folk'],'lighting','en',
'{"palette":["fire_orange","deep_black","warm_brown"],"cues":{"intro":"fire_flicker","verse":"steady_fire","chorus":"spark_peaks"},"rules":{"contrast":"high","faces_optional":false}}'::jsonb,
'Firelight lighting: flicker + shadows; high contrast; spark peaks on chorus; no-face friendly.'),

('00000000-0000-0000-0000-000000000067','Retro TV Practical Lights',ARRAY['lighting','retro','practical','tv'],'lighting','en',
'{"palette":["warm_white","red","cyan"],"cues":{"verse":"practical_warm","chorus":"color_pop","bridge":"dim_practical"},"rules":{"avoid_strobe":true}}'::jsonb,
'Retro practical lights: warm practical in verse; color pop on chorus; dim practical bridge; avoid strobe.'),

('00000000-0000-0000-0000-000000000068','Noir Street Lamp',ARRAY['lighting','noir','streetlamp','moody'],'lighting','en',
'{"palette":["sodium_yellow","deep_blue","black"],"cues":{"verse":"lamp_pool","chorus":"neon_edge","bridge":"deep_shadow"},"rules":{"rim_light":"strong","grain":"light"}}'::jsonb,
'Noir street-lamp pools: sodium yellow + deep blue; strong rim edges; deep shadow bridge; light grain.'),

('00000000-0000-0000-0000-000000000069','Cyber Grid Glow',ARRAY['lighting','cyber','grid','future'],'lighting','en',
'{"palette":["cyan","neon_green","violet"],"cues":{"intro":"grid_low","chorus":"grid_peak","bridge":"violet_dim"},"rules":{"glow":"high","strobe_max_hz":6}}'::jsonb,
'Cyber grid glow: cyan/green/violet; chorus grid peak; high glow; strobe <=6hz.'),

('00000000-0000-0000-0000-000000000070','Soft Pastel Romance',ARRAY['lighting','pastel','romance','soft'],'lighting','en',
'{"palette":["peach","rose","cream"],"cues":{"verse":"soft_fill","chorus":"rose_peak","outro":"fade_pastel"},"rules":{"contrast":"low","bloom":"soft"}}'::jsonb,
'Soft pastel romance lighting: peach/rose/cream; low contrast; soft bloom; rose peak on chorus.'),

('00000000-0000-0000-0000-000000000071','Desert Sunset Amber',ARRAY['lighting','desert','sunset','amber'],'lighting','en',
'{"palette":["amber","burnt_orange","deep_blue"],"cues":{"intro":"amber_wash","chorus":"blue_hour_edge","bridge":"torch_warm"},"rules":{"flare":"controlled"}}'::jsonb,
'Desert sunset amber: amber wash, blue-hour edge for chorus contrast; controlled flares.'),

('00000000-0000-0000-0000-000000000072','Forest Mist Diffuse',ARRAY['lighting','forest','mist','diffuse'],'lighting','en',
'{"palette":["deep_green","gray","cool_white"],"cues":{"verse":"diffuse_soft","chorus":"cool_peak","bridge":"near_mono"},"rules":{"haze":"heavy","highlights":"soft"}}'::jsonb,
'Forest mist diffuse: haze heavy; soft highlights; deep green/gray palette; cool peak on chorus.'),

('00000000-0000-0000-0000-000000000073','Ballroom Crystal Sparkle',ARRAY['lighting','ballroom','sparkle','glam'],'lighting','en',
'{"palette":["gold","champagne","white"],"cues":{"verse":"chandelier_soft","chorus":"sparkle_peak","bridge":"spotlight"},"rules":{"glitter_fx":"subtle"}}'::jsonb,
'Ballroom sparkle: chandelier soft verse; sparkle peak chorus; spotlight bridge; subtle glitter FX.'),

('00000000-0000-0000-0000-000000000074','Festival Strobe Safe',ARRAY['lighting','festival','strobe','safe'],'lighting','en',
'{"palette":["white","pink","blue"],"cues":{"chorus":"strobe_safe_peak","verse":"steady_color"},"rules":{"strobe_max_hz":5,"warn":"true"}}'::jsonb,
'Festival strobe-safe: strobe <=5hz, chorus peak only, steady color in verses.'),

('00000000-0000-0000-0000-000000000075','Underwater Teal Caustics',ARRAY['lighting','underwater','teal','calm'],'lighting','en',
'{"palette":["teal","aqua","soft_white"],"cues":{"verse":"caustic_slow","chorus":"bright_aqua_peak"},"rules":{"motion":"slow","bloom":"subtle"}}'::jsonb,
'Underwater teal caustics: slow caustic motion; aqua peaks on chorus; subtle bloom.'),

('00000000-0000-0000-0000-000000000076','High Contrast Trailer',ARRAY['lighting','trailer','high_contrast','epic'],'lighting','en',
'{"palette":["white","black","red_accent"],"cues":{"intro":"dark_low","chorus":"white_peak","bridge":"red_accent"},"rules":{"contrast":"high","vignette":"medium"}}'::jsonb,
'High-contrast trailer lighting: dark intro, white chorus peak, red accent bridge; vignette medium.'),

('00000000-0000-0000-0000-000000000077','Lo-fi Warm Fade',ARRAY['lighting','lofi','warm','fade'],'lighting','en',
'{"palette":["warm_beige","soft_brown","muted_orange"],"cues":{"all":"steady_warm"},"rules":{"contrast":"low","grain":"medium"}}'::jsonb,
'Lo-fi warm fade lighting: steady warm palette; low contrast; medium grain feel.'),

('00000000-0000-0000-0000-000000000078','Clean Corporate Keylight',ARRAY['lighting','clean','corporate','polished'],'lighting','en',
'{"palette":["neutral_white","soft_blue"],"cues":{"verse":"clean_key","chorus":"slightly_brighter"},"rules":{"avoid_color_cast":true}}'::jsonb,
'Clean corporate lighting: neutral key, soft blue fill, minimal color cast; slightly brighter chorus.'),

('00000000-0000-0000-0000-000000000079','Sunrise Warm-to-Neon Shift',ARRAY['lighting','sunrise','shift','build'],'lighting','en',
'{"palette":["warm_gold","pink","neon_cyan"],"cues":{"intro":"warm_gold","chorus":"neon_cyan_peak","outro":"warm_return"},"rules":{"shift":"gradual"}}'::jsonb,
'Lighting shift pack: warm sunrise intro → neon cyan chorus peak → warm return; gradual transitions.'),

('00000000-0000-0000-0000-000000000080','Minimal Single Spot Drama',ARRAY['lighting','spotlight','minimal','dramatic'],'lighting','en',
'{"palette":["white","black"],"cues":{"bridge":"single_spot","chorus":"double_spot"},"rules":{"negative_space":"high","avoid_strobe":true}}'::jsonb,
'Minimal spotlight drama: single spot for bridge, double spot for chorus, high negative space, avoid strobe.'),

-- ============================================================
-- SHOT RECIPES (81..100)
-- ============================================================
('00000000-0000-0000-0000-000000000081','Drone Establishing Wide',ARRAY['shot','drone','wide','cinematic'],'shot','en',
'{"shot_type":"drone_wide","duration_s":[3,6],"motion":"slow_forward","framing":"rule_of_thirds","use_when":["intro","scene_change"]}'::jsonb,
'Drone establishing wide 3–6s, slow forward motion, thirds framing; best for intros and scene changes.'),

('00000000-0000-0000-0000-000000000082','Macro Texture Insert',ARRAY['shot','macro','texture','premium'],'shot','en',
'{"shot_type":"macro_detail","duration_s":[1,3],"motion":"micro_push","subjects":["hands","fabric","water","fire"],"use_when":["verse","fills","bridge"]}'::jsonb,
'Macro detail 1–3s: hands, fabric, water, fire; micro push; perfect for verse texture and bridge breath.'),

('00000000-0000-0000-0000-000000000083','Handheld Market Inserts',ARRAY['shot','handheld','market','city'],'shot','en',
'{"shot_type":"handheld_inserts","duration_s":[1,3],"motion":"natural_shake","subjects":["steam","signage","hands_serving","crowd_edges"],"use_when":["verse","build"]}'::jsonb,
'Handheld market inserts 1–3s: steam, signage, hands serving; gritty texture for verses/builds.'),

('00000000-0000-0000-0000-000000000084','Train Window POV',ARRAY['shot','journey','train','pov'],'shot','en',
'{"shot_type":"pov_train_window","duration_s":[2,5],"motion":"steady","subjects":["passing_fields","stations","rain_drops"],"use_when":["verse","bridge"]}'::jsonb,
'Train window POV 2–5s: passing fields/stations, rain drops; great for travel verses and bridge.'),

('00000000-0000-0000-0000-000000000085','Skyline Timelapse',ARRAY['shot','timelapse','skyline','city'],'shot','en',
'{"shot_type":"skyline_timelapse","duration_s":[2,6],"motion":"timelapse","subjects":["traffic_trails","clouds"],"use_when":["build","chorus"]}'::jsonb,
'Skyline timelapse 2–6s with traffic trails/clouds; strong build/chorus energy shot.'),

('00000000-0000-0000-0000-000000000086','Water Reflection Close',ARRAY['shot','reflection','water','moody'],'shot','en',
'{"shot_type":"reflection_close","duration_s":[2,4],"motion":"slow_pan","subjects":["rain_puddles","neon_reflections","ripples"],"use_when":["verse","bridge"]}'::jsonb,
'Reflection close 2–4s: rain puddles, neon reflections, ripples; moody verse/bridge coverage.'),

('00000000-0000-0000-0000-000000000087','Crowd Hands Cutaway (No Faces)',ARRAY['shot','crowd','hands','festival'],'shot','en',
'{"shot_type":"crowd_hands","duration_s":[1,2],"motion":"handheld","subjects":["hands_up","lightsticks","confetti"],"use_when":["chorus_peak"]}'::jsonb,
'Crowd hands cutaway 1–2s: hands up, lightsticks, confetti; no-face safe chorus peak cut.'),

('00000000-0000-0000-0000-000000000088','Feet Walk Narrative',ARRAY['shot','feet','journey','story'],'shot','en',
'{"shot_type":"feet_walk","duration_s":[2,4],"motion":"follow_low","subjects":["steps","dust","wet_roads"],"use_when":["verse","transition"]}'::jsonb,
'Feet-walk narrative 2–4s: low follow shot; steps/dust/wet road; perfect for transitions and storytelling.'),

('00000000-0000-0000-0000-000000000089','Fire Sparks Macro',ARRAY['shot','fire','sparks','macro'],'shot','en',
'{"shot_type":"sparks_macro","duration_s":[1,3],"motion":"static","subjects":["sparks","embers","flame"],"use_when":["hit","bridge","drop"]}'::jsonb,
'Fire sparks macro 1–3s: embers/flame; strong on hits, drops, dramatic bridge moments.'),

('00000000-0000-0000-0000-000000000090','Silhouette Wide (No Identity)',ARRAY['shot','silhouette','wide','no_face'],'shot','en',
'{"shot_type":"silhouette_wide","duration_s":[2,5],"motion":"slow_push","subjects":["backlit_figures","shadows"],"use_when":["chorus","outro"]}'::jsonb,
'Silhouette wide 2–5s: backlit figures/shadows; no identity; cinematic chorus/outro shot.'),

('00000000-0000-0000-0000-000000000091','Symmetry Architecture Wide',ARRAY['shot','symmetry','architecture','heritage'],'shot','en',
'{"shot_type":"symmetry_wide","duration_s":[2,6],"motion":"slow_push","subjects":["arches","steps","corridors"],"use_when":["intro","verse"]}'::jsonb,
'Symmetry wide 2–6s: arches/steps/corridors; slow push; premium heritage intro/verse shot.'),

('00000000-0000-0000-0000-000000000092','Rain on Glass Macro',ARRAY['shot','rain','macro','moody'],'shot','en',
'{"shot_type":"rain_glass_macro","duration_s":[2,4],"motion":"static","subjects":["droplets","bokeh_lights"],"use_when":["verse","bridge"]}'::jsonb,
'Rain-on-glass macro 2–4s: droplets + bokeh; moody verse/bridge texture.'),

('00000000-0000-0000-0000-000000000093','Drone Sweep Reveal',ARRAY['shot','drone','reveal','epic'],'shot','en',
'{"shot_type":"drone_reveal","duration_s":[3,7],"motion":"sweep_reveal","subjects":["cliff","stadium","city"],"use_when":["chorus_peak","final_chorus"]}'::jsonb,
'Drone sweep reveal 3–7s: cliff/stadium/city reveal; epic chorus peak / final chorus moment.'),

('00000000-0000-0000-0000-000000000094','Slow Orbit Wide',ARRAY['shot','orbit','cinematic','premium'],'shot','en',
'{"shot_type":"slow_orbit","duration_s":[3,6],"motion":"orbit","subjects":["stage","bonfire","statue"],"use_when":["chorus","bridge"]}'::jsonb,
'Slow orbit 3–6s around stage/bonfire/statue; premium chorus/bridge movement shot.'),

('00000000-0000-0000-0000-000000000095','Light Trail Timelapse',ARRAY['shot','light_trails','timelapse','city'],'shot','en',
'{"shot_type":"light_trails","duration_s":[2,6],"motion":"timelapse","subjects":["traffic","neon"],"use_when":["build","chorus"]}'::jsonb,
'Light-trail timelapse 2–6s: traffic/neon; strong for builds and chorus energy.'),

('00000000-0000-0000-0000-000000000096','Ocean Wave Crash',ARRAY['shot','ocean','waves','dramatic'],'shot','en',
'{"shot_type":"wave_crash","duration_s":[2,4],"motion":"static","subjects":["crash","spray","rocks"],"use_when":["hit","chorus"]}'::jsonb,
'Ocean wave crash 2–4s: spray/rocks; perfect on hits and chorus accents.'),

('00000000-0000-0000-0000-000000000097','Food Steam Macro',ARRAY['shot','food','steam','macro'],'shot','en',
'{"shot_type":"steam_macro","duration_s":[1,3],"motion":"micro_pan","subjects":["steam","fire","hands_serving"],"use_when":["verse","build"]}'::jsonb,
'Food steam macro 1–3s: steam/fire/hands serving; adds texture in verses/builds.'),

('00000000-0000-0000-0000-000000000098','Fabric Swirl Insert',ARRAY['shot','fabric','motion','bollywood'],'shot','en',
'{"shot_type":"fabric_swirl","duration_s":[1,3],"motion":"fast_swirl","subjects":["fabric","color","sparkles"],"use_when":["chorus_peak"]}'::jsonb,
'Fabric swirl insert 1–3s: color + sparkle; excellent chorus peak “masala” accent shot.'),

('00000000-0000-0000-0000-000000000099','Hands Work Close (Craft)',ARRAY['shot','hands','craft','detail'],'shot','en',
'{"shot_type":"hands_craft","duration_s":[2,4],"motion":"steady","subjects":["weaving","painting","carving"],"use_when":["verse","story"]}'::jsonb,
'Hands craft close 2–4s: weaving/painting/carving; steady; story-driven verse coverage.'),

('00000000-0000-0000-0000-000000000100','Cloud Timelapse (Epic Build)',ARRAY['shot','clouds','timelapse','epic'],'shot','en',
'{"shot_type":"cloud_timelapse","duration_s":[2,7],"motion":"timelapse","subjects":["storm_clouds","sun_break"],"use_when":["build","final_chorus"]}'::jsonb,
'Cloud timelapse 2–7s: storm → sun break; epic build and final chorus energy.'),

-- ============================================================
-- TYPOGRAPHY (101..110)
-- ============================================================
('00000000-0000-0000-0000-000000000101','Karaoke Minimal (Mobile Safe)',ARRAY['typography','karaoke','minimal','mobile_safe'],'typography','en',
'{"mode":"karaoke","layout":"bottom_safe_area","style":{"font":"sans","weight":"600","size":"md","shadow":"subtle"},"highlight":{"type":"word","speed":"aligned"}}'::jsonb,
'Minimal karaoke captions: bottom safe area, readable sans, subtle shadow, word-level highlight aligned to timestamps.'),

('00000000-0000-0000-0000-000000000102','Hero Chorus Lines (Big Impact)',ARRAY['typography','hero_lines','chorus','impact'],'typography','en',
'{"mode":"hero_lines","layout":"center_or_thirds","style":{"font":"display_sans","weight":"800","size":"xl"},"rules":{"chorus_only":true,"max_words":8},"animate":"scale_fade"}'::jsonb,
'Hero chorus lines: big cinematic type, chorus-only, max 8 words, scale+fade animation, mobile-safe placement.'),

('00000000-0000-0000-0000-000000000103','Doc Subtitles (B-roll)',ARRAY['typography','subtitles','documentary','broll'],'typography','en',
'{"mode":"subtitles","layout":"lower_third_left","style":{"font":"sans","weight":"500","size":"sm","bg":"semi_transparent"},"rules":{"line_level":true,"no_karaoke":true}}'::jsonb,
'Documentary subtitles: lower-third left, semi-transparent background, line-level only, clean readable for b-roll.'),

('00000000-0000-0000-0000-000000000104','Chorus Punch Type',ARRAY['typography','chorus','punch','kinetic'],'typography','en',
'{"mode":"chorus_punch","layout":"center","style":{"font":"bold_sans","weight":"900","size":"xl"},"animate":{"type":"punch_in","on":"downbeats"}}'::jsonb,
'Chorus punch typography: bold center text, punch-in on downbeats for high impact.'),

('00000000-0000-0000-0000-000000000105','Handwritten Romantic Notes',ARRAY['typography','handwritten','romance','soft'],'typography','en',
'{"mode":"overlay_notes","layout":"corners","style":{"font":"handwritten","weight":"600","size":"md"},"rules":{"sparse":true,"max_lines":2}}'::jsonb,
'Handwritten romantic overlay notes: sparse corner placement, max 2 lines, soft feel for romantic b-roll arcs.'),

('00000000-0000-0000-0000-000000000106','Neon Caption Tags',ARRAY['typography','neon','tags','urban'],'typography','en',
'{"mode":"tags","layout":"upper_left","style":{"font":"condensed_sans","weight":"700","size":"sm","stroke":"neon"},"rules":{"short_phrases":true}}'::jsonb,
'Neon tag captions: short phrases, condensed sans, neon stroke, upper-left placement for urban neon montages.'),

('00000000-0000-0000-0000-000000000107','Minimal Center Line',ARRAY['typography','minimal','center','calm'],'typography','en',
'{"mode":"line_caption","layout":"center_low","style":{"font":"sans","weight":"600","size":"md"},"rules":{"one_line":true}}'::jsonb,
'Minimal center line captions: one line only, calm placement, clean sans for minimal lyric/video arcs.'),

('00000000-0000-0000-0000-000000000108','Trailer Big Title Cards',ARRAY['typography','trailer','title_cards','epic'],'typography','en',
'{"mode":"title_cards","layout":"full_frame","style":{"font":"display_serif","weight":"800","size":"xxl"},"rules":{"few_cards":true,"max_cards":6}}'::jsonb,
'Epic trailer title cards: very large type, few cards (<=6), full-frame impact for epic/trailer arcs.'),

('00000000-0000-0000-0000-000000000109','Bilingual Subtitles (EN+HI)',ARRAY['typography','subtitles','bilingual','india'],'typography','en',
'{"mode":"bilingual_subtitles","layout":"bottom_safe_area","style":{"font":"sans","weight":"600","size":"sm"},"rules":{"lines":2,"order":"native_then_en"}}'::jsonb,
'Bilingual subtitles: 2 lines in bottom safe area, native then English; useful for India-first content.'),

('00000000-0000-0000-0000-000000000110','Kinetic Word Split (Rap)',ARRAY['typography','kinetic','rap','split'],'typography','en',
'{"mode":"kinetic_split","layout":"center","style":{"font":"bold_sans","weight":"900","size":"xl"},"animate":{"type":"split_on_syllables","on":"hits"}}'::jsonb,
'Kinetic rap type: split words on hits/syllables, bold center placement, high energy without lip-sync.'),

-- ============================================================
-- EDIT PACKS (111..120)
-- ============================================================
('00000000-0000-0000-0000-000000000111','Pop Chorus Peak Cut Rules',ARRAY['edit','pop','chorus_peak','premiere'],'edit','en',
'{"cut_density":{"intro":"low","verse":"medium","chorus":"high","bridge":"low"},"rules":{"chorus_cut_on_downbeat":true,"max_shot_s":2.5,"allow_flash_cuts":true},"transitions":{"chorus":["hard_cut","whip_pan"],"bridge":["dissolve"]}}'::jsonb,
'Pop edit pacing: chorus high cut density, cut on downbeats, max 2.5s shots, allow flash cuts; bridge dissolves.'),

('00000000-0000-0000-0000-000000000112','Slow Cinema Indie Rules',ARRAY['edit','indie','slow_cinema','premium'],'edit','en',
'{"cut_density":{"intro":"low","verse":"low","chorus":"medium","bridge":"low"},"rules":{"min_shot_s":3.0,"avoid_strobe":true,"prefer_matchcuts":true},"transitions":{"default":["matchcut","crossfade"]}}'::jsonb,
'Slow cinema: longer shots (>=3s), avoid strobe, prefer match cuts + crossfades; premium calm pacing.'),

('00000000-0000-0000-0000-000000000113','Bridge Breath Hold',ARRAY['edit','bridge','breath','cinematic'],'edit','en',
'{"cut_density":{"bridge":"low"},"rules":{"hold_shots_on_bridge":true,"min_shot_s":4.0},"transitions":{"bridge":["dissolve","fade_black_short"]}}'::jsonb,
'Bridge breath: hold shots >=4s, low cut density, dissolve/fade-black shorts for emotional reset.'),

('00000000-0000-0000-0000-000000000114','EDM Drop Impact Rules',ARRAY['edit','edm','drop','impact'],'edit','en',
'{"cut_density":{"build":"medium","drop":"very_high"},"rules":{"hit_cut_on_kick":true,"allow_glitch":true,"max_shot_s_drop":1.2},"transitions":{"drop":["hard_cut","flash_cut","glitch"]}}'::jsonb,
'EDM drop rules: very high cut density on drop, hit cuts on kick, max 1.2s shots, flash+glitch transitions.'),

('00000000-0000-0000-0000-000000000115','Documentary Story Rules',ARRAY['edit','documentary','story','broll'],'edit','en',
'{"cut_density":{"verse":"medium","chorus":"medium"},"rules":{"prefer_continuity":true,"avoid_flash":true},"transitions":{"default":["straight_cut","crossfade_light"]}}'::jsonb,
'Doc story edit: continuity-first, avoid flash, straight cuts + light crossfades; stable narrative b-roll.'),

('00000000-0000-0000-0000-000000000116','Trailer Build-Peak-Release',ARRAY['edit','trailer','epic','build'],'edit','en',
'{"cut_density":{"intro":"low","build":"medium","peak":"high","release":"medium"},"rules":{"title_cards_sparse":true,"peak_on_final_chorus":true},"transitions":{"peak":["hard_cut","flash_cut"],"release":["crossfade"]}}'::jsonb,
'Trailer edit: build→peak→release, sparse title cards, peak on final chorus; hard/flash cuts at peak then crossfade release.'),

('00000000-0000-0000-0000-000000000117','Travel Postcard Flow',ARRAY['edit','travel','postcard','smooth'],'edit','en',
'{"rules":{"use_location_cards":true,"max_card_s":1.2},"transitions":{"default":["postcard_wipe","matchcut","crossfade"]},"cut_density":{"verse":"medium","chorus":"medium_high"}}'::jsonb,
'Travel postcard: location cards <=1.2s, postcard wipes + match cuts + crossfades; smooth travel montage flow.'),

('00000000-0000-0000-0000-000000000118','Rap Punch + Texture',ARRAY['edit','rap','punch','grit'],'edit','en',
'{"cut_density":{"verse":"high","chorus":"high"},"rules":{"use_texture_inserts":true,"max_shot_s":2.0},"transitions":{"default":["hard_cut","whip_pan"]}}'::jsonb,
'Rap edit: punchy high cut density, texture inserts, max 2s shots, hard cuts + whip pans for grit.'),

('00000000-0000-0000-0000-000000000119','Romantic Soft Flow',ARRAY['edit','romance','soft','cinematic'],'edit','en',
'{"cut_density":{"verse":"low","chorus":"medium"},"rules":{"prefer_dissolve":true,"avoid_hard_cuts":true},"transitions":{"default":["dissolve","crossfade","matchcut"]}}'::jsonb,
'Romantic edit: soft dissolves, avoid hard cuts, low verse density and medium chorus, match cuts for elegance.'),

('00000000-0000-0000-0000-000000000120','Festival Flash Energy',ARRAY['edit','festival','flash','high_energy'],'edit','en',
'{"cut_density":{"chorus":"very_high"},"rules":{"flash_cuts_on_hits":true,"strobe_safe":true},"transitions":{"chorus":["flash_cut","hard_cut","whip_pan_light"]}}'::jsonb,
'Festival edit: very high chorus density, flash cuts on hits (strobe-safe), hard cuts + light whip pans for peak energy.')

ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name,
  tags = EXCLUDED.tags,
  preset_type = EXCLUDED.preset_type,
  language_hint = EXCLUDED.language_hint,
  content = EXCLUDED.content,
  text_for_embedding = EXCLUDED.text_for_embedding,
  updated_at = now();

COMMIT;