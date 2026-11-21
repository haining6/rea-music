# ACE-Step 1.5 REST 路由清单（运行时发现，非硬编码）

- 发现时间：2026-11-20T19:27:19+0800
- 来源：`GET http://127.0.0.1:8001/openapi.json`（服务自描述）
- 路由数：38
- 说明：本文件由 `src/rea_music/generation/acestep.py::probe()` 自动生成，
  仅供主理人接线参考；调用语义（参数/响应 schema）仍须按服务实测核对，
  REA-Music 禁止硬编码猜测路径。

| # | 方法 | 路径 | operationId | 摘要 | 参数 | JSON body |
|---|------|------|-------------|------|------|-----------|
| 1 | POST | `/create_random_sample` | create_random_sample_endpoint_create_random_sample_post | Create Random Sample Endpoint | authorization(header) | no |
| 2 | POST | `/format_input` | format_input_endpoint_format_input_post | Format Input Endpoint | authorization(header) | no |
| 3 | GET | `/health` | health_check_health_get | Health Check | — | no |
| 4 | POST | `/query_result` | query_result_endpoint_query_result_post | Query Result Endpoint | authorization(header) | no |
| 5 | POST | `/release_task` | create_music_generate_job_release_task_post | Create Music Generate Job | authorization(header) | no |
| 6 | GET | `/v1/audio` | get_audio_v1_audio_get | Get Audio | path(query*); authorization(header) | no |
| 7 | POST | `/v1/chat/completions` | chat_completions_v1_chat_completions_post | Chat Completions | — | no |
| 8 | POST | `/v1/create_sample` | create_sample_endpoint_v1_create_sample_post | Create Sample Endpoint | authorization(header) | no |
| 9 | POST | `/v1/dataset/auto_label` | auto_label_dataset_v1_dataset_auto_label_post | Auto Label Dataset | authorization(header) | yes |
| 10 | POST | `/v1/dataset/auto_label_async` | auto_label_dataset_async_v1_dataset_auto_label_async_post | Auto Label Dataset Async | authorization(header) | yes |
| 11 | GET | `/v1/dataset/auto_label_status` | get_auto_label_status_latest_v1_dataset_auto_label_status_get | Get Auto Label Status Latest | authorization(header) | no |
| 12 | GET | `/v1/dataset/auto_label_status/{task_id}` | get_auto_label_status_v1_dataset_auto_label_status__task_id__get | Get Auto Label Status | task_id(path*); authorization(header) | no |
| 13 | POST | `/v1/dataset/load` | load_dataset_v1_dataset_load_post | Load Dataset | authorization(header) | yes |
| 14 | POST | `/v1/dataset/preprocess` | preprocess_dataset_v1_dataset_preprocess_post | Preprocess Dataset | authorization(header) | yes |
| 15 | POST | `/v1/dataset/preprocess_async` | preprocess_dataset_async_v1_dataset_preprocess_async_post | Preprocess Dataset Async | authorization(header) | yes |
| 16 | GET | `/v1/dataset/preprocess_status` | get_preprocess_status_latest_v1_dataset_preprocess_status_get | Get Preprocess Status Latest | authorization(header) | no |
| 17 | GET | `/v1/dataset/preprocess_status/{task_id}` | get_preprocess_status_v1_dataset_preprocess_status__task_id__get | Get Preprocess Status | task_id(path*); authorization(header) | no |
| 18 | GET | `/v1/dataset/sample/{sample_idx}` | get_sample_v1_dataset_sample__sample_idx__get | Get Sample | sample_idx(path*); authorization(header) | no |
| 19 | PUT | `/v1/dataset/sample/{sample_idx}` | update_sample_v1_dataset_sample__sample_idx__put | Update Sample | sample_idx(path*); authorization(header) | yes |
| 20 | GET | `/v1/dataset/samples` | get_all_samples_v1_dataset_samples_get | Get All Samples | authorization(header) | no |
| 21 | POST | `/v1/dataset/save` | save_dataset_v1_dataset_save_post | Save Dataset | authorization(header) | yes |
| 22 | POST | `/v1/dataset/scan` | scan_dataset_directory_v1_dataset_scan_post | Scan Dataset Directory | authorization(header) | yes |
| 23 | POST | `/v1/init` | init_model_v1_init_post | Init Model | authorization(header) | yes |
| 24 | POST | `/v1/lora/load` | load_lora_endpoint_v1_lora_load_post | Load Lora Endpoint | authorization(header) | yes |
| 25 | POST | `/v1/lora/scale` | set_lora_scale_endpoint_v1_lora_scale_post | Set Lora Scale Endpoint | authorization(header) | yes |
| 26 | GET | `/v1/lora/status` | get_lora_status_endpoint_v1_lora_status_get | Get Lora Status Endpoint | authorization(header) | no |
| 27 | POST | `/v1/lora/toggle` | toggle_lora_endpoint_v1_lora_toggle_post | Toggle Lora Endpoint | authorization(header) | yes |
| 28 | POST | `/v1/lora/unload` | unload_lora_endpoint_v1_lora_unload_post | Unload Lora Endpoint | authorization(header) | no |
| 29 | GET | `/v1/model_inventory` | model_inventory_v1_model_inventory_get | Model Inventory | authorization(header) | no |
| 30 | GET | `/v1/models` | list_models_v1_models_get | List Models | authorization(header) | no |
| 31 | POST | `/v1/reinitialize` | reinitialize_service_v1_reinitialize_post | Reinitialize Service | authorization(header) | no |
| 32 | GET | `/v1/stats` | get_stats_v1_stats_get | Get Stats | authorization(header) | no |
| 33 | POST | `/v1/training/export` | export_lora_v1_training_export_post | Export Lora | authorization(header) | yes |
| 34 | POST | `/v1/training/load_tensor_info` | load_tensor_info_v1_training_load_tensor_info_post | Load Tensor Info | authorization(header) | yes |
| 35 | POST | `/v1/training/start` | start_training_v1_training_start_post | Start Training | authorization(header) | yes |
| 36 | POST | `/v1/training/start_lokr` | start_lokr_training_v1_training_start_lokr_post | Start Lokr Training | authorization(header) | yes |
| 37 | GET | `/v1/training/status` | get_training_status_v1_training_status_get | Get Training Status | authorization(header) | no |
| 38 | POST | `/v1/training/stop` | stop_training_v1_training_stop_post | Stop Training | authorization(header) | no |
