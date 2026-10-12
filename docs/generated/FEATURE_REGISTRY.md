# Feature registry (generated - do not edit)

Written by `scripts/feature_inventory.py` from the live registries. `tests/test_zero_regression_contract.py` fails if anything listed here disappears.

| section | count |
|---|---|
| tools | 210 |
| agents | 23 |
| bridge_methods | 20 |
| model_slots | 8 |
| validators | 12 |
| autofix_strategies | 14 |
| task_kinds | 18 |
| pipeline_stages | 10 |
| route_targets | 168 |
| database_tables | 18 |
| bat_files | 13 |
| ops_commands | 16 |
| config_keys | 121 |
| ui_capabilities | 77 |
| test functions | 906 |

## Tools

| tool | module | risk |
|---|---|---|
| `acknowledge_alert` | industrial.plant_tools | LOW_RISK |
| `add_employee` | core.vault_tools | MEDIUM_RISK |
| `add_event` | core.builtin_tools | SAFE |
| `add_file_to_vault` | core.vault_tools | MEDIUM_RISK |
| `add_note` | core.builtin_tools | SAFE |
| `add_todo` | core.builtin_tools | SAFE |
| `agenda_for` | core.builtin_tools | SAFE |
| `agent_roster` | core.agni_tools | SAFE |
| `analyze_image` | core.builtin_tools | SAFE |
| `analyze_spreadsheet` | industrial.plant_tools | SAFE |
| `apply_wfp_firewall` | core.agni_tools | HIGH_RISK |
| `approval_note` | industrial.plant_tools | LOW_RISK |
| `approve_permit` | industrial.plant_tools | HIGH_RISK |
| `ask_industrial_kb` | industrial.tools | SAFE |
| `ask_knowledge_base` | core.builtin_tools | SAFE |
| `cancel_reminder` | core.builtin_tools | SAFE |
| `cancel_shutdown` | core.builtin_tools | LOW_RISK |
| `cancel_task` | core.builtin_tools | SAFE |
| `capability_report` | core.orchestration.tools | SAFE |
| `classify_industrial_document` | industrial.tools | SAFE |
| `clear_industrial_knowledge_base` | industrial.tools | MEDIUM_RISK |
| `clear_knowledge_base` | core.builtin_tools | MEDIUM_RISK |
| `close_application` | core.builtin_tools | MEDIUM_RISK |
| `close_window` | core.builtin_tools | MEDIUM_RISK |
| `complete_todo` | core.builtin_tools | SAFE |
| `convert_file` | core.builtin_tools | LOW_RISK |
| `copy_file` | core.builtin_tools | LOW_RISK |
| `create_folder` | core.builtin_tools | LOW_RISK |
| `create_presentation` | core.builtin_tools | MEDIUM_RISK |
| `create_schedule` | core.builtin_tools | MEDIUM_RISK |
| `create_spreadsheet` | core.builtin_tools | MEDIUM_RISK |
| `delete_event` | core.builtin_tools | SAFE |
| `delete_file` | core.builtin_tools | HIGH_RISK |
| `delete_note` | core.builtin_tools | SAFE |
| `delete_todo` | core.builtin_tools | SAFE |
| `department_roster` | core.vault_tools | LOW_RISK |
| `describe_agent` | core.builtin_tools | SAFE |
| `disengage_lockdown` | core.agni_tools | HIGH_RISK |
| `engage_lockdown` | core.agni_tools | MEDIUM_RISK |
| `engineering_calculate` | core.agni_tools | SAFE |
| `engineering_formula` | core.agni_tools | SAFE |
| `equipment_details` | industrial.plant_tools | SAFE |
| `equipment_presentation` | industrial.plant_tools | LOW_RISK |
| `equipment_trend` | industrial.plant_tools | SAFE |
| `executive_briefing_pack` | industrial.plant_tools | LOW_RISK |
| `executive_presentation` | industrial.plant_tools | LOW_RISK |
| `executive_report` | industrial.plant_tools | LOW_RISK |
| `executive_workbook` | industrial.plant_tools | LOW_RISK |
| `explain_last_run` | core.orchestration.tools | SAFE |
| `export_plant_data` | industrial.plant_tools | LOW_RISK |
| `export_vault_file` | core.vault_tools | HIGH_RISK |
| `extract_industrial_document` | industrial.tools | SAFE |
| `face_status` | core.agni_tools | SAFE |
| `find_employee` | core.vault_tools | LOW_RISK |
| `find_generated_document` | industrial.plant_tools | SAFE |
| `find_part` | industrial.plant_tools | SAFE |
| `find_sop` | industrial.plant_tools | SAFE |
| `firewall_status` | core.agni_tools | SAFE |
| `focus_window` | core.builtin_tools | LOW_RISK |
| `forget_document` | core.builtin_tools | LOW_RISK |
| `forget_fact` | core.builtin_tools | LOW_RISK |
| `forget_industrial_document` | industrial.tools | LOW_RISK |
| `get_battery` | core.builtin_tools | SAFE |
| `get_clipboard` | core.builtin_tools | SAFE |
| `get_cpu_usage` | core.builtin_tools | SAFE |
| `get_disk_usage` | core.builtin_tools | SAFE |
| `get_memory_usage` | core.builtin_tools | SAFE |
| `get_system_info` | core.builtin_tools | SAFE |
| `get_time` | core.builtin_tools | SAFE |
| `hardware_profile` | core.agni_tools | SAFE |
| `hotkey` | core.builtin_tools | LOW_RISK |
| `import_readings_csv` | core.agni_tools | MEDIUM_RISK |
| `index_file` | core.builtin_tools | LOW_RISK |
| `index_folder` | core.builtin_tools | LOW_RISK |
| `index_my_documents` | core.builtin_tools | LOW_RISK |
| `industrial_agent_status` | industrial.tools | SAFE |
| `industrial_model_health` | industrial.tools | SAFE |
| `industrial_status` | industrial.tools | SAFE |
| `industrial_term` | industrial.tools | SAFE |
| `ingest_industrial_document` | industrial.tools | LOW_RISK |
| `ingest_industrial_folder` | industrial.tools | LOW_RISK |
| `ingest_industrial_knowledge_corpus` | industrial.tools | LOW_RISK |
| `inspection_history` | industrial.plant_tools | SAFE |
| `inventory_summary` | industrial.plant_tools | SAFE |
| `knowledge_stats` | core.builtin_tools | SAFE |
| `list_agents` | core.builtin_tools | SAFE |
| `list_artifacts` | core.agni_tools | SAFE |
| `list_employees` | core.vault_tools | LOW_RISK |
| `list_engineering_formulas` | core.agni_tools | SAFE |
| `list_equipment` | industrial.plant_tools | SAFE |
| `list_events` | core.builtin_tools | SAFE |
| `list_generated_documents` | industrial.plant_tools | SAFE |
| `list_industrial_agents` | industrial.tools | SAFE |
| `list_industrial_documents` | industrial.tools | SAFE |
| `list_isolations` | industrial.plant_tools | SAFE |
| `list_knowledge_sources` | core.builtin_tools | SAFE |
| `list_meetings` | core.builtin_tools | SAFE |
| `list_memories` | core.builtin_tools | SAFE |
| `list_notes` | core.builtin_tools | SAFE |
| `list_permits` | industrial.plant_tools | SAFE |
| `list_plant_alerts` | industrial.plant_tools | SAFE |
| `list_plant_datasets` | industrial.plant_tools | SAFE |
| `list_running_apps` | core.builtin_tools | SAFE |
| `list_specialists` | core.orchestration.tools | SAFE |
| `list_todos` | core.builtin_tools | SAFE |
| `list_users` | core.agni_tools | LOW_RISK |
| `list_vault_files` | core.vault_tools | SAFE |
| `list_windows` | core.builtin_tools | SAFE |
| `lock_screen` | core.builtin_tools | MEDIUM_RISK |
| `lock_vault` | core.vault_tools | SAFE |
| `log_inspection` | industrial.plant_tools | LOW_RISK |
| `log_maintenance` | industrial.plant_tools | LOW_RISK |
| `maintenance_due` | industrial.plant_tools | SAFE |
| `maintenance_history` | industrial.plant_tools | SAFE |
| `maintenance_report` | industrial.plant_tools | LOW_RISK |
| `maximize_window` | core.builtin_tools | LOW_RISK |
| `media_control` | core.builtin_tools | SAFE |
| `minimize_window` | core.builtin_tools | LOW_RISK |
| `model_integrity_report` | core.agni_tools | SAFE |
| `model_routing` | core.orchestration.tools | SAFE |
| `monitoring_status` | industrial.plant_tools | SAFE |
| `mouse_click` | core.builtin_tools | MEDIUM_RISK |
| `mouse_move` | core.builtin_tools | LOW_RISK |
| `move_file` | core.builtin_tools | MEDIUM_RISK |
| `network_status` | core.agni_tools | SAFE |
| `open_and_type` | core.builtin_tools | MEDIUM_RISK |
| `open_application` | core.builtin_tools | LOW_RISK |
| `open_browser` | core.builtin_tools | SAFE |
| `open_document` | industrial.plant_tools | LOW_RISK |
| `open_file_explorer` | core.builtin_tools | SAFE |
| `open_settings` | core.builtin_tools | LOW_RISK |
| `open_vault_file` | core.vault_tools | HIGH_RISK |
| `operations_presentation` | industrial.plant_tools | LOW_RISK |
| `overdue_tasks` | core.builtin_tools | SAFE |
| `pin_trusted_models` | core.agni_tools | HIGH_RISK |
| `pipeline_status` | core.orchestration.tools | SAFE |
| `plant_data_sources` | industrial.plant_tools | SAFE |
| `plant_snapshot` | core.agni_tools | SAFE |
| `plant_status` | industrial.plant_tools | SAFE |
| `plant_telemetry` | core.agni_tools | SAFE |
| `press_key` | core.builtin_tools | LOW_RISK |
| `process_meeting_audio` | core.agni_tools | MEDIUM_RISK |
| `process_meeting_transcript` | core.agni_tools | MEDIUM_RISK |
| `productivity_snapshot` | core.agni_tools | SAFE |
| `rag_write_document` | core.builtin_tools | MEDIUM_RISK |
| `raise_plant_alert` | industrial.plant_tools | LOW_RISK |
| `read_file` | core.builtin_tools | LOW_RISK |
| `read_screen` | core.builtin_tools | SAFE |
| `record_isolation` | industrial.plant_tools | MEDIUM_RISK |
| `register_equipment` | industrial.plant_tools | LOW_RISK |
| `register_face` | core.agni_tools | MEDIUM_RISK |
| `release_models` | core.orchestration.tools | LOW_RISK |
| `remember_fact` | core.builtin_tools | SAFE |
| `remind_me` | core.builtin_tools | SAFE |
| `remove_employee` | core.vault_tools | HIGH_RISK |
| `remove_face` | core.agni_tools | HIGH_RISK |
| `remove_vault_file` | core.vault_tools | HIGH_RISK |
| `remove_wfp_firewall` | core.agni_tools | HIGH_RISK |
| `request_permit` | industrial.plant_tools | MEDIUM_RISK |
| `resolve_alert` | industrial.plant_tools | MEDIUM_RISK |
| `restart_pc` | core.builtin_tools | CRITICAL |
| `run_command` | core.builtin_tools | HIGH_RISK |
| `run_diagnostics` | core.builtin_tools | SAFE |
| `run_industrial_agent` | core.agni_tools | LOW_RISK |
| `run_python_sandboxed` | core.agni_tools | MEDIUM_RISK |
| `safety_report` | industrial.plant_tools | LOW_RISK |
| `safety_status` | industrial.plant_tools | SAFE |
| `schedule_maintenance` | industrial.plant_tools | LOW_RISK |
| `schedule_meeting` | core.builtin_tools | SAFE |
| `search_files` | core.builtin_tools | SAFE |
| `search_industrial_evidence` | industrial.tools | SAFE |
| `search_knowledge` | core.builtin_tools | SAFE |
| `security_events` | core.agni_tools | SAFE |
| `security_status` | core.agni_tools | SAFE |
| `seed_industrial_demo_data` | industrial.plant_tools | MEDIUM_RISK |
| `set_clipboard` | core.builtin_tools | LOW_RISK |
| `set_isolation_state` | industrial.plant_tools | HIGH_RISK |
| `set_network_mode` | core.agni_tools | HIGH_RISK |
| `set_user_role` | core.agni_tools | HIGH_RISK |
| `show_audit_log` | core.builtin_tools | SAFE |
| `shutdown_pc` | core.builtin_tools | CRITICAL |
| `solve_equation` | core.agni_tools | SAFE |
| `sovereignty_report` | core.agni_tools | SAFE |
| `spreadsheet_to_presentation` | industrial.plant_tools | LOW_RISK |
| `spreadsheet_to_report` | industrial.plant_tools | LOW_RISK |
| `stop_watching_folder` | core.builtin_tools | SAFE |
| `system_information` | industrial.plant_tools | SAFE |
| `take_screenshot` | core.builtin_tools | LOW_RISK |
| `type_text` | core.builtin_tools | MEDIUM_RISK |
| `uncomplete_todo` | core.builtin_tools | SAFE |
| `unit_convert` | core.agni_tools | SAFE |
| `update_employee` | core.vault_tools | MEDIUM_RISK |
| `update_note` | core.builtin_tools | SAFE |
| `update_schedule` | core.builtin_tools | MEDIUM_RISK |
| `update_stock` | industrial.plant_tools | LOW_RISK |
| `update_todo` | core.builtin_tools | SAFE |
| `validation_report` | core.orchestration.tools | SAFE |
| `vault_audit_log` | core.vault_tools | SAFE |
| `vault_overview` | core.agni_tools | SAFE |
| `vault_snapshot` | core.agni_tools | LOW_RISK |
| `vault_status` | core.vault_tools | SAFE |
| `verify_artifact` | core.agni_tools | SAFE |
| `verify_audit_chain` | core.agni_tools | SAFE |
| `verify_my_face` | core.agni_tools | SAFE |
| `view_logs` | industrial.plant_tools | SAFE |
| `watch_folder` | core.builtin_tools | LOW_RISK |
| `wfp_blocked_connections` | core.agni_tools | MEDIUM_RISK |
| `wfp_firewall_status` | core.agni_tools | SAFE |
| `write_document` | core.builtin_tools | MEDIUM_RISK |
| `write_file` | core.builtin_tools | MEDIUM_RISK |

## Agents

| id | name | tools |
|---|---|---|
| `operations` | Operations Agent | 0 |
| `maintenance` | Maintenance Agent | 0 |
| `inspection` | Inspection Agent | 0 |
| `safety` | Safety Agent | 0 |
| `inventory` | Inventory Agent | 0 |
| `alerts` | Alert Agent | 0 |
| `report` | Report Agent | 0 |
| `document` | Document Agent | 0 |
| `data_analysis` | Data Analysis Agent | 0 |
| `knowledge` | Industrial Knowledge Agent | 0 |
| `vision` | Vision Agent | 0 |
| `engineering` | Engineering Agent | 0 |
| `coding` | Coding Agent | 0 |
| `file` | File Agent | 0 |
| `automation` | Automation Agent | 0 |
| `memory` | Memory Agent | 0 |
| `system` | System Agent | 0 |
| `productivity` | Productivity Agent | 0 |
| `vault` | Secure Locker Agent | 0 |
| `people` | Employee Register Agent | 0 |
| `security` | Security Agent | 0 |
| `industrial_agents` | Industrial Agent Runner | 0 |
| `conversation` | Conversation Agent | 0 |

## Bridge Methods

`change_password`, `create_user`, `hello`, `history`, `listen`, `lock`, `logout`, `open_path`, `pick_file`, `pick_folder`, `probe_network`, `resolve_approval`, `set_speak`, `state`, `status`, `stop_speaking`, `submit`, `telemetry`, `tool`, `unlock_vault`

## Model Slots

`classify`, `code`, `default`, `fast`, `json_structured`, `multilingual`, `reasoning`, `vision`

## Validators

`bundle`, `code`, `csv`, `data`, `docx`, `file`, `ocr`, `pdf`, `pptx`, `rag`, `text`, `xlsx`

## Autofix Strategies

`broken_formula_reference`, `empty_deck`, `empty_document`, `empty_workbook`, `file_corrupt`, `file_empty`, `file_missing`, `layout_overflow`, `low_confidence`, `low_extraction`, `no_path_returned`, `syntax_error`, `tool_reported_error`, `type_too_small`

## Task Kinds

`automation`, `chat`, `code`, `conversion`, `document_read`, `excel`, `file_ops`, `industrial_data`, `memory`, `multi_step`, `ocr`, `pdf`, `powerpoint`, `rag`, `reasoning`, `unknown`, `vision`, `word`

## Pipeline Stages

`authorize`, `autofix`, `classify`, `deliver`, `execute`, `intent`, `memory`, `plan`, `route`, `validate`

## Database Tables

`auth_events`, `calendar_events`, `document_chunks`, `employees`, `facts`, `industrial_assets`, `industrial_chunks`, `industrial_documents`, `industrial_ingestion_log`, `messages`, `notes`, `schedules`, `sessions`, `task_history`, `todos`, `users`, `vault_audit`, `vault_files`

## Bat Files

`APPLY_FIREWALL.bat`, `FIREWALL_AGNI.bat`, `INSTALL.bat`, `STEP 1 : INSTALL`, `INSTALL_ALL.bat`, `REMOVE_FIREWALL.bat`, `REPAIR.bat`, `STEP 1 : INSTALL`, `SETUP_VOICE.bat`, `START.bat`, `STEP 2 : START`, `TEST_AGNI.bat`, `VERIFY_MODELS.bat`

## Ops Commands

`firewall`, `firewall all`, `firewall apply`, `firewall audit`, `firewall lockdown`, `firewall remove`, `firewall restore`, `firewall sovereign`, `firewall status`, `firewall tamper`, `firewall test`, `install-verify`, `preflight`, `repair`, `system`, `test`

## Ui Capabilities

`bridge:change_password`, `bridge:create_user`, `bridge:hello`, `bridge:history`, `bridge:listen`, `bridge:lock`, `bridge:logout`, `bridge:open_path`, `bridge:pick_file`, `bridge:pick_folder`, `bridge:probe_network`, `bridge:resolve_approval`, `bridge:set_speak`, `bridge:state`, `bridge:status`, `bridge:stop_speaking`, `bridge:submit`, `bridge:telemetry`, `bridge:unlock_vault`, `tool:acknowledge_alert`, `tool:add_employee`, `tool:add_event`, `tool:add_file_to_vault`, `tool:add_note`, `tool:add_todo`, `tool:agent_roster`, `tool:apply_wfp_firewall`, `tool:approval_note`, `tool:ask_industrial_kb`, `tool:capability_report`, `tool:delete_event`, `tool:delete_note`, `tool:delete_todo`, `tool:disengage_lockdown`, `tool:engage_lockdown`, `tool:equipment_details`, `tool:export_vault_file`, `tool:face_status`, `tool:hardware_profile`, `tool:ingest_industrial_document`, `tool:ingest_industrial_knowledge_corpus`, `tool:knowledge_stats`, `tool:list_artifacts`, `tool:list_industrial_documents`, `tool:list_knowledge_sources`, `tool:list_users`, `tool:lock_vault`, `tool:model_integrity_report`, `tool:model_routing`, `tool:network_status`, `tool:pin_trusted_models`, `tool:plant_snapshot`, `tool:plant_telemetry`, `tool:process_meeting_audio`, `tool:process_meeting_transcript`, `tool:productivity_snapshot`, `tool:register_face`, `tool:release_models`, `tool:remove_employee`, `tool:remove_face`, `tool:remove_wfp_firewall`, `tool:resolve_alert`, `tool:run_industrial_agent`, `tool:schedule_meeting`, `tool:security_status`, `tool:seed_industrial_demo_data`, `tool:set_network_mode`, `tool:set_user_role`, `tool:show_audit_log`, `tool:sovereignty_report`, `tool:vault_overview`, `tool:vault_snapshot`, `tool:verify_artifact`, `tool:verify_audit_chain`, `tool:verify_my_face`, `tool:wfp_blocked_connections`, `tool:wfp_firewall_status`

## Route Targets

`acknowledge_alert`, `add_event`, `add_file_to_vault`, `add_note`, `add_todo`, `agenda_for`, `analyze_image`, `analyze_spreadsheet`, `apply_wfp_firewall`, `approval_note`, `ask_industrial_kb`, `ask_knowledge_base`, `calculate`, `cancel_shutdown`, `cancel_task`, `classify_industrial_document`, `clear_knowledge_base`, `close_application`, `complete_todo`, `convert_file`, `copy_file`, `create_folder`, `create_presentation`, `create_spreadsheet`, `delete_event`, `delete_file`, `delete_note`, `delete_todo`, `department_roster`, `describe_agent`, `engage_lockdown`, `engineering_calculate`, `equipment_details`, `equipment_presentation`, `equipment_trend`, `executive_briefing_pack`, `executive_presentation`, `executive_report`, `executive_workbook`, `explain_last_run`, `export_plant_data`, `export_vault_file`, `extract_industrial_document`, `face_status`, `find_employee`, `find_generated_document`, `find_part`, `find_sop`, `focus_window`, `forget_document`, `forget_fact`, `get_battery`, `get_clipboard`, `get_cpu_usage`, `get_disk_usage`, `get_memory_usage`, `get_system_info`, `get_time`, `hardware_profile`, `hotkey`, `import_readings_csv`, `index_file`, `index_folder`, `index_my_documents`, `industrial_model_health`, `industrial_status`, `industrial_term`, `ingest_industrial_document`, `ingest_industrial_folder`, `ingest_industrial_knowledge_corpus`, `inspection_history`, `inventory_summary`, `knowledge_stats`, `list_agents`, `list_artifacts`, `list_employees`, `list_engineering_formulas`, `list_equipment`, `list_events`, `list_generated_documents`, `list_industrial_agents`, `list_industrial_documents`, `list_isolations`, `list_knowledge_sources`, `list_meetings`, `list_memories`, `list_notes`, `list_permits`, `list_plant_alerts`, `list_running_apps`, `list_specialists`, `list_todos`, `list_vault_files`, `list_windows`, `lock_screen`, `lock_vault`, `maintenance_due`, `maintenance_history`, `maintenance_report`, `maximize_window`, `media_control`, `minimize_window`, `model_integrity_report`, `model_routing`, `monitoring_status`, `move_file`, `network_status`, `open_and_type`, `open_application`, `open_browser`, `open_document`, `open_file_explorer`, `open_settings`, `open_vault_file`, `operations_presentation`, `overdue_tasks`, `pipeline_status`, `plant_data_sources`, `plant_status`, `plant_telemetry`, `press_key`, `process_meeting_audio`, `rag_write_document`, `read_file`, `read_screen`, `register_face`, `release_models`, `remember_fact`, `remind_me`, `remove_employee`, `remove_face`, `remove_vault_file`, `remove_wfp_firewall`, `resolve_alert`, `restart_pc`, `run_command`, `run_diagnostics`, `run_industrial_agent`, `safety_report`, `safety_status`, `schedule_maintenance`, `schedule_meeting`, `search_files`, `search_industrial_evidence`, `search_knowledge`, `security_status`, `seed_industrial_demo_data`, `set_clipboard`, `show_audit_log`, `shutdown_pc`, `solve_equation`, `sovereignty_report`, `spreadsheet_to_presentation`, `spreadsheet_to_report`, `stop_watching_folder`, `system_information`, `take_screenshot`, `type_text`, `unit_convert`, `validation_report`, `vault_audit_log`, `vault_status`, `verify_audit_chain`, `verify_my_face`, `view_logs`, `watch_folder`, `wfp_blocked_connections`, `wfp_firewall_status`

## Config Keys

`auth.allow_multiple_users`, `auth.app_name`, `auth.app_tagline`, `auth.debug`, `auth.lockout_seconds`, `auth.log_level`, `auth.max_failed_attempts`, `auth.min_password_length`, `auth.require_login`, `auth.session_idle_minutes`, `auth.version`, `face.app_name`, `face.app_tagline`, `face.camera_index`, `face.debug`, `face.enabled`, `face.liveness`, `face.liveness_threshold`, `face.log_level`, `face.login`, `face.login_margin`, `face.login_threshold`, `face.model_pack`, `face.threshold`, `face.version`, `memory.app_name`, `memory.app_tagline`, `memory.backend`, `memory.context_chars`, `memory.debug`, `memory.log_level`, `memory.max_results`, `memory.min_score`, `memory.on_unavailable`, `memory.version`, `models.app_name`, `models.app_tagline`, `models.debug`, `models.keep_alive`, `models.log_level`, `models.max_resident`, `models.num_ctx`, `models.version`, `nodes.app_name`, `nodes.app_tagline`, `nodes.debug`, `nodes.enabled`, `nodes.log_level`, `nodes.node_name`, `nodes.port`, `nodes.version`, `ollama.allow_remote`, `ollama.app_name`, `ollama.app_tagline`, `ollama.debug`, `ollama.generation_timeout`, `ollama.host`, `ollama.log_level`, `ollama.timeout`, `ollama.version`, `paths.app_name`, `paths.app_tagline`, `paths.base_dir`, `paths.config_dir`, `paths.data_dir`, `paths.debug`, `paths.log_dir`, `paths.log_level`, `paths.models_dir`, `paths.version`, `performance.app_name`, `performance.app_tagline`, `performance.debug`, `performance.log_level`, `performance.low_resource`, `performance.version`, `security.app_name`, `security.app_tagline`, `security.confirm_threshold`, `security.debug`, `security.firewall_include_ollama`, `security.lan_ranges`, `security.log_level`, `security.network_mode`, `security.require_trusted_models`, `security.version`, `voice.app_name`, `voice.app_tagline`, `voice.debug`, `voice.enabled`, `voice.listen_max_seconds`, `voice.listen_seconds`, `voice.listen_until_silence`, `voice.log_level`, `voice.max_spoken_chars`, `voice.preload`, `voice.stt_compute_type`, `voice.stt_device`, `voice.stt_isolation`, `voice.stt_model_size`, `voice.stt_provider`, `voice.tts_cfg_weight`, `voice.tts_device`, `voice.tts_exaggeration`, `voice.tts_idle_seconds`, `voice.tts_keep_loaded`, `voice.tts_provider`, `voice.tts_temperature`, `voice.tts_voice_prompt`, `voice.tts_volume`, `voice.version`, `voice.voice_auto_play`, `voice.voice_device`, `voice.voice_fallback`, `voice.voice_interruptible`, `voice.voice_language`, `voice.voice_polish`, `voice.voice_profile`, `voice.voice_speed`, `voice.voice_streaming`, `voice.voice_style`
